# 校园网连接助手桌面软件设计

状态：设计冻结，可按阶段 A-D 开发
目标版本：v5.0.0
适用平台：Ubuntu 20.04/22.04/24.04、Windows 10/11

## 1. 目标

把现有命令行脚本包装成普通用户容易理解的小软件，同时保留已经验证过的后台自动恢复能力。

软件需要做到：

- 后台服务不依赖图形界面，退出托盘程序后仍能自动认证和恢复网络。
- 从托盘或主窗口查看网络、校园网认证、远程控制软件和版本状态。
- GitHub 发布新版本后，每天检查一次并显示桌面通知。
- 默认只提示更新，不在无人值守时自动替换服务或重启网络。
- 不向 GitHub 或其他服务器上传校园网账号、密码、设备标识、日志或网络名称。
- Ubuntu 和 Windows 使用同一套产品交互，平台相关能力由适配层实现。

本项目仍只恢复正常网络连接和门户认证，不绕过终端数量、计费、验证码、MFA、802.1X 或学校访问控制。本文中的“必须”“不得”“应当”是实现和验收要求，不是建议。

## 2. 产品边界

### 2.1 v5.0 必须包含

- 系统托盘图标与主状态窗口。
- 当前连接、互联网、校园网认证服务和远程软件恢复服务状态。
- “立即检测”“重新认证”“打开日志”“检查更新”操作。
- 首次配置和修改配置入口。
- GitHub Release 更新检查、版本比较、忽略当前版本和打开下载页。
- Ubuntu `.deb` 与 Windows 安装包。
- 升级时保留现有配置，卸载时由用户选择是否删除配置。
- 继续支持当前已有的 ToDesk、向日葵、AnyDesk、RustDesk 和 TeamViewer 检测与恢复。

v5.0 首批二进制只支持 Ubuntu `amd64` 和 Windows `x64`。其他 CPU 架构可以从源码运行，但不属于 v5.0 发布验收范围。

### 2.2 v5.0 不包含

- 后台静默自动升级。
- 软件内下载并自动执行未知安装包。
- 云端账号、遥测、崩溃日志上传或设备绑定。
- 在界面中展示已保存的校园网密码。
- 替代 ToDesk、向日葵、AnyDesk、RustDesk 或 TeamViewer 本身。

自动升级可以在后续版本中作为明确的自愿选项加入，但必须先完成包签名、校验和回滚。

## 3. 核心决策

| 项目 | 决定 | 原因 |
| --- | --- | --- |
| UI 技术 | Python + PySide6 | 与现有 Python 核心复用代码，Ubuntu/Windows 都支持托盘、窗口和通知 |
| 后台架构 | 保留独立系统服务 | GUI 退出、崩溃或用户注销时不影响自动认证 |
| 更新来源 | GitHub Releases API | 有正式版本、发布日期和下载页，不需要 API key |
| 更新策略 | 默认仅提醒 | 避免远程机器因升级失败、重启服务或网络切换而失联 |
| 权限模型 | 状态只读；危险操作逐次提权 | GUI 永远不以 root/管理员身份常驻 |
| 配置存储 | 沿用现有受保护配置 | 不迁移或复制密码到用户目录 |
| 日志展示 | 读取脱敏日志的有限尾部 | 避免界面接触完整历史和凭据 |

PySide6 会让安装包大于纯脚本，预计压缩后约 70-120 MB；这是换取稳定托盘、跨平台界面和可打包安装体验的明确取舍。后台认证服务继续只使用 Python 标准库，不依赖 GUI 组件。

## 4. 总体架构

```text
GitHub Releases
      |
      | HTTPS：版本号、更新说明、下载页（每天最多一次）
      v
桌面助手（普通用户进程）
  |- 托盘图标与状态窗口
  |- 更新检查与桌面通知
  |- 只读状态适配器
  `- 受控操作入口
          |
          | 固定子命令；需要时弹出系统提权确认
          v
平台控制层（campusctl + 本地控制服务）
  |- 返回规范化状态与脱敏日志
  |- 立即检测/重新认证
  |- 重启后台服务
  `- 校验并保存受保护配置
          |
          +-----------------------------+
          v                             v
校园网后台服务                  远程软件恢复用户服务
  |- 外网检测                     |- 客户端健康检查
  |- 门户认证                     |- GUI/后台服务分级恢复
  `- 持续断网后的网络重连          `- 冷却与防抖
```

### 4.1 进程与权限

| 组件 | Ubuntu 身份 | Windows 身份 | 可访问内容 |
| --- | --- | --- | --- |
| `campus-autologin` | 专用无登录服务账户 | 当前用户计划任务 | 受保护配置、网络检测和门户认证 |
| `campus-remote-recovery` | 当前桌面用户 | 当前用户计划任务 | 已安装远程软件的进程/服务状态 |
| `campus-desktop` | 当前桌面用户 | 当前桌面用户 | 脱敏状态、用户偏好、GitHub Release 元数据 |
| `campus-control` | root 的最小系统服务 | 不单独常驻 | 状态读取、固定控制方法和 polkit 授权 |
| `campusctl` | 当前用户；由控制服务决定授权 | 当前用户 | 将固定命令转换为本地 IPC，请求中不包含 shell 文本 |

Ubuntu GUI 不直接读取 `/etc/campus-autologin/config.json`，也不继承后台服务权限。`campus-control` 通过 system D-Bus 暴露固定方法，并用独立 polkit action 控制每种操作。Windows v5.0 明确采用 **per-user 安装**：配置、计划任务、GUI 和更新缓存都属于安装用户，不因 UAC 管理员身份重新解析 `%APPDATA%`。只有重启需要管理员权限的第三方远程软件服务时，才启动带固定服务名的提升助手；助手同时接收安装时记录的原始用户 SID，禁止使用提升账户的用户目录。

不得提供“执行任意命令”接口，不得把用户输入拼接到 shell、PowerShell 或 `systemctl` 字符串。Ubuntu 自动认证系统服务在用户注销后继续运行；Windows v5.0 只保证安装用户保持登录时运行，注销后的无人值守运行不属于本版本承诺。

## 5. 用户体验

### 5.1 托盘状态

| 颜色 | 含义 | 托盘提示 |
| --- | --- | --- |
| 绿色 | 公网与后台服务正常 | 校园网已连接 |
| 黄色 | 正在确认、认证或恢复 | 正在恢复连接 |
| 红色 | 持续离线或服务停止 | 连接需要处理 |
| 灰色 | 尚未配置或状态未知 | 尚未完成配置 |

托盘菜单固定为：

1. 打开状态窗口
2. 立即检测
3. 重新认证
4. 检查更新
5. 打开日志
6. 设置
7. 退出界面

“退出界面”旁边注明后台自动恢复继续运行，但不在菜单中堆叠使用说明。

### 5.2 主窗口

```text
+--------------------------------------------------+
| 校园网连接助手                         v5.0.0    |
+--------------------------------------------------+
| 网络连接       有线连接 / eno1          已连接   |
| 互联网         最近检测 10:32:18        正常     |
| 校园网认证     后台服务运行中            正常     |
| 远程控制       ToDesk                   在线     |
+--------------------------------------------------+
| [立即检测] [重新认证] [查看日志] [设置]          |
+--------------------------------------------------+
| 新版本 v5.1.0 可用                  [查看更新]    |
+--------------------------------------------------+
```

窗口只展示连接名称、接口名称和服务状态，不展示 IP、校园网账号、密码、Cookie、Token 或远程软件设备码。远程软件未安装时整行隐藏，不显示错误。

### 5.3 通知规则

- 新版本：每个版本默认只主动提醒一次；只有用户点“稍后提醒”才会在 24 小时后再提醒该版本。
- 网络恢复成功：只在离线持续超过 60 秒后恢复时提醒。
- 恢复失败：同类错误 30 分钟内最多提醒一次。
- 服务刚启动、单次探测失败和正常周期检查不通知。
- 勿扰模式下只更新托盘颜色，不弹窗。

## 6. 状态接口

后台服务写入不含秘密的原子状态快照，GUI 通过控制层读取，不直接解析 journal 或受保护文件。

Ubuntu 路径：`/run/campus-autologin/status.json`
Windows 路径：`%LOCALAPPDATA%\CampusAutoLogin\status.json`

以下是 v5.0 的强制 schema：

```json
{
  "schema_version": 1,
  "instance_id": "9b1f0a5d-15b4-4cab-9f04-0bbcecd4e8e1",
  "sequence": 42,
  "updated_at": "2026-08-03T10:32:18+08:00",
  "service": "running",
  "network": "connected",
  "internet": "online",
  "authentication": "online",
  "recovery": "idle",
  "connection_type": "ethernet",
  "connection_name": "Campus LAN",
  "interface": "eno1",
  "offline_seconds": 0,
  "cooldown_remaining": 0,
  "last_error_code": null
}
```

必填字段、类型和枚举：

| 字段 | 类型 | 合法值/约束 |
| --- | --- | --- |
| `schema_version` | integer | v5.0 只能写 `1` |
| `instance_id` | string | 写入进程每次启动随机生成的 UUID，不落盘、不是设备 ID |
| `sequence` | integer | 同一 instance 内每次写入都递增，最小为 `0` |
| `updated_at` | string | RFC 3339，必须包含时区；仅供显示 |
| `service` | string | `not_configured/starting/running/stopping/stopped/failed` |
| `network` | string | `connected/disconnected/waiting/unknown` |
| `internet` | string | `online/offline/checking/unknown` |
| `authentication` | string | `online/required/authenticating/failed/unknown` |
| `recovery` | string | `idle/waiting/reconnecting/cooldown/failed` |
| `connection_type` | string | `ethernet/wifi/unknown` |
| `connection_name` | string/null | 最长 255 个 Unicode 字符；仅本机返回 |
| `interface` | string/null | 最长 64 个字符，不允许控制字符 |
| `offline_seconds` | integer | 非负，未知时为 `0` 且 `internet=unknown` |
| `cooldown_remaining` | integer | 非负秒数 |
| `last_error_code` | string/null | 只能使用第 6.3 节定义的稳定错误码 |

缺少必填字段、类型错误或未知枚举会使整个快照无效。读取端必须忽略未知字段；`schema_version > 1` 时显示“软件版本过旧，无法读取状态”，但仍查询服务管理器显示进程是否存在。`schema_version < 1` 不支持。

### 6.1 远程软件状态

远程恢复服务写入用户私有的 `remote-status.json`：Ubuntu 位于 `~/.local/state/campus-autologin/`，Windows 与主状态文件同目录。强制结构如下：

```json
{
  "schema_version": 1,
  "instance_id": "9b1f0a5d-15b4-4cab-9f04-0bbcecd4e8e1",
  "sequence": 17,
  "updated_at": "2026-08-03T10:32:18+08:00",
  "monitor": "running",
  "apps": [
    {
      "key": "todesk",
      "display_name": "ToDesk",
      "process": "running",
      "background_service": "running",
      "connection": "online",
      "recovery": "idle",
      "cooldown_remaining": 0,
      "last_error_code": null
    }
  ]
}
```

`monitor` 必填并使用 `running/stopped/not_needed/failed/unknown`。`key` 只能是 `todesk/sunlogin/anydesk/rustdesk/teamviewer`；数组只包含检测到的软件。`process` 和 `background_service` 使用 `running/stopped/unknown/not_applicable`，`connection` 使用 `online/offline/unknown`，`recovery` 使用 `idle/waiting/restarting/cooldown/failed`。未来增加客户端必须新增 key，不得改变已有 key 的含义。

v5.0 沿用并必须回归验证现有探针：ToDesk Linux 检查进程、后台服务、本地控制端口、中心认证日志和当前 TCP 连接；AnyDesk 优先使用官方状态命令；向日葵、RustDesk 和 TeamViewer 检查进程与已安装后台服务，并在公网恢复后刷新。没有可靠在线探针时必须返回 `connection=unknown`，不能把“进程存在”伪装成在线。

### 6.2 写入、过期和状态合成

- Ubuntu 主快照由 `RuntimeDirectory=campus-autologin` 创建的目录保存，目录 `0750 campus-autologin:campus-autologin`，文件 `0640 campus-autologin:campus-autologin`。root 控制服务可以读取，其他本机用户不能直接读取连接名称。
- 临时文件必须在同一目录以 `O_CREAT|O_EXCL|O_NOFOLLOW` 和最终 mode 创建，写完后 `fsync` 文件、原子替换并 `fsync` 目录；替换后校验 owner、group 和 mode。
- Windows 文件使用安装用户 SID 的 ACL，禁止其他普通用户访问。
- 主服务和远程恢复服务即使状态没有变化，也必须至少每 30 秒写一次心跳并递增 `sequence`；每次进程启动生成新的 `instance_id` 并从 sequence `0` 开始。写入失败按 5、15、30 秒退避，但不得刷屏记录同一错误。
- GUI 以 `(instance_id, sequence)` 是否变化为依据，用本地单调时钟记录最后一次变化；连续 90 秒未变化即为 `stale`。首次读取时，`updated_at` 比当前墙上时钟早 90 秒以上或晚 5 分钟以上也视为 `stale`。
- 新鲜快照是业务状态的主来源；服务管理器只判断进程存活。服务管理器报告 stopped/failed 时覆盖快照并显示红色；进程 running 但快照 stale 时显示黄色“服务运行但状态未更新”。
- 远程恢复 unit/任务为 stopped/failed 时，控制层必须忽略旧快照中的 `connection=online`，合成 `monitor=stopped/failed`，把已知客户端的连接改为 `unknown` 并使用 `remote_service_stopped`；若安装时确认没有受支持客户端，则合成 `monitor=not_needed`，不显示故障。
- `network` 专指安装时选中的物理校园网连接，不把 VPN、Docker 或备用非校园网络计为已连接。

总状态颜色按以下顺序计算，先匹配者优先：

1. 服务 stopped/failed，或已确认远程客户端离线且恢复失败：红色。
2. 配置缺失、schema 不支持且服务状态也未知：灰色。
3. 快照 stale、任一业务字段 unknown、正在检测/认证/恢复/冷却：黄色。
4. 主服务 running、目标网络 connected、互联网和认证 online，且所有已检测远程客户端 online：绿色。
5. 其他组合：黄色，不允许凭猜测显示绿色。

### 6.3 稳定错误码

v5.0 只允许以下错误码进入状态、IPC 和 UI：

```text
ok invalid_request schema_unsupported operation_conflict operation_cancelled
config_missing config_invalid network_missing ipv4_missing
internet_unreachable portal_rejected portal_unreachable dns_failed
network_recovery_failed service_stopped remote_process_stopped
remote_service_stopped remote_connection_offline permission_denied
operation_timeout update_unreachable update_invalid_response internal_error
```

原始异常、门户响应、URL 查询、命令 stderr 和日志文本不得作为错误码或状态字段。UI 根据错误码映射本地化说明。

## 7. 受控操作接口

`campusctl` 仅接受以下固定子命令：

```text
campusctl status --json
campusctl logs --limit 200 --json
campusctl check-now
campusctl authenticate-now
campusctl automation on|off
campusctl operation <operation_id> --json
campusctl restart-service
campusctl enrollment-state --json
campusctl enroll --stdin-json
campusctl get-settings --json
campusctl apply-config --stdin-json
campusctl remove-credentials
campusctl diagnostics --json
```

### 7.1 本地 IPC

Ubuntu 实现 system D-Bus 服务 `io.github.fan693.CampusNetwork1`，object path `/io/github/fan693/CampusNetwork1`。Windows 后台任务实现仅允许安装用户 SID 连接的 named pipe `\\.\pipe\CampusNetworkAutoLogin-<SID>`。两端暴露相同逻辑方法：

```text
GetStatus GetLogs CheckNow AuthenticateNow SetAutomationEnabled GetOperation RestartService
GetEnrollmentState Enroll GetSettings ApplyConfig RemoveCredentials GetDiagnostics
```

- Ubuntu 所有 D-Bus 方法统一使用 signature `Method(s request_json) -> (s response_json)`；Windows named pipe 每次连接只处理一个请求，帧格式为 4 字节网络字节序无符号长度加 UTF-8 JSON，响应使用相同格式。
- 请求顶层固定为 `{"schema_version":1,"request_id":"UUID","data":{...}}`。单个请求最大 64 KiB，日志或诊断响应最大 1 MiB；JSON 必须是对象，禁止重复键、NaN/Infinity 和控制字符，超过限制或 schema 不符返回 `invalid_request`。
- 所有请求带随机 `request_id`，响应回显该值；named pipe ACL 只允许安装用户 SID 和 SYSTEM，D-Bus 由 polkit 与记录的桌面 UID 双重校验。
- `CheckNow` 和 `AuthenticateNow` 的并发请求合并为一次操作；重复请求返回同一 operation id。
- `CheckNow` 只立即执行一次检测，不认证、不重连物理连接。
- `AuthenticateNow` 立即检测并在需要时认证，但不得跳过网络恢复冷却。
- Ubuntu 控制服务通过固定 D-Bus/系统服务接口唤醒后台，不接受 signal 编号、unit 名或命令文本作为调用参数。

逐方法 `data` 契约：

| 方法 | 请求 `data` | 成功响应 `data` |
| --- | --- | --- |
| `GetStatus` | `{}` | `{"main":<第6节主状态>,"remote":<第6.1节状态>,"overall":"green/yellow/red/gray"}` |
| `GetLogs` | `{"limit":1..500}` | `{"entries":[{"timestamp":"RFC3339","level":"info/warn/error","code":"稳定错误码或null","message_key":"本地化键"}]}` |
| `CheckNow` | `{}` | operation 对象 |
| `AuthenticateNow` | `{}` | operation 对象 |
| `SetAutomationEnabled` | `{"enabled":boolean}` | operation 对象；同时控制校园认证与已安装的远程恢复服务，GUI 自身保持运行 |
| `GetOperation` | `{"operation_id":"UUID"}` | operation 对象 |
| `RestartService` | `{}` | operation 对象 |
| `GetEnrollmentState` | `{}` | `{"state":"unbound/bound","desktop_uid":integer或null}`；Windows 返回当前 SID 是否匹配 |
| `Enroll` | `{"desktop_uid":integer,"config":<完整候选配置>,"update_check":boolean}` | operation 对象 |
| `GetSettings` | `{}` | `{"automation_enabled":boolean,"username":string,"network_name":string,"interface":string,"portal":object,"timers":object}`，无密码 |
| `ApplyConfig` | `{"config":object,"password_mode":"preserve/replace","password":string可选}` | operation 对象 |
| `RemoveCredentials` | `{"confirm":true}` | operation 对象 |
| `GetDiagnostics` | `{"log_limit":0..200}` | `{"version":string,"status":object,"logs":array}` |

operation 对象固定为：

```json
{
  "operation_id": "6a93d443-545a-4701-9fd7-f342308ce2a4",
  "kind": "authenticate_now",
  "state": "accepted",
  "started_at": "2026-08-03T10:35:00+08:00",
  "completed_at": null,
  "code": "ok"
}
```

控制层保留最多 128 条 operation，完成后保存 10 分钟；超出后最旧的已完成记录先删除。operation state 只能是 `accepted/running/completed/failed/cancelled`。GUI 必须用 `GetOperation` 跟踪本次请求，不能根据周期状态变化猜测完成。合并请求返回正在运行的相同 operation id。

Ubuntu polkit 权限固定为：

| 方法 | 权限 |
| --- | --- |
| `GetStatus/GetLogs/GetSettings/GetDiagnostics/GetOperation/GetEnrollmentState` | 已绑定的当前活动本地桌面用户，无密码；enrollment operation 在 10 分钟保留期内始终允许其发起 UID 按精确 operation id 查询，不受后来绑定状态影响 |
| `CheckNow/AuthenticateNow` | 当前活动本地桌面用户，无密码 |
| `RestartService/SetAutomationEnabled/ApplyConfig/RemoveCredentials/Enroll` | `auth_admin_keep` |

所有无需密码的 Ubuntu 方法仍必须核对 polkit subject UID 等于安装时记录的桌面用户 UID；其他活动本地用户不能读取状态或触发认证。`campus-control.service` 以 root 运行，但只包含 IPC、状态读取、固定 systemd 操作和配置原子写入，不包含 HTTP 客户端或门户协议，并启用 `NoNewPrivileges` 之外适用于 root D-Bus 服务的 systemd 文件系统、设备和地址族限制。

源码预览阶段允许使用安装在 `/usr/local/lib/campus-autologin/campus_control.py` 的 root 所有固定动作助手验证界面。GUI 只能通过 `pkexec /usr/bin/python3 <固定绝对路径> <固定动作>` 调用它，助手拒绝非 root 所有、组/其他用户可写或符号链接文件，数据只走有大小上限的 stdin JSON。正式 `.deb` 仍按本节迁移到 D-Bus 常驻控制服务，不以源码目录中的可写脚本执行提权操作。

Windows per-user 版本的上述操作都由安装用户执行；仅第三方系统服务重启使用现有的精确服务白名单和 UAC，不允许调用方传入任意服务名。

### 7.2 返回协议

`campusctl` 的 stdout 只能包含一个 UTF-8 JSON 对象：

```json
{
  "schema_version": 1,
  "request_id": "7a62773b-3507-4e18-8c26-aaea7956e3c6",
  "ok": true,
  "state": "completed",
  "code": "ok",
  "message_key": "operation.completed",
  "operation_id": null,
  "data": {}
}
```

`state` 只能是 `completed/accepted/running/failed/cancelled`。`message_key` 是本地化键，不包含服务原始输出。退出码固定为：

| 退出码 | 含义 |
| --- | --- |
| `0` | 已完成或已接受 |
| `2` | 命令或输入格式错误 |
| `3` | 控制服务不可用 |
| `4` | 权限拒绝或用户取消提权 |
| `5` | 操作超时 |
| `6` | 配置校验失败 |
| `7` | 操作冲突且不能合并 |
| `8` | 已脱敏的内部错误 |

同步读取 IPC 5 秒内没有响应时，CLI 才以退出码 `5` 结束。变更操作在 2 秒内返回 operation；未完成时正常返回 `accepted` 和退出码 `0`，GUI 通过 `GetOperation` 轮询。只有 operation 超过其硬期限（检测 30 秒、认证 120 秒、服务重启 60 秒、配置应用 90 秒）才进入 `failed` 且 code 为 `operation_timeout`。GUI 不分析 stderr 来判断结果。

### 7.3 配置和日志

- `GetSettings` 返回用户名、网络选择、门户类型、去除查询和 userinfo 的门户基础 URL、时间参数及更新偏好，永远不返回密码、Cookie 或 token。
- GUI 保存时把完整候选配置作为 D-Bus/named-pipe 消息发送，密码不得出现在 argv、环境变量、普通临时目录或日志。只有第 7.5 节定义的受保护事务备份可以短暂包含旧密码。密码留空表示保留；“删除已保存凭据”是独立危险操作，需要再次确认并停止认证服务。
- 总开关关闭时持久停止并禁用校园认证服务及当前用户已安装的远程恢复服务；GUI、状态查看和重新开启入口继续可用。开启时先启动校园认证，再启动远程恢复；任一操作失败必须显示部分失败状态并执行安全回滚，不能只改变界面开关。
- “重新认证”窗口同时提供“使用已保存配置重新检测”和“更换学校/认证方式/账号”两条路径。学校选择至少包括重庆大学预设、其他 Dr.COM/ePortal、深澜 SRUN 和通用 HTTP 门户，并只展开所选协议需要的字段。更换配置要求重新输入完整账号与密码，永远不回显旧密码；提交成功后重启认证检测，当前网络已经在线时不主动踢下现有会话，新配置在下次门户认证时生效。旧版 CQU 专用后台只允许保存 CQU 预设，选择其他协议必须先完成通用后台升级，禁止直接写入旧后台无法解析的配置。
- `ApplyConfig` 先调用与后台相同的 schema 校验并取得配置事务锁，再按第 7.5 节提交；任何失败都恢复旧配置。成功后重载服务，读取新状态确认可启动；不能启动时自动回滚并返回 `config_invalid`。
- `logs --limit` 的 limit 范围为 1-500，只返回应用自身最近日志的结构化时间、级别、错误码和脱敏消息；不得返回完整 journal 或第三方远程软件原始日志。
- `diagnostics --json` 返回版本、规范化状态和最多 200 条脱敏应用日志。GUI 先预览，再由普通用户进程写入用户选择的目标文件，因此提权进程不处理任意输出路径，也不存在符号链接/重解析点 TOCTOU。

GUI 使用参数数组启动命令，不通过 `shell=True`、PowerShell 字符串拼接或 `bash -c` 传递用户输入。

### 7.4 Ubuntu 首次绑定

新安装初始为 `unbound`。此时活动本地用户只能调用不含机器信息的 `GetEnrollmentState`、发起需要管理员认证的 `Enroll`，以及查询由同一 UID 发起的 enrollment operation；其他读取和控制方法均拒绝。operation 记录保存发起 UID，控制服务按 `(operation_id, initiator_uid)` 授权查询，任何用户都不能查询其他 UID 的 operation。enrollment operation 在完成后的 10 分钟保留期内继续允许发起 UID 查询，即使另一用户已经完成绑定；这样并发失败方能够取得 `operation_conflict` 终态，但不能读取新绑定用户的状态、配置或其他 operation。首次设置使用以下原子认领流程：

1. 活动本地用户打开向导并提交 `Enroll`，请求的 `desktop_uid` 必须等于 polkit subject UID；root CLI 可以显式指定非 root 用户。
2. `Enroll` 必须通过 `auth_admin_keep`，控制服务取得 `/etc/campus-autologin/enrollment.lock` 独占锁。
3. 锁内再次检查绑定状态；仍为 `unbound` 才校验配置并在同一事务写入配置和 `/etc/campus-autologin/desktop-user.json`。
4. 绑定文件为 `0600 root:root`，只保存 UID、用户名和绑定时间，不保存会话 token。
5. 启用对应用户服务并启动认证服务后，operation 才能进入 `completed`。

两个不同用户并发认领时只有第一个通过锁内 compare-and-set 的事务成功，第二个返回 `operation_conflict`，不得覆盖绑定。同一用户重复提交正在执行的请求返回相同 operation id。改变已绑定用户只能由 root 执行 `campus-network-assistant-setup --rebind --desktop-user <用户>`；该命令先停止旧用户服务、完成新绑定，再启用新用户服务，任一步失败都恢复旧绑定。

从 v4 升级时，只有受保护的 `remote-user` 记录能解析为现存非 root 用户且 home/UID 匹配，才自动迁移为绑定；否则保持 `unbound`，但原系统认证服务可以继续使用已有配置，不允许任意活动用户无确认抢占。

### 7.5 配置事务与秘密备份

Ubuntu 配置事务目录固定为 `/var/lib/campus-autologin/transactions/<operation_id>/`，目录 `0700 root:root`，旧配置副本 `0600 root:root`。文件以 `O_CREAT|O_EXCL|O_NOFOLLOW` 创建，不使用 `/tmp`，事务 marker 只记录阶段和文件哈希，不记录秘密。Windows 使用 `%LOCALAPPDATA%\CampusAutoLogin\.transactions\<operation_id>\`，关闭 ACL 继承，只授权原始安装用户 SID 和 SYSTEM。

事务顺序固定为：

1. 校验候选配置但不修改运行状态。
2. 创建受保护事务目录和旧配置副本，`fsync`/FlushFileBuffers。
3. 原子替换主配置并重载服务。
4. 等待服务在硬期限内写出 schema 有效且非 `config_invalid` 的新 instance 状态。
5. 成功则删除旧配置副本和事务目录；失败则原子恢复旧配置、恢复原 mode/owner/ACL、重启旧服务并删除事务目录。

控制服务/Windows 后台每次启动都扫描未完成 marker：新配置有效且服务已正常运行则清理；否则恢复旧配置。任何备份最多保留到下一次控制服务启动，不作为历史版本保存。文件删除不能保证底层存储安全擦除，因此设计目标是避免额外长期副本，而不是宣称物理抹除。

## 8. 更新提醒设计

### 8.1 版本来源

仓库增加根目录 `VERSION`，内容为严格 SemVer，例如 `5.0.0`。正式发布使用 `v5.0.0` 标签，并创建非草稿、非预发布的 GitHub Release。

更新检查请求：

```text
GET https://api.github.com/repos/fan693/campus-network-autologin/releases/latest
Accept: application/vnd.github+json
If-None-Match: <上次保存的 ETag>
User-Agent: campus-network-assistant/5.0.0
```

请求不附带 GitHub token。保存的内容仅包括：

- `last_auto_attempt_at`
- `last_success_at`
- `etag`
- `latest_version`
- `release_page`
- `ignored_version`
- `notified_version`
- `snoozed_version`
- `snoozed_until`

缓存路径为 Ubuntu `~/.local/state/campus-network-assistant/update.json` 和 Windows `%LOCALAPPDATA%\CampusNetworkAssistant\update.json`，只允许当前用户读写。写入使用原子替换；同目录锁文件保证 GUI、手动命令和定时任务不会并发请求。缓存损坏时重建，但仍遵守文件 mtime 推导出的 24 小时自动检查下限。

自动检查使用独立调度器：Ubuntu 安装 systemd user timer，Windows 安装“仅在用户登录时运行”的 `CampusUpdateCheck` 计划任务。调度器每天触发一次并执行 `campus-network-assistant --check-updates --notify`；错过的任务在下一次登录后执行。无论上次成功、离线、超时或限流，自动请求在任意滚动 24 小时内最多一次。手动“检查更新”不受此限制，但按钮在请求进行时禁用，不能并发提交。

更新检查失败不影响网络服务；使用 5 秒连接超时、10 秒总超时，不在同一次任务中重试。GitHub 返回限流时，将下一次允许时间延后到 `X-RateLimit-Reset` 和 24 小时下限中的较晚者。

首次安装在发送任何 GitHub 请求前展示简短隐私说明，更新检查默认勾选启用，用户可以取消。无人值守安装默认关闭，只有显式参数 `--enable-update-check` 才启用。从 v4 升级时，在用户首次打开 v5 界面前保持关闭并询问一次。

### 8.2 版本比较

- 本地 `VERSION` 只接受 `MAJOR.MINOR.PATCH`；API 只读取 `tag_name`，只接受 `vMAJOR.MINOR.PATCH`。
- 三段均为无前导零的 0-999999 整数；v5.0 不支持 prerelease 或 build metadata。
- 数字分段比较，禁止把版本当普通字符串比较；忽略 API 中 `prerelease=true` 或 `draft=true` 的 Release。
- 本地版本格式异常时不提示升级，只记录脱敏错误。
- `html_url` 必须经结构化 URL 解析，并严格满足：scheme 为 `https`、无 userinfo、无显式非 443 端口、host 精确为 `github.com`、path 以 `/fan693/campus-network-autologin/releases/tag/v` 开头。API 请求只允许精确的 `api.github.com/repos/fan693/campus-network-autologin/releases/latest`，禁用跨 origin 重定向。

### 8.3 用户操作

更新通知提供：

- “查看更新”：打开 GitHub Release 页面。
- “忽略此版本”：仅忽略当前版本，后续新版本仍提醒。
- “稍后提醒”：写入 `snoozed_version` 和 `snoozed_until`，24 小时后该版本允许再主动提醒一次。

“每个版本最多主动提醒一次”指没有用户交互时只弹一次；用户点击“稍后提醒”是明确授权该版本在指定时间再弹一次。手动检查始终只更新窗口，不计为主动通知。

v5.0 不在后台下载或执行安装包。这样即使 Release 内容、网络响应或本地状态出现异常，也不会自动改变正在运行的远程网络环境。“查看更新”只打开经过上述白名单验证的 Release 页面。

## 9. 配置与秘密

- Ubuntu 继续使用 `/etc/campus-autologin/config.json`，权限保持 `root:campus-autologin 0640`。
- Windows 继续使用 `%APPDATA%\CampusAutoLogin\config.json`，安装器限制为当前用户可读。
- GUI 的用户偏好存放在用户目录，只包含通知、窗口、更新同意和忽略版本设置。
- 普通设置包括开机启动界面、应用勿扰、更新检查和窗口偏好，不提权；校园网账号、网络选择、门户和恢复参数属于受保护设置，按第 7.3 节保存。
- 配置保存前调用现有校验逻辑并持有配置锁，后台读配置只读取完整的旧文件或完整的新文件。
- 界面中的密码输入框默认隐藏，永远不回显已保存密码；留空表示保留原密码。
- 日志函数继续执行账号、密码、URL 编码值和 Base64 值脱敏。

GitHub 更新检查会向 GitHub 暴露普通网络请求必然包含的公网 IP 和 User-Agent，但不发送稳定设备 ID、校园网账号、连接名称、日志或配置。系统勿扰或应用勿扰任一开启时均不弹通知；无法读取系统勿扰状态时只服从应用设置。

## 10. 平台实现

### 10.1 Ubuntu

安装包内容：

- `/usr/lib/campus-network-assistant/`：PyInstaller 构建的桌面程序与私有 Qt 运行库。
- `/usr/lib/campus-autologin/`：现有后台代码、控制服务与 `campusctl`。
- `/usr/bin/campus-network-assistant`：桌面程序入口。
- `/usr/bin/campusctl`：固定控制命令入口。
- `/usr/share/applications/campus-network-assistant.desktop`：应用菜单入口。
- `/etc/xdg/autostart/campus-network-assistant.desktop`：托盘自启动。
- `/usr/share/icons/hicolor/`：状态图标。
- systemd 系统服务、远程恢复用户服务、更新检查 user timer、D-Bus policy 和 polkit action。

构建产物为 `campus-network-assistant_<version>_amd64.deb`。桌面二进制在 Ubuntu 20.04 amd64 的干净构建容器内用 PyInstaller 生成并携带匹配的 PySide6/Qt 私有库，从而兼容更新的目标系统；后台 Python 代码使用系统 Python。安装和卸载脚本必须可重复执行。

图形会话不可用时跳过托盘，但后台系统服务照常运行。QSystemTrayIcon 不可用时，应用菜单仍可打开主窗口；通知通过 `org.freedesktop.Notifications` 发送，通知服务也不可用时只在下次打开窗口时显示更新横幅，不把 `notify-send` 作为硬依赖。

v5.0 Ubuntu 安装是每台机器一个校园网配置、一个安装时选定的桌面用户，不支持多个用户分别配置不同校园网账号。`apt remove` 删除程序、服务和易失状态但保留 `/etc/campus-autologin/config.json`；`apt purge` 才删除配置和保存的凭据，保证非交互包管理可预测。从 v4 升级时，安装器必须使用第 7.5 节同一个受保护配置事务完成校验、备份、迁移、启动确认、失败恢复和清理；成功启动 v5 后再删除本项目拥有的旧 `/usr/local/lib/campus-autologin` 文件，不删除未知文件。

`.deb` 的 `postinst` 只创建无登录服务账户、安装/重载 unit、D-Bus policy 和 polkit action，不在 dpkg 中弹交互问题。存在有效旧配置时迁移并启用后台服务；新安装保持 `not_configured`，由用户从应用菜单启动首次设置，或执行 `sudo campus-network-assistant-setup --desktop-user <用户>`。设置成功后记录唯一桌面 UID，启用系统认证服务，并为该用户启用远程恢复和更新检查 user unit。系统 unit 必须使用 `RuntimeDirectory`、明确的 `User/Group`、最小地址族、只读系统目录和空 capability bounding set；只有 root 控制服务保留完成固定管理操作所需的权限。

### 10.2 Windows

使用 PyInstaller 生成 Windows x64、无控制台窗口且携带 Python/PySide6 运行库的可执行文件，再由 Inno Setup 制作 **per-user** 安装包，默认安装到 `%LOCALAPPDATA%\Programs\CampusNetworkAssistant`：

- 开始菜单快捷方式。
- 当前用户登录时启动托盘程序。
- 保留现有自动认证和远程恢复计划任务。
- 创建每天一次、仅在用户登录时运行的 `CampusUpdateCheck` 任务。
- 卸载页提供“保留校园网配置”复选框，默认保留。

托盘主进程保持普通用户权限。只有精确白名单中的第三方系统服务恢复才触发 UAC，提升助手使用安装时固化的原始用户 SID 和绝对路径，不读取提升账户的 `%APPDATA%`。每个用户、每个交互会话使用命名 mutex 防止重复托盘图标；快速用户切换时不同用户可以各自运行。

Windows 使用稳定的 `campus-launcher.exe` 和 side-by-side 版本目录，计划任务始终指向 launcher，不直接指向某个版本。升级事务固定为：

1. 在不停止旧任务时把新版本解压到新的只读版本目录，验证 Authenticode、manifest 和 SHA-256。
2. 按第 7.5 节创建受保护配置事务，并导出三个本项目计划任务的 XML 与 enabled/running 状态；任务备份使用同一 ACL。
3. 通知并关闭同一用户会话中的 GUI，停止本项目任务；旧版本目录保持不动。
4. 原子替换 launcher 使用的 `current-version.json`，注册/更新任务并启动新后台。
5. 60 秒内必须收到新 instance 的有效状态心跳；成功后提交配置事务，并保留一个旧版本目录到下一次成功启动。
6. 任一步失败都自动恢复旧版本指针、配置、任务 XML 和原 enabled/running 状态，重新启动旧后台并验证心跳；只有旧后台确认恢复后才向用户报告升级失败。

如果旧版本也无法恢复，安装器必须保持旧文件和诊断信息、返回失败并显示明确的本地修复入口，不能宣称升级成功。卸载始终删除程序、任务、缓存和易失状态；是否删除主配置及认证日志由复选框决定。

## 11. 目录规划

```text
VERSION
campus_desktop/
  __init__.py
  app.py                 # Qt 生命周期、单实例和托盘
  window.py              # 主窗口和设置窗口
  status.py              # 状态快照读取与过期判断
  updater.py             # GitHub Release 检查和 SemVer
  notifications.py       # 防重复通知
  actions.py             # campusctl 参数数组调用
  platform_linux.py
  platform_windows.py
  resources/
campusctl.py
campus_control.py          # Ubuntu root D-Bus 控制服务
campus_setup.py            # Ubuntu 首次绑定与 root rebind 入口
status_schema.py           # 主状态、远程状态和 IPC schema
packaging/
  linux/                   # debian metadata、systemd、D-Bus、polkit、timer
  windows/                 # PyInstaller spec、Inno Setup、计划任务
tests/
  test_status.py
  test_updater.py
  test_actions.py
  test_control_protocol.py
  test_config_migration.py
  test_desktop_smoke.py
```

现有 `campus_autologin.py` 和 `remote_recovery.py` 继续作为后台核心，不导入 PySide6。桌面模块可以导入后台中的纯数据结构，但后台不得反向依赖桌面模块。

## 12. 故障处理

| 故障 | 行为 |
| --- | --- |
| GitHub 不可访问 | 保持当前版本，不影响认证；按退避时间再检查 |
| 状态文件缺失 | 查询服务状态，界面显示“状态未知”而不是“离线” |
| 后台服务停止 | 显示红色状态，提供需要提权的重启操作 |
| GUI 崩溃 | systemd/计划任务不受影响，下次登录重新启动 GUI |
| 更新版本格式非法 | 忽略该 Release 并记录错误 |
| 配置校验失败 | 不覆盖旧配置，显示具体字段错误 |
| 提权被取消 | 保持原状态并显示“操作已取消” |
| 网络恢复处于冷却 | 显示剩余冷却时间，不重复触发 |

## 13. 发布流程

1. 更新 `VERSION` 和变更日志。
2. 运行全部单元测试、安装/升级/卸载测试和隐私扫描。
3. 构建 Ubuntu `.deb` 与 Windows 安装包。
4. Windows 可执行文件和安装包使用 Authenticode 代码签名；`.deb` 生成独立签名，发布公钥预置于上一稳定版和仓库。
5. 对产物计算 SHA-256，生成并签名 `SHA256SUMS`；生成只含版本、文件名、大小、SHA-256 和签名文件名的 `release-manifest.json`。
6. 在干净虚拟机执行安装、断网恢复、升级保留配置和卸载测试。
7. 创建签名 Git 标签和 GitHub Release，上传安装包、manifest、校验和与签名。
8. 用上一稳定版客户端确认能发现新 Release，但不会自动下载或安装。

缺少规定签名的构建不得标记为稳定版。GitHub HTTPS 和 SHA-256 只能检测传输/文件损坏，不能代替发布者签名。

发布说明不得包含本机路径、真实网络名称、账号、IP、日志片段或构建机环境变量。

## 14. 测试与验收

### 14.1 自动测试

- SemVer：新版本、相同版本、旧版本、预发布和非法版本。
- 更新响应：`200`、`304`、超时、无网络、限流和错误 JSON。
- URL 白名单：拒绝错误 owner/repo/path、userinfo、非 443 端口、跨 origin 重定向和非 HTTPS 页面。
- 状态快照：枚举、必填字段、未知字段、30 秒心跳、90 秒过期、未来 schema、instance_id/sequence、损坏 JSON 和权限保持。
- 状态合成：服务管理器冲突、多网卡、目标网络缺失、远程状态 unknown/failed 和冷却倒计时。
- 通知去重：首次提醒、稍后提醒、忽略版本、手动检查和勿扰组合。
- 更新缓存：原子写、损坏恢复、跨进程锁和滚动 24 小时请求上限。
- 操作安全：每个按钮映射固定 IPC 方法，不经过 shell；D-Bus/named-pipe 拒绝错误 UID/SID、超大请求和未知方法。
- 返回协议：所有退出码、超时、accepted/completed、并发合并和用户取消提权。
- 首次绑定：两个不同 UID 并发认领、同 UID 重试、管理员取消、v4 用户记录无效和 root rebind 回滚。
- 配置升级：v3/v4 配置升级到 v5 时密码不丢失、不输出；每个事务阶段崩溃后恢复；事务目录 owner/mode/ACL 正确且成功后无秘密副本残留。
- 日志与诊断：只允许稳定错误码和脱敏结构，不含门户原文或第三方原始日志。
- 后台回归：现有校园网和远程恢复测试全部继续通过。

### 14.2 Ubuntu 验收

- 安装后后台服务立即运行，托盘在下次图形登录出现。
- 新安装没有配置时服务保持安全 idle，由首次设置向导创建配置；检测到有效 v4 配置时自动迁移并启动。
- 关闭托盘后，真实断网恢复仍正常工作。
- 只有安装时选定的桌面用户能读取状态；普通状态查看不弹权限窗口，配置、删除凭据和重启服务才请求提权。
- 断网时更新检查不造成高频日志或重试。
- 从上一版本升级后，原校园网账号配置仍可用。
- `apt remove` 保留配置，`apt purge` 删除配置；两种方式都可非交互执行。
- 无托盘或通知服务时，主窗口仍能显示状态和更新横幅。

### 14.3 Windows 验收

- 安装包无需用户预装 Python。
- 安装范围为当前用户，计划任务、配置和缓存始终属于原始安装用户 SID。
- 登录后只出现一个托盘图标，无控制台窗口。
- UAC 取消不会损坏配置或停止计划任务。
- 升级保留配置，卸载可选择保留或清除配置。
- 用户注销后不承诺继续认证；重新登录后任务自动恢复。
- 对 staging、停止任务、切换版本指针、注册任务、启动和健康确认逐阶段注入失败，均能自动恢复旧二进制、旧任务状态、旧配置并重新产生有效心跳。

### 14.4 更新提醒验收

- 本地版本落后时 10 秒内完成手动检查并显示正确版本。
- 任何滚动 24 小时内自动请求最多一次；同一版本默认只弹一次，用户选择“稍后提醒”时允许再弹一次。
- 点击“查看更新”只打开官方 Release HTTPS 页面。
- 安装或升级时在第一次请求前完成隐私告知；禁用更新检查后不再自动访问 GitHub，手动检查仍需用户明确触发。

## 15. 开发顺序

### 阶段 A：版本与更新核心

- 增加 `VERSION`、SemVer 比较、GitHub Release 客户端和缓存。
- 提供无 GUI 的 `--check-updates` 命令及完整单元测试。

### 阶段 B：状态接口与控制层

- 后台写入脱敏状态快照。
- 实现 `campusctl` 白名单子命令和平台权限规则。

### 阶段 C：桌面界面

- 实现托盘、主窗口、设置、日志查看和通知去重。
- 保证关闭 GUI 不影响后台服务。

### 阶段 D：安装与发布

- 构建 `.deb`、Windows 安装包、自动启动和升级迁移。
- 在干净系统完成真实安装与断网恢复验收。

阶段 A 和 B 完成后再接 UI，避免界面先行却没有稳定状态契约。v5.0 的完成标准是两个平台均有可安装产物，并通过本节全部验收项，而不是仅能从源码启动窗口。

## 16. 后续版本

- 可选的签名自动更新与失败回滚。
- 多语言界面。
- 可导入、导出不含密码的诊断包。
- 更多远程控制客户端适配。
- 针对学校门户模板的社区贡献向导。

任何后续遥测、云同步或自动更新功能都必须默认关闭，并在实现前单独完成隐私与威胁模型评审。
