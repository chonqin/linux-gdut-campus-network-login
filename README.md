# linux终端下解决广工GDUT校园网连接问题

用于 Linux 设备的广工校园网登录，自动识别网卡和 IPv4 地址。已在鲁班猫 5 / Debian 11 验证过基础登录；设备需先连接校园网。

## 第一次使用

**1. 准备环境。**需要 Python 3.6+、curl、完整 iproute2；ping 使用 iputils。不需要 pip 库。以下命令适用于 Debian/Ubuntu，其他发行版使用对应的包管理器安装。

```bash
sudo apt update
sudo apt install -y python3 curl iproute2 iputils-ping
```

**2. 把 `gdut-login.py` 复制到板卡，在文件所在目录运行：**

```bash
sudo python3 gdut-login.py
```

按提示输入校园网账号和密码即可。脚本自动保存配置、准备服务并启用开机连接，**不用手动创建 JSON，也不用手动安装服务文件**。已有账号配置时会显示菜单，选 `1` 连接。

脚本会检查依赖和 systemd。没有 systemd 时，用 `sudo python3 gdut-login.py --once` 单次连接；开机自启暂不自动配置。不兼容的 ping 会跳过，不影响认证。

## 以后怎么用

再次运行 `sudo python3 gdut-login.py`，从菜单选择连接、退出、查看状态或修改账号。

也可在命令后加一个选项，例如 `sudo python3 gdut-login.py --logout`：

| 选项 | 作用 |
| --- | --- |
| 不带选项 | 终端中显示菜单；无配置时先输入账号密码 |
| `--setup` | 保存或修改账号密码，不立即切换当前在线账号 |
| `--login` | 启动连接并启用开机自启 |
| `--logout` | 注销并停用开机自启，保留账号密码 |
| `--status` | 查看状态及四个网站的 ping 延迟，完成后退出 |
| `--once` | 单次检查/认证 |
| `--daemon` | 持续监控，供 systemd 或手动前台运行 |
| `-h` / `--help` | 查看帮助 |

**退出登录会同时取消开机自动连接**，选择菜单中的连接或使用 `--login` 可以恢复。

## 原理和文件作用

| 文件 | 作用 |
| --- | --- |
| `gdut-login.py` | 执行登录、注销和联网检查；运行副本自动放到 `/usr/local/sbin/` |
| `credentials.json` | 自动生成在 `/etc/gdut-login/`，保存输入的账号密码 |
| `gdut-login.service` | 自动生成在 `/etc/systemd/system/`，负责开机启动和后台运行 |

脚本按访问认证服务器的路由选择网卡和源 IP，地址变化后自动重新识别。需要认证时模拟网页登录请求。连接成功后显示延迟，每分钟检查一次；连续 3 次失败就停止，用菜单“连接”或 `--login` 可重新开始。

延迟测试包含百度、哔哩哔哩、腾讯、阿里云，每站发送 3 个包，单站最多等待 12 秒。测试结束后退出 ping；`--status`、`--once` 返回终端，后台模式继续检查网络。

## 当前不足

- 仅支持 IPv4；非 systemd 系统的开机服务需要自行配置，精简版 BusyBox ip 不兼容。
- 注销接口尚未在广工网关实测；未确认退出时会提示，自动连接仍保持停用。
- 在线判断依赖百度 HTTP 页面；页面变化可能影响检测，ping 无回应也不代表断网。
- 账号明文保存在仅 root 可读的配置文件中，认证使用 HTTP。
