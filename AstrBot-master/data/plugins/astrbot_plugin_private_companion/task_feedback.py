"""仅在持久任务异常时唤起前台，允许查证并纠正原任务。"""

import json

import jsonschema

from astrbot.core.agent.tool import FunctionTool, ToolSet


class FeedbackText(str):
    """携带本次纠正的版本凭证，防止旧失败通知被误发。"""

    def __new__(cls, text, recovery_revision=None, recovery_key=None):
        value = super().__new__(cls, text)
        value.recovery_revision = recovery_revision
        value.recovery_key = recovery_key
        return value


class RecoverBackgroundTool(FunctionTool):
    def __init__(self, runtime, job):
        super().__init__(
            name="pc_recover_background",
            description=(
                "给本次异常对应的原任务补充具体纠正建议并恢复处理，保留目标、检查点和原授权。"
                "先查清错误，避免重复副作用；不能换模型、改变工作区或越过用户取消和撤权。"
                "不需要修复或缺少必要信息时直接说明，不必调用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "expected_revision": {"type": "integer"},
                    "guidance": {"type": "string", "minLength": 1, "maxLength": 4000},
                },
                "required": ["expected_revision", "guidance"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.job = runtime, job
        self.recovery_revision = None
        self.recovery_key = None

    async def call(self, context, **kwargs):
        rt, original = self.runtime, self.job
        current = rt.service.get(original.task_id, original.scope)
        if current is None or not rt.authorize_job(current):
            return json.dumps({"ok": False, "code": "authorization_revoked"})
        guidance = rt._safe_text(kwargs["guidance"], 4000)
        if current.metadata.get("engine") == "codex":
            receipt = await rt.control_codex_task(
                current,
                "recover",
                expected_revision=kwargs["expected_revision"],
                recovery_guidance=guidance,
            )
            ok = receipt.get("accepted") is True
            if ok:
                self.recovery_revision = receipt.get("recovery_revision")
                self.recovery_key = receipt.get("recovery_key")
            return json.dumps(
                {
                    "ok": ok,
                    "code": receipt.get("code", "codex_recovery_failed"),
                    "task_id": current.task_id,
                    "thread_id": receipt.get("thread_id"),
                    "status": receipt.get("status"),
                }
            )
        ok = rt.service.recover_with_guidance(
            original.task_id, original.scope, kwargs["expected_revision"], guidance
        )
        if ok:
            current = rt.service.get(original.task_id, original.scope)
            self.recovery_revision = current.revision
            self.recovery_key = current.metadata.get("feedback_recovery_key")
        return json.dumps({"ok": ok, "code": "recovery_queued" if ok else "state_changed_or_recovery_unsafe"})


async def render_error_feedback(runtime, provider, job, **request):
    """事件触发的工具对话；没有后台监控循环或定时模型请求。"""
    event = runtime.restore_event(job)
    # 通知不是新用户指令：仅临时绑定持久任务身份供只读工具使用。
    runtime._events[event] = {"notification_task_id": job.task_id}
    recovery = RecoverBackgroundTool(runtime, job)
    tools = ToolSet([runtime.make_read_tool(event), recovery])
    contexts = list(request.pop("contexts", []))
    prompt = request.pop("prompt")
    try:
        while True:
            current = runtime.service.get(job.task_id, job.scope)
            if current is None or current.status in {"cancelled", "paused"}:
                return FeedbackText("")
            response = await provider.text_chat(prompt=prompt, contexts=contexts, func_tool=tools, **request, timeout=60)
            calls = getattr(response, "tools_call_name", [])
            role = getattr(response, "role", "")
            # 核心OpenAI适配器以tool标记模型工具调用；它仍是本轮模型的有效响应。
            if role not in {"assistant", "tool"} or (role == "tool" and not calls):
                raise ValueError("invalid_feedback_response")
            if not calls:
                return FeedbackText(
                    runtime._safe_text(getattr(response, "completion_text", ""), 4000),
                    recovery.recovery_revision,
                    recovery.recovery_key,
                )
            # 复用正式工具协议序列化，保留Gemini签名和调用ID。
            serialized = response.to_openai_tool_calls_model()
            if prompt:
                contexts.append({"role": "user", "content": prompt})
                prompt = ""
            contexts.append({
                "role": "assistant", "content": response.completion_text or "",
                "tool_calls": [call.model_dump(exclude_none=True) for call in serialized],
            })
            for call in serialized:
                tool = tools.get_tool(call.function.name)
                try:
                    if tool is None:
                        raise ValueError("tool_not_available_in_error_feedback")
                    args = json.loads(call.function.arguments)
                    jsonschema.validate(args, tool.parameters)
                    result = await tool.call(None, **args)
                except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
                    result = json.dumps({"ok": False, "code": "invalid_tool_arguments"})
                contexts.append({
                    "role": "tool", "tool_call_id": call.id,
                    "content": runtime._safe_text(result, 24000),
                })
    finally:
        runtime._events.pop(event, None)
