"""Send model-composed group text and real member mentions to the bound session."""

from __future__ import annotations

import json
import re

import jsonschema

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain


class GroupMessageTool(FunctionTool):
    """Keep ordinary mentions independent of task execution authorization."""

    def __init__(self, runtime, event):
        super().__init__(
            name="pc_send_group_message",
            description=(
                "按需向当前群发送一条含真实@的回复。parts按顺序组合text文字和at用户，可@主要用户或普通群友，"
                "也可一条中@多位群友。at.user_id必须来自本轮@、历史记录或宿主权限事实的真实平台账号，"
                "不能用昵称猜账号；不支持@全体，不改变任何人权限。是否@及措辞由当前人格与语境决定，"
                "例如无任务权限时可解释、请其找主要用户，或选择@主要用户协助。"
                "这是实际发送而非草稿；already_sent=true后不要再次输出同一内容；失败可以正常文字说明。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "parts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 40,
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["text", "at"]},
                                "text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 4000,
                                },
                                "user_id": {
                                    "type": "string",
                                    "pattern": "^[A-Za-z0-9_.:@-]{1,160}$",
                                },
                            },
                            "required": ["type"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["parts"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event, self.scope = runtime, event, runtime.scope(event)

    async def call(self, context, **kwargs):
        """Send one ordered message without granting task or administration rights.

        Args:
            context: Standard frontend tool context.
            **kwargs: Ordered text and mention parts, with no destination fields.

        Returns:
            The actual adapter result and whether this message was already sent.
        """
        rt, event = self.runtime, self.event
        try:
            jsonschema.validate(kwargs, self.parameters)
            if (
                event not in rt._events
                or rt.scope(event) != self.scope
                or not rt._trusted_event(event)
            ):
                raise PermissionError("untrusted_event")
            if not event.get_group_id():
                raise ValueError("current_group_required")
            chain = []
            for part in kwargs["parts"]:
                if set(part) != {
                    "type",
                    "text" if part["type"] == "text" else "user_id",
                }:
                    raise ValueError("part_fields_do_not_match_type")
                if part["type"] == "text":
                    chain.append(Plain(rt._safe_text(part["text"], 4000)))
                else:
                    user = part["user_id"]
                    if user.casefold() in {
                        "all",
                        "@all",
                        "everyone",
                        "here",
                        "0",
                    } or not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,160}", user):
                        raise ValueError("individual_account_required")
                    chain.append(At(qq=user))
            sent = await rt.plugin.context.send_message(
                self.scope.session_key, MessageChain(chain)
            )
            return json.dumps(
                {
                    "ok": sent is True,
                    "already_sent": sent is True,
                    "receipt_level": "adapter",
                }
            )
        except Exception as exc:
            return json.dumps(
                {"ok": False, "already_sent": False, "code": type(exc).__name__}
            )
