# 脚本互动接口使用说明

任务模型负责写脚本，宿主提供真实输入、操作接口和执行回执。采集多久、判断哪些内容、发送几次、何时结束、是否继续运行，都由脚本决定。不会因每条入站消息自动请求任务模型。

## 建立与运行

宿主根据真实用户已有授权提供独立工作目录，无需模型填写 `use_workspace=true`；明确禁用工作区或纯文字推理的任务仍按原选择处理。先用 `pc_task_workspace(action="info")` 查看实际工作目录与可读目录，再调用 `pc_interaction_channel(action="open", key="稳定键", ttl_seconds=86400)` 建立通道，在自身工作目录写 `.py` 或 `.ps1`。调用同一工具的 `action="run"`，提供 `channel_id`、相对 `path`、稳定 `key`、可选 `arguments`、`run_at`、`timeout`。

`run_at` 为空时尽快启动；未来时间使用 UTC 秒或带时区的 ISO 日期。`timeout` 是单次进程运行上限，最高沿用现有执行器的 3600 秒；更长流程由脚本保存状态并安排下一次执行。通道必须在下一次启动时仍有效，失效后不能自行续权。源任务结束不关闭通道。

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
| `tasks()` / `tasks(task_id)` | 原机器人、会话、真实账号的任务摘要或单任务详情，包含工具步骤与实际执行结果 |
| `history(limit=24)` | 原会话当前聊天历史，仅 user/assistant 文本；最多 100 条，不返回 system 或媒体数据 |
| `tools()` | 原任务允许集与当前可用集交集中的工具定义 |
| `call_tool(name, arguments, key)` | 按原授权直接调用工具并返回持久任务 ID，不额外请求任务模型，不自动发送结果 |
| `start_script(path, key, arguments, run_at, timeout)` | 安排脚本现在或以后运行，不自动重跑已中断进程 |
| `script_status(script_id)` / `stop_script(script_id)` | 查询启动、退出码、输出或中断状态；停止指定脚本进程树 |

## 结果判断与重试

- `reply()` 返回 `pending` 只说明受理。`sent` 才表示发送接口确认成功；`unknown` 不能当失败重发，以免重复。
- 用户新消息会使本通道未发出的旧回复失效，状态为 `superseded`。基于旧序号提交返回 `409 stale_cursor`；脚本应先读取新输入，再决定是否发言。已交给平台的消息无法由此接口撤回。
- `events()` 会推进 SDK 的本地游标。需要重启续读时自行把 `next_cursor` 保存在 `state()`，再次读取时传入 `after_seq`。消息按平台消息 ID 去重。
- 工具调用返回的是任务 ID；通过 `tasks(task_id)` 检查实际状态、结果和 `steps`，不能把排队或运行中当完成。列表只提供结果摘要，详情保留结构化步骤。
- 回复、工具调用、脚本启动都应使用稳定 `key`；相同键不同内容会冲突。网络超时后使用原键查询或重试，不能随意生成新键重复副作用。
- 服务重启保留待执行脚本；当时已启动的脚本记为 `interrupted`，不自动重跑。脚本可根据持久状态和已确认回执自行安排恢复。
- `stop_replies()` 只管理当前脚本通道，不关闭普通聊天，不影响其他机器人或其他通道。关闭机器人、撤销原授权或关闭通道后，接口拒绝后续操作。

## 原始 HTTP 与权限

接口只监听 `127.0.0.1:6196`。PowerShell 可读取运行器传入的配置文件，通过 `Authorization: Bearer <token>` 调用，不使用网页管理员令牌。不得把配置内容、令牌打印到工具输出或发到聊天。

对应路径为 `/v1/events`、`/v1/status`、`/v1/state`、`/v1/reply`、`/v1/replies/{id}`、`/v1/stop`、`/v1/resume`、`/v1/close`、`/v1/tasks[/{id}]`、`/v1/history`、`/v1/tools`、`/v1/tool-call`、`/v1/scripts`、`/v1/scripts/{id}`、`/v1/scripts/{id}/stop`。读操作用 GET；状态写入、发言、控制、工具调用和脚本启动/停止用 POST，正文为 JSON。

通道绑定原机器人、会话及真实账号，脚本不能传入其他身份。共享工具文件全局可读，执行身份和普通任务目录仍按原授权隔离。同机器人同群目录可经 `pc_task_workspace` 固定接口互读，主要用户私聊可查看该机器人各群目录；他人私聊、其他机器人和内部令牌文件不开放。跨工作区只读不允许以他人账号运行任意命令，写入和命令仍限自身目录。每次调用及实际执行都复验权限。这里开放的是编排接口，不自动扩大工具范围或授权管理机器人本身。

当前消息采集记录文本与组件类型；纯图片能看到图片组件，但此接口不提供图片识别结果或任意本地媒体路径。原有普通聊天、90 秒任务进度和定时工具继续兼容；本次没有把所有旧功能一律删除或改写。
