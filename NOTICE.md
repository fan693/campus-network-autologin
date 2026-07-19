# 参考、许可与边界

本安装包不包含用户账号、密码、Cookie 或校园网会话数据。

## 原 CQU 思路

- 无一可期（NCUcxk），《重庆大学校园网开机自动连接Python脚本》，2024-04-11：<https://blog.csdn.net/NCUcxk/article/details/137638931>
- 文章标注采用 CC BY-SA 4.0：<https://creativecommons.org/licenses/by-sa/4.0/>
- AutoLogin-CQU：<https://github.com/nagelanping/AutoLogin-CQU>（MIT License）

上述文章说明了在浏览器开发者工具中定位 CQU 登录 URL、动态替换 `wlan_user_ip`，并在 Windows 打包为可执行文件/加入启动目录的原始思路。v4 没有保存一次性抓取的完整凭据 URL，而是将 CQU 参数作为 Dr.COM 配置生成，并增加准确联网检测、TLS 校验、失败退避、权限隔离、Windows 计划任务和跨学校适配器。

## SRUN 协议核对

- coffeehat/BIT-srun-login-script：<https://github.com/coffeehat/BIT-srun-login-script>（MIT License）
- xiaoxin-tools/srun-campus-login：<https://github.com/xiaoxin-tools/srun-campus-login>（MIT License）

SRUN 的 challenge、HMAC-MD5、SRBX1/XXTEA、定制 Base64 和 SHA-1 字段组合依据这些公开实现及登录页协议行为重新实现，并仅用于与用户有权使用的校园认证服务互操作。

`coffeehat/BIT-srun-login-script` 的 MIT 许可文本如下：

> MIT License
>
> Copyright (c) 2020 coffeehat
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## 系统集成资料

- Microsoft `Register-ScheduledTask`：<https://learn.microsoft.com/powershell/module/scheduledtasks/register-scheduledtask>
- NetworkManager `nm-settings-nmcli(5)`：<https://networkmanager.dev/docs/api/latest/nm-settings-nmcli.5.html>
- Python `urllib.request`：<https://docs.python.org/3/library/urllib.request.html>

不同学校的门户参数由学校自行部署并可能随时变化。本项目不附带学校账号、不探测私有系统，也不保证验证码、MFA、SSO、802.1X 或修改过的专有协议可用。
