# 校园网自动重连

适用于 Ubuntu 和 Windows 的校园网监测、断线重连与门户自动认证工具。

## 为什么有这个脚本

校园网经常会出现两种“看起来都像断网”的情况：

1. Wi-Fi 信号短暂中断，电脑没有及时重新连接校园 Wi-Fi。
2. Wi-Fi 仍显示已连接，但校园网认证已经过期，打开网页又要输入账号密码。

这对需要长时间联网、远程连接、下载任务或无人值守电脑尤其麻烦。这个脚本因此而创建：它在后台检查指定校园网，发现外网不可用时自动重新认证，尽量减少反复打开登录页的操作。

运行逻辑很简单：

```text
等待指定校园网 → 检查外网 → 掉线后登录门户 → 成功后继续监测
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

安装完成后会创建 `campus-autologin.service`，立即运行并开机自启。

### 2. 查看运行状态

```bash
sudo systemctl status campus-autologin --no-pager
sudo journalctl -u campus-autologin -n 50 --no-pager
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

程序会注册计划任务 `CampusNetworkAutoLogin`，当前用户登录 Windows 后自动运行。

### 2. 查看日志或立即测试

```powershell
Get-Content "$env:APPDATA\CampusAutoLogin\campus-autologin.log" -Tail 50
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
- 本工具只恢复正常校园网认证，不绕过终端数量、计费或访问控制。

## 常见问题

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
python3 -m py_compile campus_autologin.py configure.py
bash -n install.sh configure.sh uninstall.sh
```
