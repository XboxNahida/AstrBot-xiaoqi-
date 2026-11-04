# 脚本互动接口使用说明

任务模型负责写脚本，宿主提供真实输入、操作接口和执行回执。采集多久、判断哪些内容、发送几次、何时结束、是否继续运行，都由脚本决定。不会因每条入站消息自动请求任务模型。

## 建立与运行

宿主根据真实用户已有授权提供独立工作目录；是否具备工具能力由当前身份和授权决定，无需模型填写 `use_workspace=true`。实际开户或执行前才准备受限账户。先用 `pc_task_workspace(action="info")` 查看实际工作目录与可读目录，再用 `pc_interaction_channel(action="list")` 查已有通道；有通道时先 `action="list_scripts"` 查询原脚本和回执，避免因重试或换任务重复创建。

需要新通道时调用 `pc_interaction_channel(action="open", key="稳定键", ttl_seconds=86400, cancel_on_input=false)`，在自身工作目录写 `.py` 或 `.ps1`。`action="run"` 提供 `channel_id`、相对 `path`、稳定 `key`、可选 `arguments`、`run_at`、`timeout`，返回已有或新建的持久脚本 ID。返回 `pending` 只表示已经排程。

`run_at` 为空时尽快启动；未来时间使用 UTC 秒或带时区的 ISO 日期。`timeout` 是单次进程运行上限；当前 Windows 执行账户确实限制单次最多 3600 秒，与互动期限无关。更长流程由任务模型决定如何分段，脚本保存状态并安排后续执行。源任务结束不关闭通道。

通道期限须覆盖未来启动时间。`script_starts_after_channel_expiry` 会返回 `expires_at`、`run_at` 和修正提示；模型可调用 `action="renew", channel_id="原ID", ttl_seconds=...` 显式调整期限，再决定是否修改计划。续期保留原 ID、令牌、脚本和状态，不自动重跑脚本。当前授权有效的任务也可续期已经到期但未关闭的原通道；已到期脚本令牌不能自行续期，已关闭或真实撤权的通道不能借续期恢复权限。到期通道可直接停止或关闭，无需为了停止先续期。

新通道的 `cancel_on_input` 默认 `false`：用户新消息只作为真实输入，不替脚本决定是否取消待发回复。需要“任意回复即使旧待发内容失效”的流程可显式选择 `true`，并由脚本读取输入后决定是否结束及取消后续排程。`renew` 可同时设置该策略；省略时保留现值。升级前已存在的通道保留原行为，模型可明确调整。

脚本以原任务的低权限账号运行，不占用任务模型执行队列；每通道一次只运行一个脚本。同通道其他脚本按计划排队。全局共享 SDK 位于 `C:\AstrBotTasks\tools\astrbot_interaction.py`，运行器自动设置导入路径与 `ASTRBOT_INTERACTION_CONFIG`，令牌只写入原工作目录的配置文件，不返回给模型。

```python
from astrbot_interaction import InteractionClient

client = InteractionClient.from_env()
page = client.events(after_seq=0)
print(page["total_messages"], page["last_message"])

# 固定窗口采集原始消息。如何处理，由当前任务脚本定义。
for message in client.iter_events(window_seconds=30):
    print(message["seq"], message["received_at"], message["text"])
```

这是接口调用示例，不是默认互动方案，不会自动向用户发送任何消息。

## 可用接口

| SDK 方法 | 数据或操作 |
| --- | --- |
| `events(after_seq, since, until, wait_seconds, limit)` | 真实入站消息、原文、组件类型、序号、接收时间、窗口计数、总计数、最新消息、分页游标 |
| `iter_events(window_seconds, after_seq)` / `collect(...)` | 在脚本指定的时间窗口采集消息；迭代可自行提前结束 |
| `status()` | 当前通道状态与消息计数，不改变 SDK 游标 |
| `state()` / `set_state(state, expected_revision)` | 持久 JSON 自定义状态，版本冲突返回 409 |
| `reply(message, key, based_on_seq)` | 请求按当前人格和聊天上下文表达内容，返回待发送请求 ID |
| `reply_status(request_id)` | 查询真实发送状态及回执时间 |
| `stop_replies(pause=True)` | 取消本通道尚未发出的消息并暂停后续发送；继续采集输入 |
| `stop_replies(pause=False)` | 取消当前待发消息，通道继续开放 |
| `resume_replies()` | 恢复本通道的后续发送，不恢复已取消消息 |
| `close()` | 关闭通道、取消待发送请求并停止本通道脚本 |
| `renew(ttl_seconds, cancel_on_input=None)` | 显式调整自身活动通道期限及输入失效策略；到期后由当前授权任务使用通道工具续期 |
| `tasks()` / `tasks(task_id)` | 原机器人、会话、真实账号的任务摘要或单任务详情，包含工具步骤与实际执行结果 |
| `history(limit=24)` | 原会话当前聊天历史，仅 user/assistant 文本；最多 100 条，不返回 system 或媒体数据 |
| `tools()` | 当前真实身份已授权且宿主实际提供的工具定义，以接口响应为准 |
| `call_tool(name, arguments, key)` | 按原授权直接调用工具并返回持久任务 ID，不额外请求任务模型，不自动发送结果 |
| `start_script(path, key, arguments, run_at, timeout)` | 安排脚本现在或以后运行，不自动重跑已中断进程 |
| `scripts(offset=0, limit=50)` | 分页发现本通道已有脚本 ID、计划、状态和真实回执，返回 `next_offset` |
| `script_status(script_id)` / `stop_script(script_id)` | 查询状态、退出码、输出、`finished_at/error_code`；停止指定脚本进程树 |

通道工具对应的生命周期操作为 `open/list/renew/list_scripts/read/stop/resume/close/run/script_status/stop_script`；除 `open/list` 外均须提供原 `channel_id`。`list_scripts` 使用 `offset/limit` 翻页，模型可在通道到期或关闭后继续查询自己原脚本的回执。

脚本工具目录读取当前真实身份的可用工具，不以建立通道时的旧工具名称快照永久限制后续纠错。每次实际调用仍验证当前工具和授权；已禁用或撤销的工具立即不可调用。通道工具 `pc_interaction_channel` 不通过脚本工具调用递归暴露，生命周期操作使用上表的专用 SDK 接口。

## 结果判断与重试

- `reply()` 返回 `pending` 只说明受理。`sent` 才表示发送接口确认成功；`unknown` 不能当失败重发，以免重复。
- 显式选择 `cancel_on_input=true` 时，用户新消息使本通道未发出的旧回复变成 `superseded`，基于旧序号提交返回 `409 stale_cursor`；默认 `false` 不因新消息自动取消。显式停止、关闭、真实撤权及通道代次变化仍在发送前复验。已交给平台的消息无法由此接口撤回。
- `events()` 会推进 SDK 的本地游标。需要重启续读时自行把 `next_cursor` 保存在 `state()`，再次读取时传入 `after_seq`。消息按平台消息 ID 去重。
- 工具调用返回的是任务 ID；通过 `tasks(task_id)` 检查实际状态、结果和 `steps`，不能把排队或运行中当完成。列表只提供结果摘要，详情保留结构化步骤。
- 回复、工具调用、脚本启动都应使用稳定 `key`；相同键不同内容会冲突。网络超时或结果不确定时，模型应先查询原 ID 和真实状态，再决定下一步；不能通过更换键默认重复副作用。新任务处理已有计划时，应先 `list_scripts` 找回原记录。
- 服务重启保留待执行脚本；当时已启动的脚本记为 `interrupted`，不自动重跑。到期而无法启动的原 `pending` 会落 `expired`，保留原 ID、原因及结束时间。
- `finished` 仅证明进程以成功退出码结束，不能代替互动目标完成。`failed/interrupted/expired` 保存真实回执并通过可信恢复事件交给任务模型，由模型决定调查、改脚本、调整期限、继续、停止或询问用户；宿主不把脚本终态写成固定重跑方案，不额外自动发送 QQ。恢复事件使用稳定 ID 幂等提交，重启后可能再次投递同一事件，但不因此重复执行原脚本；既有历史失败不因升级自动补交。
- `stop_replies()` 只管理当前脚本通道，不关闭普通聊天，不影响其他机器人或其他通道。关闭机器人、撤销原授权或关闭通道后，接口拒绝后续操作。

## 原始 HTTP 与权限

接口只监听 `127.0.0.1:6196`。PowerShell 可读取运行器传入的配置文件，通过 `Authorization: Bearer <token>` 调用，不使用网页管理员令牌。不得把配置内容、令牌打印到工具输出或发到聊天。

对应路径为 `/v1/events`、`/v1/status`、`/v1/state`、`/v1/reply`、`/v1/replies/{id}`、`/v1/stop`、`/v1/resume`、`/v1/close`、`/v1/renew`、`/v1/tasks[/{id}]`、`/v1/history`、`/v1/tools`、`/v1/tool-call`、`/v1/scripts`、`/v1/scripts/{id}`、`/v1/scripts/{id}/stop`。`GET /v1/scripts?offset=0&limit=50` 列脚本，`POST /v1/scripts` 排程；其他读操作用 GET，状态写入、发言、续期、控制、工具调用和脚本启动/停止用 POST，正文为 JSON。SDK 的 `InteractionError.details` 保留接口返回的错误细节，便于脚本诊断，令牌不会放入错误正文。

通道绑定原机器人、会话及真实账号，脚本不能传入其他身份。主要用户及已有有效任务授权的群友可使用普通任务能力，撤销或过期立即停止；管理机器人本身仍需单独授权。共享工具文件全局可读，写入和任意命令仍以调用者自身的受限账号执行，不能借用其他群友账号或改写机器人核心。

普通文件读取统一使用 `pc_task_read_file` 或 `pc_task_workspace` 的固定 `read/list` 接口，可提供本地绝对路径，支持跨工作区查询普通文档及源码；无需通过命令扫描受保护的父目录。密钥、凭据、内部通道令牌、机器人私有运行数据及数据库拒绝读取，普通文本返回前清理敏感字段及已配置密钥。重解析点、硬链接、网络和设备路径不接受。文本读取上限 128 KiB，目录每页最多 50 项，用返回的 `next_offset` 翻页。每次查询前后均复验调用者授权。普通读取不提供其他工作区的写入能力，也不改变互动通道、聊天历史与消息接口的身份隔离。

调用 `pc_interaction_channel` 时，先 `list/open` 取得 `channel_id`，后续操作带上该 ID。漏传 ID 返回 `channel_id_required`，不代表主动消息权限不足。确定的工具错误及未执行参数错误交回任务模型决定如何处理；不以工具名、固定纠错次数或脚本业务策略替模型结束任务。真实身份、授权、隔离及密钥保护持续核验，不确定的副作用结果不会自动重播。

当前消息采集记录文本与组件类型；纯图片能看到图片组件，但此接口不提供图片识别结果或任意本地媒体路径。原有普通聊天、90 秒任务进度和定时工具继续兼容；本次没有把所有旧功能一律删除或改写。
