# 校园网自动重连 v4

一个不依赖第三方 Python 包的跨平台校园网监测与自动认证工具。

## 支持范围

| 项目 | 支持情况 |
| --- | --- |
| Ubuntu | 20.04、22.04、24.04（systemd + NetworkManager） |
| Windows | Windows 10、Windows 11（登录触发的计划任务） |
| 重庆大学 | 内置 CQU Dr.COM 一键预设 |
| 其他 Dr.COM / ePortal 学校 | 配置登录 API 后使用 |
| 深澜 SRUN 学校 | 支持 `get_challenge` + `srun_portal` 标准流程 |
| 其他简单 Web 门户 | 可配置 GET/POST、请求字段、请求头和成功标志 |

不同学校并不存在统一的校园网登录协议。本工具通过“协议适配器 + 学校参数”扩展适用范围，而不是声称一个固定请求可以登录所有学校。验证码、短信/MFA、统一身份认证 SSO、802.1X 客户端、非标准加密插件目前不能用通用 HTTP 适配器自动处理。

## 主要改进

- CQU 地址和参数不再写死在监测逻辑中。
- Ubuntu 24.04 与原有 20.04/22.04 使用同一套标准库实现。
- 同一个 `campus_autologin.py` 可在 Linux 和 Windows 运行。
- 支持 Wi-Fi 和有线 NetworkManager 连接，并只在指定网络上提交账号。
- 使用“精确 204 / 精确响应正文”检测外网，不会把校园门户的 200/302 页面误判为联网。
- 连续失败确认、指数退避、TLS 证书校验、低权限服务和日志脱敏继续保留。
- 可直接读取 v3 的 `/etc/cqu-autologin/config.json` 并导入 CQU 配置。
- 按需求，交互向导输入密码时会明文显示，并额外打印一次供确认。

## Ubuntu 安装

安装前先连接目标校园 Wi-Fi 或校园有线网络，然后进入解压目录执行：

```bash
sudo bash install.sh
```

配置向导中，重庆大学用户选择 `1`。安装器会启用当前 Wi-Fi 配置的 NetworkManager 自动连接，并注册 `campus-autologin.service`。

查看状态和日志：

```bash
sudo systemctl status campus-autologin --no-pager
sudo journalctl -u campus-autologin -n 50 --no-pager
```

重新配置账号、学校或门户：

```bash
sudo bash configure.sh
```

立即执行一次外网检查；掉线时跳过连续失败等待并立刻认证：

```bash
sudo systemctl stop campus-autologin
sudo -u campus-autologin /usr/bin/python3 \
  /usr/local/lib/campus-autologin/campus_autologin.py \
  --config /etc/campus-autologin/config.json --once
sudo systemctl start campus-autologin
```

卸载：

```bash
sudo bash uninstall.sh
```

## Windows 安装

要求 Python 3.8 或更高版本。安装 Python 时勾选 `Add Python to PATH`。在解压目录打开 PowerShell，执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

不要求管理员权限。程序安装在 `%LOCALAPPDATA%\CampusAutoLogin`，配置和日志位于 `%APPDATA%\CampusAutoLogin`。计划任务 `CampusNetworkAutoLogin` 会在当前用户登录 Windows 后启动，并持续监测指定网络。

重新配置和单次测试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\configure.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\test-once.ps1
```

查看任务与日志：

```powershell
Get-ScheduledTask -TaskName CampusNetworkAutoLogin
Get-Content "$env:APPDATA\CampusAutoLogin\campus-autologin.log" -Tail 50
```

卸载：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\uninstall.ps1
```

## 其他学校怎么选

### Dr.COM / ePortal

登录请求通常包含 `user_account`、`user_password`、`wlan_user_ip`，URL 常以 `/eportal/portal/login` 结尾。向导选择 `2`，输入完整登录 API URL。多数版本的账号前缀是 `,0,`；如果门户直接接收账号，前缀输入 `-`。

如果运营商由账号后缀区分，请把后缀直接写在账号中，例如 `20240001@cmcc`，不要写入脚本。

### 深澜 SRUN

门户通常会请求 `/cgi-bin/get_challenge`，随后请求 `/cgi-bin/srun_portal`。向导选择 `3`，输入认证服务器 base URL（例如 `http://10.0.0.55`）和登录页中的 `ac_id`。

本实现包含 HMAC-MD5、SRBX1/XXTEA、定制 Base64 和 SHA-1 校验流程。不同部署可能修改 Base64 字母表、API 路径、`n`、`type` 或 `enc_ver`；向导允许修改常用参数，示例见 `examples/srun.json`。

### 通用 GET/POST

向导选择 `4`。从学校登录请求中填写：

- 登录 URL 和 GET/POST 方法；
- 请求参数 JSON；
- 可选请求头 JSON；
- 登录成功时响应正文中确定存在的字符串。

可用占位符为 `{username}`、`{password}`、`{password_base64}`、`{ipv4}`、`{ipv6}`、`{network_name}`、`{timestamp}`。例如：

```json
{
  "action": "login",
  "username": "{username}",
  "password": "{password}",
  "user_ip": "{ipv4}"
}
```

完整示例见 `examples/generic-post.json`。

## 如何确认学校的认证类型

1. 先断开门户认证，但保持校园 Wi-Fi/网线已连接。
2. 浏览器打开学校官方登录页，按 `F12` 进入“网络/Network”，勾选保留日志。
3. 手动登录一次，查看包含账号字段的请求名称、URL、方法、参数和响应。
4. 只记录字段名、固定参数和成功响应标志；不要把真实密码、Cookie 或 token 发给别人。
5. 优先选择 Dr.COM 或 SRUN 适配器，只有简单表单才使用通用适配器。

门户参数升级后，学校可能改变接口。此时先用 `--once` / `test-once.ps1` 查看脱敏日志，再重新核对浏览器中的官方登录请求。

## 密码与安全

自动登录必须能读取凭据，因此密码会明文保存在本机配置文件中。Linux 文件权限为 `640`，仅 root 和无登录权限的 `campus-autologin` 服务账户可读；Windows 安装器会移除继承权限，只授予当前 Windows 用户。

按本版本需求，配置向导不会隐藏密码。终端输入和“你刚才输入的密码”都清晰可见，但不会进入命令历史。请避免旁观、投屏和录屏环境。运行日志仍会脱敏账号、密码、URL 编码密码和 Base64 密码。

HTTPS 门户始终校验证书，程序没有“忽略证书”选项。很多学校的旧门户只有 HTTP，此时密码在传输途中没有 TLS 保护，程序会明确写入警告；应优先向学校确认 HTTPS 地址。

本工具只恢复本机正常账号认证，不绕过终端数量、计费、访问控制或学校网络规定。

## 故障排查

### 一直提示 waiting for network

Ubuntu 查看活动连接：

```bash
nmcli -t -f NAME,TYPE,DEVICE connection show --active
```

Windows 查看当前网络配置：

```powershell
Get-NetConnectionProfile | Format-Table Name,InterfaceAlias,IPv4Connectivity
```

名称或网卡发生变化后重新运行配置向导。

### 登录被拒绝

先确认账号后缀、`ac_id`、登录 URL、请求方法和成功标志。通用适配器必须使用响应中稳定且只代表成功的字符串，不能使用网页标题或通用的 `200 OK`。

### SRUN challenge 失败

确认 base URL 能在未认证状态访问，并且浏览器确实请求 `get_challenge`。如果登录页加载的 JavaScript 给出了不同的 64 字符 Base64 字母表，在向导中替换默认值。

### TLS 校验失败

Ubuntu 检查时间和 CA：

```bash
timedatectl status
sudo apt update
sudo apt install --reinstall ca-certificates
```

Windows 确认系统时间正确并安装学校要求的受信任证书。不要把 `https://` 改成来源不明的 `http://` 来规避错误。

## 开发验证

```bash
python3 -m py_compile campus_autologin.py configure.py
python3 -m unittest discover -s tests -v
bash -n install.sh configure.sh uninstall.sh
```

协议和参考来源见 `NOTICE.md`。
