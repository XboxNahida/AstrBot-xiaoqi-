"""Expose identity-bound task administration directly to the chat model."""

from __future__ import annotations

import json
import sqlite3

import jsonschema

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from .task_catalog import TASK_GUIDANCE
from .task_execution import actor_workspace


class TaskManagementTool(FunctionTool):
    """Administer owned work without submitting a background query job.

    Args:
        runtime: Host-owned execution runtime.
        event: Original trusted chat event; model context cannot replace it.
    """

    def __init__(self, runtime, event):
        super().__init__(
            name="pc_manage_tasks",
            description=(
                TASK_GUIDANCE
                + "\nlist查询四类真实数量和分页清单；detail查询详情；classify只调整未来安排长期/短期属性；"
                "archive/unarchive管理历史展示；delete只删除已归档的终态历史正文。"
                "pause/resume/cancel/retry使用source和真实item_id；取消一个后台工作不会自动取消它创建的定时安排。"
                "schedule复用官方future_task的create/edit操作，不创建GPT查询任务。"
                "同一条定时创建使用稳定operation_key；同一消息的不同明确安排使用不同键。结果未知时先查询，不换键盲目重建。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list",
                            "detail",
                            "classify",
                            "archive",
                            "unarchive",
                            "delete",
                            "pause",
                            "resume",
                            "cancel",
                            "retry",
                            "schedule",
                        ],
                    },
                    "source": {
                        "type": "string",
                        "enum": ["background", "script", "schedule"],
                    },
                    "item_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "category": {
                        "type": "string",
                        "enum": [
                            "active",
                            "all",
                            "long_term",
                            "short_term",
                            "background",
                            "history",
                        ],
                    },
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "include_archived": {"type": "boolean"},
                    "operation_key": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                    "schedule": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["create", "edit"]},
                            "job_id": {"type": "string"},
                            "name": {"type": "string"},
                            "note": {"type": "string"},
                            "cron_expression": {"type": "string"},
                            "run_at": {"type": "string"},
                            "run_once": {"type": "boolean"},
                            "task_kind": {
                                "type": "string",
                                "enum": ["reminder", "monitor"],
                            },
                        },
                        "required": ["action"],
                        "additionalProperties": False,
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event)

    async def call(self, context, **kwargs):
        """Validate arguments and return an actual administrative receipt.

        Args:
            context: Ignored for identity; the original event stays authoritative.
            **kwargs: Bounded action and record selectors.

        Returns:
            JSON result or actionable failure with no invented success.
        """
        rt, e = self.runtime, self.event
        if e not in rt._events or rt.scope(e) != self.scope or not rt._trusted_event(e):
            return json.dumps({"ok": False, "code": "untrusted_event"})
        try:
            jsonschema.validate(kwargs, self.parameters)
            result = await self._call(kwargs)
        except jsonschema.ValidationError:
            result = {"ok": False, "code": "invalid_arguments"}
        except PermissionError:
            result = {"ok": False, "code": "permission_denied"}
        except (ValueError, OSError, sqlite3.Error):
            result = {
                "ok": False,
                "code": "management_failed",
                "next_action": "重新查询真实状态后修正参数，未验证前不要宣称成功。",
            }
        except Exception:
            result = {
                "ok": False,
                "code": "management_result_unknown",
                "next_action": "执行来源暂不可用，先查询真实状态；不要宣称成功或盲目重复创建。",
            }
        return rt._safe_text(
            json.dumps(result, ensure_ascii=False, default=str), 2**63 - 1
        )

    async def _call(self, args):
        """Dispatch one validated action to the existing owner-bound service."""
        rt, scope, e = self.runtime, self.scope, self.event
        action = args["action"]
        if action == "list":
            return await rt.catalog.read(
                scope,
                **{
                    key: args[key]
                    for key in ("category", "offset", "limit", "include_archived")
                    if key in args
                },
            )
        if action == "schedule":
            actor = rt.actor(e)
            if not rt.permissions.check(
                actor, "schedule", actor_workspace(rt.workspace, actor)
            ):
                return {"ok": False, "code": "permission_denied"}
            operation = args.get("schedule")
            if not operation:
                return {"ok": False, "code": "missing_schedule_arguments"}
            tool = rt._registry(e.unified_msg_origin).get("future_task")
            if tool is None:
                return {"ok": False, "code": "schedule_tool_unavailable"}
            bound = ContextWrapper(
                context=AstrAgentContext(context=rt.plugin.context, event=e)
            )
            key = (
                scope.bot_id,
                scope.session_key,
                scope.sender_id,
                str(e.message_obj.message_id or ""),
                args.get("operation_key", "main"),
            )
            if not key[3]:
                return {"ok": False, "code": "original_message_id_required"}
            guarded = operation["action"] == "create"
            unknown = {
                "ok": False,
                "code": "schedule_result_unknown",
                "next_action": "先list核对真实安排，不重建同一任务。",
            }
            if guarded:
                with rt.service._transaction() as db:
                    old = db.execute(
                        "SELECT receipt FROM pc_task_management_ops WHERE bot_id=? AND session_key=? AND sender_id=? AND message_id=? AND operation_key=?",
                        key,
                    ).fetchone()
                    if old:
                        return json.loads(old[0]) if old[0] else unknown
                    db.execute(
                        "INSERT INTO pc_task_management_ops(bot_id,session_key,sender_id,message_id,operation_key) VALUES(?,?,?,?,?)",
                        key,
                    )
            try:
                text = await tool.call(bound, **dict(operation))
            except Exception:
                return unknown
            receipt = {
                "ok": not str(text).lower().startswith("error:"),
                "action": "schedule",
                "receipt": str(text),
            }
            if guarded:
                with rt.service._transaction() as db:
                    if receipt["ok"]:
                        db.execute(
                            "UPDATE pc_task_management_ops SET receipt=? WHERE bot_id=? AND session_key=? AND sender_id=? AND message_id=? AND operation_key=?",
                            (json.dumps(receipt), *key),
                        )
                    else:
                        # Definite validation failures may be corrected in place.
                        db.execute(
                            "DELETE FROM pc_task_management_ops WHERE bot_id=? AND session_key=? AND sender_id=? AND message_id=? AND operation_key=?",
                            key,
                        )
            return receipt
        source, item_id = args.get("source"), args.get("item_id")
        if not source or not item_id:
            return {
                "ok": False,
                "code": "source_and_item_id_required",
                "next_action": "先list获取真实来源和ID。",
            }
        if action in {"classify", "archive", "unarchive"}:
            return await rt.catalog.change(
                scope, source, item_id, action, category=args.get("category")
            )
        if action == "delete":
            return await rt.catalog.delete(scope, source, item_id)
        items, unavailable = await rt.catalog._records(scope)
        item = next(
            (v for v in items if v["source"] == source and v["item_id"] == item_id),
            None,
        )
        if item is None:
            return {
                "ok": False,
                "code": "source_unavailable" if source in unavailable else "not_found",
            }
        if action == "detail":
            if source == "background":
                details = json.loads(
                    await rt.make_read_tool(e).call(None, task_id=item_id)
                )
            elif source == "script":
                details = json.loads(
                    await rt.make_read_tool(e).call(
                        None, resource="scripts", channel_id=item["channel_id"]
                    )
                )
            else:
                details = item
            return {"ok": True, "item": item, "details": details}
        if source == "background":
            if action == "cancel" and item["status"] == "cancelled":
                return {"ok": True, "changed": False, "status": "cancelled"}
            receipt = await rt.submit(e, {"action": action, "task_id": item_id})
            return {**receipt, "ok": receipt.get("accepted") is True}
        actor = rt.actor(e)
        if action in {"resume", "retry"} and not rt.permissions.check(
            actor, "schedule", actor_workspace(rt.workspace, actor)
        ):
            return {"ok": False, "code": "permission_denied"}
        if source == "schedule":
            if action not in {"pause", "resume", "cancel"}:
                return {"ok": False, "code": "unsupported_schedule_action"}
            if action == "cancel":
                if item["status"] == "cancelled":
                    return {
                        "ok": True,
                        "changed": False,
                        "status": "cancelled",
                        "item_id": item_id,
                    }
                # Persist an honest uncertain state before a non-atomic scheduler call.
                saved = {
                    **item,
                    "status": "cancel_result_unknown",
                    "active": True,
                    "needs_attention": True,
                }
                with rt.service._transaction() as db:
                    db.execute(
                        "INSERT INTO pc_task_catalog(bot_id,session_key,sender_id,source,item_id,saved) VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id,session_key,sender_id,source,item_id) DO UPDATE SET saved=excluded.saved,revision=revision+1",
                        (
                            scope.bot_id,
                            scope.session_key,
                            scope.sender_id,
                            source,
                            item_id,
                            json.dumps(saved),
                        ),
                    )
                await rt.cron_manager.delete_job(item_id)
                saved.update(
                    status="cancelled",
                    active=False,
                    category="history",
                    needs_attention=False,
                )
                with rt.service._transaction() as db:
                    db.execute(
                        "UPDATE pc_task_catalog SET saved=?,revision=revision+1 WHERE bot_id=? AND session_key=? AND sender_id=? AND source=? AND item_id=?",
                        (
                            json.dumps(saved),
                            scope.bot_id,
                            scope.session_key,
                            scope.sender_id,
                            source,
                            item_id,
                        ),
                    )
                return {
                    "ok": True,
                    "changed": True,
                    "status": "cancelled",
                    "item_id": item_id,
                    "note": "后续定时安排已取消，已经启动的后台执行需单独查询和取消。",
                }
            updated = await rt.cron_manager.update_job(
                item_id, enabled=action == "resume"
            )
            return {
                "ok": updated is not None,
                "changed": updated is not None,
                "enabled": action == "resume",
                "item_id": item_id,
            }
        if action == "cancel":
            channel = rt.interaction_api.channels.get(
                scope, item["channel_id"], include_inactive=True
            )
            if channel is None:
                return {"ok": False, "code": "not_found"}
            await rt.interaction_api.script_runner.cancel(channel, item_id)
            status = rt.interaction_api.script_runner.status(channel, item_id)["status"]
            return {"ok": status == "cancelled", "status": status, "item_id": item_id}
        return {
            "ok": False,
            "code": "unsupported_script_action",
            "next_action": "脚本支持cancel；修改执行逻辑或重建安排请交给后台模型，复用原脚本及稳定操作键。",
        }
