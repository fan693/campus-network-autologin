# 校园网自动重连

适用于 Ubuntu 和 Windows 的校园网监测、断线重连、门户自动认证与远程控制软件恢复工具。

## 为什么有这个脚本

校园网经常会出现两种“看起来都像断网”的情况：

1. Wi-Fi 信号短暂中断，电脑没有及时重新连接校园 Wi-Fi。
2. Wi-Fi 仍显示已连接，但校园网认证已经过期，打开网页又要输入账号密码。

这对需要长时间联网、远程连接、下载任务或无人值守电脑尤其麻烦。即使校园网已经恢复，ToDesk、向日葵等远程控制客户端有时仍停留在离线状态。这个项目因此同时处理两层问题：先恢复校园网认证，再刷新已经安装的远程控制客户端。

运行逻辑很简单：

```text
等待指定校园网 → 检查外网 → 掉线后登录门户 → 网络恢复 → 刷新远程控制客户端
```

认证失败时会逐步延长重试间隔，避免错误密码造成高频请求。

## 支持范围

| 系统或门户 | 支持情况 |
| --- | --- |
| Ubuntu | 20.04、22.04、24.04 |
| Windows | Windows 10、Windows 11 |
| 重庆大学 | 内置 CQU 一键配置 |
| Dr.COM / ePortal | 支持常见登录接口 |
| 深澜 SRUN | 支持 challenge 加密登录流程 |
| 其他简单门户 | 可配置 GET/POST 请求 |

远程控制软件恢复支持以下客户端：

| 客户端 | Ubuntu | Windows |
| --- | --- | --- |
| ToDesk | 支持 | 支持 |
| 向日葵 Sunlogin | 支持 | 支持 |
| AnyDesk | 支持 | 支持 |
| RustDesk | 支持 | 支持 |
| TeamViewer | 支持 | 支持 |

安装器会检查常见安装路径、Linux 桌面启动项和 Windows 卸载注册表。只为实际检测到的客户端启用恢复；没有安装上述软件时，不会创建远程恢复服务或计划任务。

不同学校的接口并不完全相同，因此除重庆大学外，首次使用可能需要填写学校的登录地址和固定参数。

## 下载

从 [Releases](https://github.com/fan693/campus-network-autologin/releases/latest) 下载：

- Ubuntu/Linux 建议下载 `tar.gz`。
- Windows 建议下载 `zip`。

下载后先解压，再进入解压目录安装。

## Ubuntu 使用方法

安装前先手动连接目标校园 Wi-Fi 或校园有线网络。

### 1. 安装

```bash
sudo bash install.sh
```

安装器会识别当前网络。根据向导输入校园网账号、密码并选择认证类型；重庆大学用户选择 `1`。

安装完成后会创建 `campus-autologin.service`，立即运行并开机自启。如果安装器检测到支持的远程控制软件，还会为当前桌面用户创建 `campus-remote-recovery.service`。

### 2. 查看运行状态

```bash
sudo systemctl status campus-autologin --no-pager
sudo journalctl -u campus-autologin -n 50 --no-pager
systemctl --user status campus-remote-recovery --no-pager
journalctl --user -u campus-remote-recovery -n 50 --no-pager
```

### 3. 修改配置或卸载

```bash
sudo bash configure.sh
sudo bash uninstall.sh
```

旧版 CQU v3 配置会在安装时自动识别并导入。

## Windows 使用方法

先安装 Python 3.8 或更高版本，并勾选 `Add Python to PATH`。然后在解压目录打开 PowerShell。

### 1. 安装

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

根据向导输入账号、密码并选择认证类型。安装不要求管理员权限。

程序会注册计划任务 `CampusNetworkAutoLogin`，当前用户登录 Windows 后自动运行。如果检测到支持的远程控制软件，还会注册 `CampusRemoteRecovery`。

### 2. 查看日志或立即测试

```powershell
Get-Content "$env:APPDATA\CampusAutoLogin\campus-autologin.log" -Tail 50
Get-Content "$env:APPDATA\CampusAutoLogin\remote-recovery.log" -Tail 50
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-once.ps1
```

### 3. 修改配置或卸载

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

## 认证类型怎么选

| 向导选项 | 适用情况 | 需要填写 |
| --- | --- | --- |
| `1` | 重庆大学 CQU | 账号、密码 |
| `2` | Dr.COM / ePortal | 登录 API URL，通常以 `/eportal/portal/login` 结尾 |
| `3` | 深澜 SRUN | 认证服务器地址和 `ac_id` |
| `4` | 简单 GET/POST 门户 | URL、请求字段和成功标志 |

账号需要运营商后缀时，直接输入完整账号，例如 `20240001@cmcc`。

不知道学校使用哪一种时：

1. 保持校园 Wi-Fi 已连接，但先退出校园网认证。
2. 浏览器打开学校登录页，按 `F12`，在 `Network` 中保留日志。
3. 手动登录一次，查看登录请求 URL：出现 `eportal/portal/login` 通常是 Dr.COM；出现 `get_challenge` 和 `srun_portal` 通常是 SRUN。

不要公开真实密码、Cookie 或临时 token。配置示例位于 [`examples`](examples/) 目录。

## 密码与安全

- 按本项目需求，配置向导输入密码时会直接显示，并再次打印供确认。
- 自动登录必须保存密码。Linux 只允许 root 和专用服务账户读取；Windows 只授权当前用户读取。
- 日志会隐藏账号、密码、URL 编码密码和 Base64 密码。
- HTTPS 门户始终校验证书。若学校只提供 HTTP，密码在传输中不受 TLS 保护，程序会输出警告。
- 远程恢复模块只读取本机安装路径和进程名称，不读取 ToDesk、向日葵等客户端的账号、密码或设备验证码。
- 本工具只恢复正常校园网认证，不绕过终端数量、计费或访问控制。

## 常见问题

### 远程软件会在什么情况下重启

恢复模块会连续确认断网和联网，过滤单次探测失败。只有确认网络从离线恢复到在线后，才重新打开检测到的远程控制客户端；联网正常但客户端进程意外退出时，也会重新启动。两次恢复操作默认至少间隔 180 秒，避免网络抖动造成反复重启。

安装或服务刚启动时不会重启正在运行的远程客户端，因此不会因为重新安装本项目而主动断开当前远程会话。

### 已经安装远程软件但没有检测到

Ubuntu 先确认应用的 `.desktop` 启动项位于标准应用目录，或可执行文件位于常见安装路径。Windows 先确认软件出现在“已安装的应用”中。便携版或自定义安装目录可在项目中提交路径适配；恢复模块不会通过全盘扫描寻找程序。

### 一直显示 `waiting for network`

当前连接名称或网卡与安装时不同。重新运行 `configure.sh` 或 `configure.ps1`。

Ubuntu 可先查看活动连接：

```bash
nmcli -t -f NAME,TYPE,DEVICE connection show --active
```

### 登录被拒绝

检查账号后缀、密码、登录 URL 和 `ac_id`。然后执行单次测试并查看脱敏日志。

### 学校有验证码、短信或统一身份认证

验证码、MFA、复杂 SSO、802.1X 和学校修改过的专有协议目前不能自动处理。

## 项目说明

核心程序只使用 Python 标准库，不需要安装 `requests` 等第三方包。协议参考、许可与项目边界见 [`NOTICE.md`](NOTICE.md)。

本地验证命令：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile campus_autologin.py configure.py remote_recovery.py
bash -n install.sh configure.sh uninstall.sh
```
