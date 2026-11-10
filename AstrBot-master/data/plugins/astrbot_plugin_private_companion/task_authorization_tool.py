"""Let the frontend manage permanent group grants using the real owner's identity."""

from __future__ import annotations

import json
import re

import jsonschema

from astrbot.core.agent.tool import FunctionTool


class TaskAuthorizationTool(FunctionTool):
    """Bind authorization management to one original inbound group event."""

    def __init__(self, runtime, event):
        super().__init__(
            name="pc_manage_task_authorization",
            description=(
                "按主要用户的自然语言要求管理当前机器人、当前群的永久任务授权，无需固定口令。"
                "action=grant授权，revoke撤销今后新任务授权，list查询。user_id必须是真实平台账号ID，"
                "从用户明确提供的账号、@组件或pc_read_chat_history核对，不能使用昵称或猜测同名账号；"
                "无法确认目标时先澄清。授权不设到期时间且覆盖普通任务能力，不能转授管理权限。"
                "权限只在当前群生效；撤销不打断已接受任务，要停止任务另用pause/cancel。"
                "只有操作回执ok=true才可宣称授权或撤销成功。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["grant", "revoke", "list"]},
                    "user_id": {"type": "string", "minLength": 1, "maxLength": 160},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event)

    async def call(self, context, **kwargs):
        """Apply owner-approved mutations without accepting caller scope fields."""
        rt, event = self.runtime, self.event
        try:
            jsonschema.validate(kwargs, self.parameters)
        except jsonschema.ValidationError:
            return json.dumps({"ok": False, "code": "invalid_arguments"})
        if (
            event not in rt._events
            or rt.scope(event) != self.scope
            or not rt._trusted_event(event)
        ):
            return json.dumps({"ok": False, "code": "untrusted_event"})
        actor = rt.actor(event)
        if not actor.is_primary_user:
            return json.dumps({"ok": False, "code": "primary_user_required"})
        if not actor.group_id:
            return json.dumps(
                {
                    "ok": False,
                    "code": "current_group_required",
                    "hint": "请在要授权的群内提出要求。",
                },
                ensure_ascii=False,
            )
        user = kwargs.get("user_id")
        action = kwargs["action"]
        if action != "list" and (
            not user or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", user)
        ):
            return json.dumps({"ok": False, "code": "real_account_id_required"})
        grants = rt.permissions.list(actor, user_id=user)
        live = [
            grant
            for grant in grants
            if grant.revoked_at is None
            and (grant.expires_at is None or grant.expires_at > rt.service.clock())
        ]
        if action == "grant":
            permanent = next(
                (
                    grant
                    for grant in live
                    if grant.expires_at is None
                    and set(grant.capabilities) == rt.permissions.allowed_capabilities
                    and grant.roots == (rt.workspace.resolve(),)
                ),
                None,
            )
            grant = permanent or rt.permissions.grant(
                actor,
                user,
                capabilities=rt.permissions.allowed_capabilities,
                roots=[rt.workspace],
                expires_at=None,
            )
            result = {
                "ok": True,
                "grant_id": grant.grant_id,
                "user_id": user,
                "expires_at": None,
                "changed": permanent is None,
            }
        elif action == "revoke":
            revoked = [
                grant.grant_id
                for grant in live
                if rt.permissions.revoke(actor, grant.grant_id)
            ]
            result = {
                "ok": True,
                "user_id": user,
                "revoked_grant_ids": revoked,
                "accepted_tasks_unchanged": True,
            }
        else:
            result = {
                "ok": True,
                "grants": [
                    {
                        "grant_id": grant.grant_id,
                        "user_id": grant.user_id,
                        "expires_at": grant.expires_at,
                        "capabilities": sorted(grant.capabilities),
                    }
                    for grant in live
                ],
            }
        result.update(bot_id=actor.bot_id, group_id=actor.group_id)
        return json.dumps(result, ensure_ascii=False)
