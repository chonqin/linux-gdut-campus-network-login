#!/usr/bin/env python3
"""GDUT portal login on Linux; no third-party Python packages required."""
import argparse
import html
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlsplit

INTERFACE = None
SOURCE_IP = None
PING_SUPPORTED = True
PORTAL = '10.0.3.2'
CONFIG = Path('/etc/gdut-login/credentials.json')
PING_TARGETS = [
    ('百度', 'www.baidu.com'),
    ('哔哩哔哩', 'www.bilibili.com'),
    ('腾讯', 'www.qq.com'),
    ('阿里云', 'www.aliyun.com'),
]
SERVICE = 'gdut-login.service'
INSTALLED_SCRIPT = Path('/usr/local/sbin/gdut-login.py')
UNIT_FILE = Path('/etc/systemd/system/gdut-login.service')
SYSTEMD_RUNTIME = Path('/run/systemd/system')
PID1_COMM = Path('/proc/1/comm')
MAX_CONNECT_ATTEMPTS = 3
SERVICE_TEMPLATE = '''[Unit]
Description=GDUT campus network automatic login
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart="{python}" -u "{script}" --daemon
Restart=no
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes

[Install]
WantedBy=multi-user.target
'''
# AC address observed in this board's original portal redirect; login discovers it dynamically.
LOGOUT_AC_IP = '172.16.254.2'


def emit(message):
    print(message, flush=True)
    # systemd stdout is the journal; also write to the installation serial TTY.
    console = os.environ.get('GDUT_CONSOLE', '/dev/console')
    if not console or sys.stdout.isatty():
        return
    try:
        descriptor = os.open(console, os.O_WRONLY | os.O_APPEND | getattr(os, 'O_NONBLOCK', 0))
        try:
            os.write(descriptor, ('[GDUT] ' + message + '\r\n').encode('utf-8'))
        finally:
            os.close(descriptor)
    except OSError:
        pass  # A disconnected console must not stop network authentication.


def ping_latency(label, host):
    prefix = '{} ({})：'.format(label, host)
    if not PING_SUPPORTED:
        return prefix + '已跳过，请安装兼容的 iputils ping 工具。'
    if not INTERFACE:
        return prefix + '尚未识别到可用网卡。'
    try:
        result = subprocess.run(
            ['ping', '-4', '-n', '-I', INTERFACE, '-c', '3', '-W', '2', '-w', '10', host],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=12,
            env=dict(os.environ, LC_ALL='C'))
    except FileNotFoundError:
        return prefix + '未安装 ping，可安装 iputils-ping。'
    except subprocess.TimeoutExpired:
        return prefix + 'ping 超时（也可能是 DNS 解析超时）。'
    except OSError:
        return prefix + '无法执行 ping。'
    text = result.stdout.decode('utf-8', errors='replace')
    summary = re.search(r'(?:rtt|round-trip)[^\n=]*=\s*([\d.]+)/([\d.]+)/([\d.]+)', text)
    loss = re.search(r'([\d.]+)% packet loss', text)
    if summary:
        minimum, average, maximum = summary.groups()
        return prefix + '平均 {} ms，最小 {} ms，最大 {} ms，丢包 {}'.format(
            average, minimum, maximum, loss.group(1) + '%' if loss else '未知')
    if result.returncode == 1:
        return prefix + '未收到 ICMP 回应（可能丢包或对方限制 ping）。'
    return prefix + 'ping 执行异常，请检查域名解析或 ping 权限。'


def show_ping_results():
    for label, host in PING_TARGETS:
        emit(ping_latency(label, host))
    emit('ping 测试结束。')


def select_network():
    global INTERFACE, SOURCE_IP
    route = subprocess.run(['ip', '-j', '-4', 'route', 'get', PORTAL],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    if route.returncode:
        raise OSError('No route to portal')
    rows = json.loads(route.stdout)
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise ValueError('Invalid route response')
    item = rows[0]
    interface = item.get('dev')
    source = str(ipaddress.IPv4Address(item.get('prefsrc') or item.get('src')))
    if not isinstance(interface, str) or not interface or interface == 'lo':
        raise ValueError('Invalid interface')
    result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', interface],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    if result.returncode:
        raise OSError('Interface unavailable')
    devices = json.loads(result.stdout)
    if not isinstance(devices, list):
        raise ValueError('Invalid interface response')
    addresses = {entry['local'] for device in devices if isinstance(device, dict)
                 for entry in device.get('addr_info', [])
                 if isinstance(entry, dict) and entry.get('family') == 'inet'}
    if source not in addresses:
        raise ValueError('Source address does not belong to interface')
    if (INTERFACE, SOURCE_IP) != (interface, source):
        emit('已识别网卡：{}，IPv4：{}'.format(interface, source))
    INTERFACE, SOURCE_IP = interface, source
    return addresses


def systemd_available():
    try:
        return (bool(shutil.which('systemctl')) and SYSTEMD_RUNTIME.is_dir()
                and PID1_COMM.read_text(encoding='utf-8').strip() == 'systemd')
    except OSError:
        return False


def check_environment(mode):
    global PING_SUPPORTED
    if not sys.platform.startswith('linux'):
        emit('此脚本需在 Linux 板卡上运行；Windows 仅用于编辑和复制文件。')
        return False
    if sys.version_info < (3, 6):
        emit('需要 Python 3.6 或更高版本。')
        return False
    if os.geteuid() != 0:
        emit('请使用 sudo python3 gdut-login.py 运行，或以 root 用户运行。')
        return False
    if mode == 'setup':
        return True
    missing = [name for name in ('curl', 'ip') if not shutil.which(name)]
    if missing:
        emit('缺少必要命令：{}；请安装 curl 和 iproute2 后重试。'.format(', '.join(missing)))
        return False
    try:
        result = subprocess.run(['ip', '-j', '-4', 'addr', 'show'],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        if result.returncode or not isinstance(json.loads(result.stdout), list):
            raise ValueError('Incompatible ip command')
    except (OSError, ValueError, subprocess.SubprocessError):
        emit('当前 ip 命令不支持所需的 JSON 输出；请安装完整 iproute2，不能只用精简版 BusyBox ip。')
        return False
    PING_SUPPORTED = bool(shutil.which('ping'))
    if PING_SUPPORTED:
        try:
            help_result = subprocess.run(['ping', '-h'], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, timeout=5,
                                         env=dict(os.environ, LC_ALL='C'))
            help_text = (help_result.stdout + help_result.stderr).decode('utf-8', errors='replace')
            PING_SUPPORTED = all(flag in help_text for flag in ('-4', '-I', '-c', '-W', '-w'))
        except (OSError, subprocess.SubprocessError):
            PING_SUPPORTED = False
    if not PING_SUPPORTED:
        emit('未检测到兼容的 ping，延迟测试将跳过；可安装 iputils ping 工具，连接功能不受影响。')
    if mode in (None, 'login', 'logout') and not systemd_available():
        emit('当前系统未运行 systemd，暂不支持自动配置开机服务和服务管理菜单。')
        emit('可使用 --once 单次连接，或 --daemon 在前台监控；开机自启需按当前系统另行配置。')
        return False
    return True


def load_credentials():
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    if (not isinstance(config, dict) or not isinstance(config.get('account'), str)
            or not isinstance(config.get('password'), str)
            or not config['account'] or not config['password']):
        raise ValueError('Empty credentials')
    return config


def setup_credentials():
    account = input('校园网账号（与网页登录时完全一致）: ').strip()
    password = getpass.getpass('校园网密码（输入时不显示）: ')
    if not account or not password:
        raise ValueError('账号和密码不能为空，原配置未修改。')
    CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG.parent, 0o700)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                         dir=CONFIG.parent, delete=False) as temporary:
            temporary_name = temporary.name
            json.dump({'account': account, 'password': password}, temporary, ensure_ascii=False)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, CONFIG)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    emit('账号配置已保存，以后自动读取；只有 root 可以读取配置文件。')


def request(url):
    if not INTERFACE:
        select_network()
    # Send the URL over stdin so credentials do not appear in process arguments.
    config = ('url = ' + json.dumps(url, ensure_ascii=True) + '\n').encode()
    result = subprocess.run([
        'curl', '--disable', '-4', '--interface', INTERFACE, '--noproxy', '*',
        '--silent', '--show-error', '--connect-timeout', '5', '--max-time', '12',
        '--max-filesize', '1048576', '--include', '--config', '-'
    ], input=config, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=17)
    if result.returncode:
        raise OSError('HTTP request failed')
    headers, separator, body = result.stdout.partition(b'\r\n\r\n')
    if not separator:
        raise ValueError('Invalid HTTP response')
    return int(headers.split()[1]), headers.decode('latin-1'), body


def online():
    try:
        status, headers, body = request('http://www.baidu.com/')
        return (status == 200 and b'<!--STATUS OK-->' in body
                and '<title>百度一下，你就知道</title>'.encode() in body)
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return False


def tick():
    try:
        select_network()
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return False, 60, '尚无可用的校园网 IPv4 路由；请检查网卡连接和地址分配。'

    if online():
        return True, 60, '外网 HTTP 检查通过，无需重新登录。'

    try:
        status, headers, body = request('http://9.9.9.9/')
        content = headers + '\n' + body.decode('latin-1')
        match = re.search(r'(?im)^Location:\s*(https?://[^\r\n]+)', content)
        if not match:
            match = re.search(r'<NextURL>\s*([^<]+)', content, re.I)
        if not match:
            return False, 60, '外网检查未通过，未发现认证跳转；等待网络恢复。'
        target = urlsplit(html.unescape(match.group(1).strip()))
        if target.scheme != 'http' or target.hostname != PORTAL or target.port not in (None, 80):
            return False, 60, '认证跳转目标与配置不符，未发送账号密码。'
        params = parse_qs(target.query)
        user_ip = str(ipaddress.IPv4Address(params['wlanuserip'][0]))
        ac_ip = str(ipaddress.IPv4Address(params['wlanacip'][0]))
        if user_ip != SOURCE_IP:
            return False, 60, '认证跳转中的 IP 与当前路由源地址不一致，未发送账号密码。'
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return False, 60, '暂时无法获取有效认证参数；等待网络恢复。'

    try:
        credentials = load_credentials()
    except (OSError, ValueError):
        return False, 300, '账号配置缺失或无效，请检查 /etc/gdut-login/credentials.json。'

    query = urlencode({
        'user_account': credentials['account'], 'user_password': credentials['password'],
        'wlan_user_ip': user_ip, 'wlan_ac_ip': ac_ip
    })
    try:
        status, headers, body = request('http://' + PORTAL + ':801/eportal/portal/login?' + query)
        text = body.decode('utf-8', errors='replace')
        start, end = text.find('{'), text.rfind('}')
        reply = json.loads(text[start:end + 1])
        accepted = status == 200 and (str(reply.get('result')) == '1'
                                     or str(reply.get('ret_code')) == '2')
        if not accepted:
            return False, 300, '认证未成功，请核对账号配置。'
        time.sleep(2)
        if online():
            return True, 60, '自动认证完成，外网 HTTP 检查通过。'
        return False, 60, '认证请求已接受，等待外网检查通过。'
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return False, 300, '认证请求失败或返回异常。'


def configure():
    try:
        setup_credentials()
        return 0
    except (OSError, ValueError):
        emit('保存账号配置失败，请使用 sudo 运行并检查输入；原配置未主动清空。')
    except (EOFError, KeyboardInterrupt):
        emit('已取消账号配置。')
    return 1


def service_action(*arguments):
    try:
        result = subprocess.run(['systemctl'] + list(arguments) + [SERVICE],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_credentials():
    try:
        load_credentials()
        return True
    except (OSError, ValueError, AttributeError):
        if sys.stdin.isatty():
            emit('尚无有效账号配置，请先输入校园网账号密码。')
            return configure() == 0
        emit('请先在终端执行：sudo python3 /usr/local/sbin/gdut-login.py --setup')
        return False


def install_service():
    if not systemd_available():
        emit('当前系统未运行 systemd，无法创建开机服务；可使用 --once 单次连接。')
        return False
    def save(path, data, mode):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
                temporary_name = temporary.name
                temporary.write(data)
            os.chmod(temporary_name, mode)
            os.replace(temporary_name, path)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    try:
        source = Path(__file__).resolve()
        if source != INSTALLED_SCRIPT.resolve():
            save(INSTALLED_SCRIPT, source.read_bytes(), 0o755)
        unit = SERVICE_TEMPLATE.format(script=INSTALLED_SCRIPT.as_posix(), python=Path(sys.executable).as_posix())
        save(UNIT_FILE, unit.encode('utf-8'), 0o644)
        # Remember an actual serial TTY when first launched from the board terminal.
        try:
            terminal = os.ttyname(sys.stdin.fileno()) if sys.stdin.isatty() else ''
        except (AttributeError, OSError):
            terminal = ''
        if re.fullmatch(r'/dev/tty[A-Za-z0-9]+', terminal):
            console_config = UNIT_FILE.parent / (SERVICE + '.d') / 'console.conf'
            save(console_config, ('[Service]\nEnvironment="GDUT_CONSOLE=' + terminal + '"\n').encode(), 0o644)
        result = subprocess.run(['systemctl', 'daemon-reload'], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=45)
        if result.returncode:
            raise OSError('systemd reload failed')
        return True
    except (OSError, subprocess.SubprocessError):
        emit('无法准备开机服务，请使用 root 权限并检查 systemd 运行状态。')
        return False


def start_connection():
    if not ensure_credentials():
        return 1
    if not install_service():
        return 1
    if not service_action('enable') or not service_action('restart'):
        emit('无法启动自动连接服务；请使用 sudo 运行，并查看 systemd 服务日志。')
        return 1
    emit('已恢复开机自动连接，后台正在连接 gdut；连接结果会打印到串口和服务日志。')
    return 0


def logout_redirect(user_ip):
    try:
        status, headers, body = request('http://9.9.9.9/')
        text = headers + '\n' + body.decode('latin-1')
        match = re.search(r'(?im)^Location:\s*(https?://[^\r\n]+)', text)
        if not match:
            match = re.search(r'<NextURL>\s*([^<]+)', text, re.I)
        if not match:
            return False
        target = urlsplit(html.unescape(match.group(1).strip()))
        return (target.scheme == 'http' and target.hostname == PORTAL
                and target.port in (None, 80)
                and parse_qs(target.query).get('wlanuserip') == [user_ip])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def logout():
    # Disable first: the daemon must be fully stopped before requesting logout.
    if not service_action('disable', '--now'):
        emit('无法停用自动连接服务，未发送注销请求；请使用 sudo 运行。')
        return 1
    emit('已停用自动连接和开机自启；正在退出校园网登录。')
    try:
        select_network()
        user_ip = SOURCE_IP
        if not online() and logout_redirect(user_ip):
            emit('当前已处于未认证状态，自动连接保持停用。')
            return 0
        query = urlencode({
            'callback': 'dr1006', 'login_method': '1',
            'user_account': 'drcom', 'user_password': '123',
            'ac_logout': '1', 'register_mode': '1',
            'wlan_user_ip': user_ip, 'wlan_user_ipv6': '',
            'wlan_user_mac': '000000000000', 'wlan_ac_ip': LOGOUT_AC_IP,
            'wlan_ac_name': '', 'wlan_vlan_id': '0', 'jsVersion': '4.1', 'lang': 'zh'
        })
        # drcom/123 are the portal protocol's logout placeholders, not saved credentials.
        request('http://' + PORTAL + ':801/eportal/portal/logout?' + query)
        for _ in range(3):
            time.sleep(2)
            if not online() and logout_redirect(user_ip):
                emit('退出登录成功（已重新出现本机认证跳转）。账号密码已保留，自动连接已停用。')
                return 0
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        pass
    emit('自动连接已停用，但尚未确认退出登录；需要在板卡上核对注销接口和实际返回。')
    return 1


def show_status():
    if systemd_available():
        emit('自动连接服务：' + ('运行中' if service_action('is-active', '--quiet') else '未运行'))
        emit('开机自动连接：' + ('已启用' if service_action('is-enabled', '--quiet') else '未启用'))
    else:
        emit('当前未运行 systemd，不提供开机服务状态。')
    try:
        select_network()
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        emit('无法识别访问校园网的 IPv4 网卡，请先连接网络。')
        return 1
    if online():
        emit('当前连接成功（外网 HTTP 检查通过）')
        show_ping_results()
    else:
        emit('当前外网 HTTP 检查未通过；可能未认证，也可能是网络或探测站点异常。')
    return 0


def menu():
    try:
        load_credentials()
    except (OSError, ValueError, AttributeError):
        if configure() != 0:
            return 1
        return start_connection()
    emit('GDUT 校园网管理')
    emit('1. 连接并启用开机自动连接')
    emit('2. 退出登录并停用开机自动连接')
    emit('3. 查看连接状态与 ping 延迟')
    emit('4. 修改保存的账号密码（下次认证使用）')
    emit('0. 返回终端，不改变连接状态')
    try:
        choice = input('请选择 [0-4]: ').strip()
    except (EOFError, KeyboardInterrupt):
        return 0
    actions = {'1': start_connection, '2': logout, '3': show_status, '4': configure, '0': lambda: 0}
    if choice not in actions:
        emit('无效选项，连接状态未改变。')
        return 1
    return actions[choice]()


def run_monitor(once=False):
    if not CONFIG.exists():
        emit('尚未配置账号；请在终端执行 sudo python3 /usr/local/sbin/gdut-login.py --setup')
    failures = 0
    was_online = False
    emit('正在连接 gdut')
    while True:
        ok, delay, message = tick()
        if ok:
            failures = 0
        if ok and not was_online:
            emit('连接成功（外网 HTTP 检查通过）')
            show_ping_results()
        elif not ok:
            if was_online:
                emit('正在连接 gdut（重新检查网络并等待认证）')
            if once:
                emit('连接失败（单次检查）：' + message)
                return 1
            failures += 1
            emit('第 {}/{} 次连接失败：{}'.format(failures, MAX_CONNECT_ATTEMPTS, message))
            if failures >= MAX_CONNECT_ATTEMPTS:
                emit('连接失败：连续 3 次尝试未成功，已停止自动重连。请选择“连接”或使用 --login 再试。')
                return 1
            emit('{} 秒后进行下一次尝试。'.format(delay))
        was_online = ok
        if once:
            return 0 if ok else 1
        time.sleep(delay)


def main():
    parser = argparse.ArgumentParser(description='GDUT 校园网自动连接与注销管理')
    group = parser.add_mutually_exclusive_group()
    for option, help_text in [
            ('daemon', '后台自动连接模式，供 systemd 使用'),
            ('once', '单次检查/认证并显示延迟'),
            ('setup', '输入或修改保存的账号密码'),
            ('login', '连接并恢复开机自动连接'),
            ('logout', '退出登录，并停用开机自动连接'),
            ('status', '查看状态与 ping 延迟')]:
        group.add_argument('--' + option, action='store_const', const=option, dest='mode', help=help_text)
    mode = parser.parse_args().mode
    if not check_environment(mode):
        return 1
    if mode == 'setup':
        return configure()
    if mode == 'login':
        return start_connection()
    if mode == 'logout':
        return logout()
    if mode == 'status':
        return show_status()
    if mode == 'once':
        if not CONFIG.exists() and sys.stdin.isatty() and configure() != 0:
            return 1
        return run_monitor(once=True)
    if mode == 'daemon':
        return run_monitor()
    if sys.stdin.isatty():
        return menu()
    # Compatibility with the previously installed unit that had no --daemon flag.
    return run_monitor()


if __name__ == '__main__':
    sys.exit(main())
