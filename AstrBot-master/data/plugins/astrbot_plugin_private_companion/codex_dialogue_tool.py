"""Expose Codex conversations through the trusted chat event and task scope."""

from __future__ import annotations

import json

import jsonschema

from astrbot.core.agent.tool import FunctionTool


class CodexDialogueTool(FunctionTool):
    """Bind dialogue operations to one original user event.

    Args:
        runtime: Host-owned task runtime with durable Codex bindings.
        event: Actual registered chat event; tool context cannot replace it.
    """

    def __init__(self, runtime, event):
        super().__init__(
            name="pc_codex_dialogue",
            description=(
                "直接管理独立Codex对话，每个对话关联后台任务列表的一条task_id。"
                "create创建返回task_id/thread_id和真实状态，接收不等于完成；"
                "显式create只支持实际GPT模型，网页关闭Codex则engine_disabled，非GPT则unsupported_model，不另建原生任务。"
                "普通pc_submit_task_intent默认新GPT走Codex、非GPT或网页native走原生。"
                "model_id必须先查pc_list_task_models，沿用网页目录的真实ID。"
                "model_id/reasoning_effort仅用于create/guide/resume，不适用的参数会明确拒绝。"
                "list/read按需查看真实工具时间线，无需执行授权；不要持续轮询模型。"
                "guide引导同线程，运行中模型/思考程度修改只应用下一turn，原turnId继续接收引导；"
                "stop停止当前轮，resume核对进度续做，不自动重放旧操作。"
                "默认思考程度medium，可按对话改为模型支持的非空档位（如low/medium/high/xhigh），不修改Gemini。"
                "task_id/thread_id从真实回执选取，双ID必须一致。Codex自行压缩，无需每次重复整段聊天。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "list", "read", "guide", "stop", "resume"],
                    },
                    "goal": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                        "pattern": r"\S",
                    },
                    "fragment_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": r"\S",
                    },
                    "model_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": r"\S",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": r"\S",
                    },
                    "task_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": r"\S",
                    },
                    "thread_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": r"\S",
                    },
                    "text": {"type": "string", "maxLength": 16000},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
                    "after": {"type": "integer", "minimum": 0, "maximum": 2**53 - 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event)

    async def call(self, context, **kwargs):
        """Validate arguments and dispatch only within the captured scope.

        Args:
            context: Ignored for identity and execution authorization.
            **kwargs: Bounded dialogue operation and actual record selectors.

        Returns:
            Sanitized JSON containing actual receipts or an explicit error.
        """
        rt, e = self.runtime, self.event
        if e not in rt._events or rt.scope(e) != self.scope or not rt._trusted_event(e):
            return json.dumps(
                {"ok": False, "accepted": False, "code": "untrusted_event"}
            )
        try:
            jsonschema.validate(kwargs, self.parameters)
            action = kwargs["action"]
            allowed = {
                "create": {"goal", "fragment_key", "model_id", "reasoning_effort"},
                "list": {"offset", "limit"},
                "read": {"task_id", "thread_id", "after", "limit"},
                "guide": {
                    "task_id",
                    "thread_id",
                    "text",
                    "model_id",
                    "reasoning_effort",
                },
                "stop": {"task_id", "thread_id"},
                "resume": {
                    "task_id",
                    "thread_id",
                    "text",
                    "model_id",
                    "reasoning_effort",
                },
            }
            if set(kwargs) - allowed[action] - {"action"}:
                raise ValueError("Parameters do not apply to this action")
            if action == "create" and "goal" not in kwargs:
                raise ValueError("create requires goal")
            if action not in {"create", "list"} and not (
                kwargs.get("task_id") or kwargs.get("thread_id")
            ):
                raise ValueError("operation requires task_id or thread_id")
            if action not in {"create", "list", "read"}:
                binding = rt.codex.read(
                    self.scope,
                    task_id=kwargs.get("task_id", ""),
                    thread_id=kwargs.get("thread_id", ""),
                )
                job = rt.service.get(binding["task_id"], self.scope)
                if not rt.authorize_job(job):
                    raise PermissionError("Codex task authorization required")
            if "model_id" in kwargs:
                catalog = json.loads(await rt.make_model_tool(e).call(None))
                if not any(
                    item.get("id") == kwargs["model_id"]
                    for item in catalog.get("models", ())
                ):
                    return json.dumps(
                        {"ok": False, "accepted": False, "code": "invalid_model_policy"}
                    )
            if action == "create":
                payload = {
                    key: kwargs[key]
                    for key in ("goal", "fragment_key", "model_id", "reasoning_effort")
                    if key in kwargs
                }
                result = await rt.submit(
                    e, {"action": "submit", **payload}, require_codex=True
                )
                result["ok"] = result.get("accepted", False)
            elif action == "list":
                result = {
                    "ok": True,
                    **rt.codex.list(
                        self.scope,
                        offset=kwargs.get("offset", 0),
                        limit=kwargs.get("limit", 20),
                    ),
                }
            else:
                selectors = {
                    key: kwargs[key]
                    for key in ("task_id", "thread_id")
                    if key in kwargs
                }
                if action == "read":
                    result = rt.codex.read(
                        self.scope,
                        **selectors,
                        after=kwargs.get("after", 0),
                        limit=kwargs.get("limit", 20),
                    )
                elif action == "guide":
                    result = await rt.codex.guide(
                        self.scope,
                        **selectors,
                        text=kwargs.get("text", ""),
                        model_id=kwargs.get("model_id"),
                        reasoning_effort=kwargs.get("reasoning_effort"),
                    )
                elif action == "stop":
                    result = await rt.codex.stop(self.scope, **selectors)
                else:
                    if "model_id" in kwargs or "reasoning_effort" in kwargs:
                        configured = await rt.codex.guide(
                            self.scope,
                            **selectors,
                            text="",
                            model_id=kwargs.get("model_id"),
                            reasoning_effort=kwargs.get("reasoning_effort"),
                        )
                        if configured.get("error"):
                            return rt._safe_text(
                                json.dumps(
                                    {
                                        **configured,
                                        "ok": False,
                                        "code": configured["error"],
                                    },
                                    ensure_ascii=False,
                                    default=str,
                                ),
                                2**63 - 1,
                            )
                    result = await rt.codex.resume(
                        self.scope, **selectors, text=kwargs.get("text", "")
                    )
                result = {
                    **result,
                    "ok": not bool(result.get("error")),
                    "engine": "codex",
                }
                if result.get("error"):
                    result["code"] = result["error"]
        except jsonschema.ValidationError:
            result = {"ok": False, "accepted": False, "code": "invalid_arguments"}
        except LookupError:
            result = {"ok": False, "accepted": False, "code": "not_found"}
        except PermissionError:
            result = {"ok": False, "accepted": False, "code": "permission_denied"}
        except ValueError as exc:
            result = {
                "ok": False,
                "accepted": False,
                "code": "invalid_arguments",
                "message": str(exc),
            }
        except Exception as exc:
            result = {
                "ok": False,
                "accepted": False,
                "code": "codex_operation_failed",
                "message": str(exc),
                "next_action": "先read原任务与线程事实；结果未知时不要重放旧操作。",
            }
        return rt._safe_text(
            json.dumps(result, ensure_ascii=False, default=str), 2**63 - 1
        )
