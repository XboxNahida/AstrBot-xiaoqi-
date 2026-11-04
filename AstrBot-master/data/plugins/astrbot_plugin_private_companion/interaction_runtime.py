"""Bind script API primitives to trusted task identity and normal chat delivery."""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

from astrbot.api import logger
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .bot_identity import ACTIVE_BOT_IDENTITY, bind_event_bot_identity
from .conversation_route import _task_chat_text
from .task_execution import actor_workspace
from .task_service import TaskScope


class ScriptInteractionMixin:
    """Expose primitive responses; scripts own timing, conditions and decisions."""

    def interaction_job(self, channel):
        scope = TaskScope(
            channel["bot_id"], channel["session_key"], channel["sender_id"]
        )
        job = SimpleNamespace(
            scope=scope,
            metadata=copy.deepcopy(channel["metadata"]),
            task_id=channel["metadata"].get("source_task_id", channel["id"]),
            origin_message_id=channel["metadata"].get(
                "origin_message_id", "script:" + channel["id"]
            ),
            goal="用户授权脚本的互动",
            steps={},
            attempts=0,
            status="running",
            step_key="script",
        )
        job.workdir = str(
            actor_workspace(self.workspace, self.actor(self.restore_event(job)))
        )
        return job

    def interaction_actor(self, channel):
        return self.actor(self.restore_event(self.interaction_job(channel)))

    def interaction_allowed(self, channel):
        try:
            if not bool(getattr(self.plugin, "enabled", True)):
                return False
            job = self.interaction_job(channel)
            event = self.restore_event(job)
            checker = getattr(self.plugin, "_bot_scope_allows_event", None)
            if callable(checker) and checker(event) is not True:
                return False
            actor = self.actor(event)
            return all(
                self.permissions.check(actor, capability, job.workdir)
                for capability in ("schedule", "read", "write")
            )
        except Exception:
            return False

    async def prepare_interaction_config(self, channel):
        actor = self.interaction_actor(channel)
        if not self.interaction_allowed(channel) or not self.interaction_api.base_url:
            raise PermissionError("interaction_api_unavailable")
        relative = ".interaction-" + channel["id"] + ".json"
        config = {
            "base_url": self.interaction_api.base_url,
            "channel_id": channel["id"],
            "token": channel["token"],
        }
        result = await self.get_execution_broker(actor).write_text(
            actor, relative, json.dumps(config, ensure_ascii=False)
        )
        if result.exit_code != 0:
            raise PermissionError("interaction_config_write_failed")
        return actor_workspace(self.workspace, actor) / relative

    def observe_script_interactions(self, event):
        if not self._trusted_event(event):
            return 0
        inbound = getattr(self.plugin, "_event_is_inbound_chat_message", None)
        if callable(inbound) and not inbound(event):
            return 0
        known = getattr(self.plugin, "_known_bot_self_ids", None)
        if callable(known) and event.get_sender_id() in known():
            return 0
        checker = getattr(self.plugin, "_bot_scope_allows_event", None)
        if not bool(getattr(self.plugin, "enabled", True)) or (
            callable(checker) and checker(event) is not True
        ):
            return 0
        count = self.interaction_api.channels.observe(
            self.scope(event),
            str(event.message_obj.message_id),
            self._safe_text(event.message_str, 16000),
            [type(part).__name__ for part in event.message_obj.message],
            self.service.clock(),
        )
        if count:
            self.interaction_api.changed.set()
        return count

    def make_interaction_tool(self):
        existing = getattr(self, "_script_channel_tool", None)
        if existing is not None:
            return existing
        runtime = self

        class ChannelTool(FunctionTool):
            async def call(self, context, **kwargs):
                job = getattr(
                    context.context.event, "_private_companion_script_source_job", None
                )
                return json.dumps(
                    await runtime.call_interaction_tool(job, kwargs), ensure_ascii=False
                )

        self._script_channel_tool = ChannelTool(
            name="pc_interaction_channel",
            description="Manage script API channels bound to the current real bot/session/user. List existing channels and list_scripts before creating replacements. Open creates a channel; renew explicitly changes an owned channel lifetime or cancel_on_input policy, preserving its ID and state, but never reopens a closed channel. New channels default cancel_on_input=false: scripts decide whether new input stops future speech; select true to invalidate pending replies when any new input arrives. Every action except open/list requires channel_id. Write your own Python/PowerShell script for timing, conditions and stopping. run returns a durable start receipt, not completion. The Windows broker currently limits each process job to 3600 seconds; choose state checkpoints and later invocations for longer flows. Scripts receive the private config and shared SDK automatically. Credentials stay isolated.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "open",
                            "list",
                            "renew",
                            "list_scripts",
                            "read",
                            "stop",
                            "resume",
                            "close",
                            "run",
                            "script_status",
                            "stop_script",
                        ],
                    },
                    "key": {
                        "type": "string",
                        "description": "Stable per-task operation key for open/run retries.",
                    },
                    "channel_id": {"type": "string"},
                    "ttl_seconds": {"type": "number", "exclusiveMinimum": 0, "description": "Lifetime from now; required for renew. Must cover future script start times."},
                    "cancel_on_input": {"type": "boolean", "description": "Invalidate pending replies on any new input. New channels default false; omitted renew keeps existing policy."},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "after_seq": {"type": "integer", "minimum": 0},
                    "pause": {"type": "boolean"},
                    "path": {
                        "type": "string",
                        "description": "Existing authored .py/.ps1 relative path in the task workspace.",
                    },
                    "arguments": {"type": "array", "items": {"type": "string"}},
                    "run_at": {
                        "type": ["string", "number", "null"],
                        "description": "Future UTC epoch or ISO datetime with timezone; null starts promptly.",
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 3600,
                        "description": "Process job timeout enforced by the existing Windows broker, not channel lifetime.",
                    },
                    "script_id": {"type": "string"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )
        return self._script_channel_tool

    def script_interaction_snapshot(self, event):
        channels = [
            c
            for c in self.interaction_api.channels.list(self.scope(event))
            if c["status"] != "closed" and c["expires_at"] > self.service.clock()
        ]
        return json.dumps(channels[:6], ensure_ascii=False) if channels else ""

    async def call_interaction_tool(self, job, args):
        if job is None or not self.service.heartbeat(job):
            return {"success": False, "code": "live_task_required"}
        api = self.interaction_api
        action = args.get("action")
        try:
            actor = self.actor(self.restore_event(job))
            if not self.permissions.check(actor, "schedule", job.workdir):
                raise PermissionError("permission_denied")
            if action == "list":
                return {"success": True, "channels": api.channels.list(job.scope)}
            if action == "open":
                if not api.base_url:
                    return {"success": False, "code": "interaction_api_unavailable"}
                await self.ensure_workspace(job)
                metadata = {
                    **copy.deepcopy(job.metadata),
                    "source_task_id": job.task_id,
                }
                c = api.channels.open(
                    job.scope,
                    job.task_id + ":" + str(args.get("key", "main")),
                    metadata,
                    ttl_seconds=args.get("ttl_seconds", 86400),
                    cancel_on_input=args.get("cancel_on_input", False),
                )
                path = await self.prepare_interaction_config(c)
                return {
                    "success": True,
                    "channel_id": c["id"],
                    "config_path": str(path),
                    "sdk_path": str(self.workspace / "tools/astrbot_interaction.py"),
                    "expires_at": c["expires_at"],
                    "cancel_on_input": bool(c["cancel_on_input"]),
                    "execution_timeout_limit": 3600,
                    "api": api.base_url,
                    "usage": "from astrbot_interaction import InteractionClient; client=InteractionClient.from_env(). Scripts launched by action=run receive config and shared SDK automatically. Use events/iter_events/collect for input, reply + reply_status for speech, stop_replies/resume_replies/close for control, state/set_state for checkpoints, tasks/history/tools/call_tool for real data. reply pending means queued, not delivered. Preserve stable operation keys across retries.",
                }
            if not args.get("channel_id"):
                return {
                    "success": False, "code": "channel_id_required", "recoverable": True,
                    "hint": "Call action=list first. If no channel exists, call action=open key=<stable-key>; then pass its returned channel_id to read/run/stop/resume/close. This is a missing argument, not denied proactive communication permission.",
                }
            if action == "run" and not args.get("path"):
                return {"success": False, "code": "script_path_required", "recoverable": True, "hint": "Write your .py/.ps1 script in your workspace, then pass its relative path and channel_id to action=run."}
            if action in {"script_status", "stop_script"} and not args.get("script_id"):
                return {"success": False, "code": "script_id_required", "recoverable": True, "hint": "Use the script_id returned by action=run or the channel's script list."}
            c = api.channels.get(job.scope, args.get("channel_id"), include_inactive=True)
            if c is None or not self.interaction_allowed(c):
                raise PermissionError("channel_not_owned")
            if action == "renew":
                if "ttl_seconds" not in args:
                    raise ValueError("ttl_seconds_required")
                renewed = api.channels.renew(
                    job.scope, c["id"], ttl_seconds=args["ttl_seconds"],
                    cancel_on_input=args.get("cancel_on_input"),
                )
                return {"success": True, "channel_id": renewed["id"],
                        "expires_at": renewed["expires_at"], "status": renewed["status"],
                        "cancel_on_input": bool(renewed["cancel_on_input"])}
            if action == "list_scripts":
                offset, limit = args.get("offset", 0), args.get("limit", 50)
                rows = api.script_runner.list(c, offset=offset, limit=limit)
                return {"success": True, "channel_id": c["id"], "scripts": rows,
                        "next_offset": offset + len(rows) if len(rows) == limit else None}
            if action not in {"script_status", "stop_script", "stop", "close"}:
                if c["status"] == "closed":
                    raise ValueError("channel_closed")
                if c["expires_at"] <= api.channels.clock():
                    return {"success": False, "code": "channel_expired",
                            "expires_at": c["expires_at"], "recoverable": True,
                            "hint": "Inspect list_scripts, then use action=renew with ttl_seconds if the current authorized plan needs more time. Reuse the existing channel ID."}
            if action == "read":
                return {
                    "success": True,
                    **api.channels.events(c["id"], after_seq=args.get("after_seq", 0)),
                }
            if action in {"stop", "resume", "close"}:
                result = api.channels.control(
                    c["id"], action, pause=args.get("pause", True)
                )
                if action == "close":
                    await api.script_runner.stop_channel(c["id"])
                return {"success": True, **result}
            if action == "run":
                result = await api.script_runner.schedule(
                    c,
                    path=args["path"],
                    key=job.task_id + ":" + str(args.get("key", "main")),
                    arguments=args.get("arguments"),
                    run_at=args.get("run_at"),
                    timeout=args.get("timeout", 600),
                )
                return {"success": True, **result}
            if action == "script_status":
                return {
                    "success": True,
                    **api.script_runner.status(c, args["script_id"]),
                }
            if action == "stop_script":
                return {
                    "success": True,
                    **await api.script_runner.cancel(c, args["script_id"]),
                }
            raise ValueError("invalid_action")
        except (PermissionError, ValueError, KeyError, TypeError) as exc:
            code = str(exc)
            if not code or len(code) > 70 or not code.replace("_", "").isalnum():
                code = type(exc).__name__
            return {"success": False, "code": code,
                    "recoverable": not isinstance(exc, PermissionError),
                    "hint": "Inspect the error and existing channel/script receipts, then decide whether to correct the request, stop, or ask the user. Current identity and authorization remain required.",
                    **getattr(exc, "details", {})}

    async def send_script_reply_once(self):
        reply = self.interaction_api.channels.claim_reply()
        if reply is None:
            return False
        channel = reply["channel"]
        token = identity_token = None
        try:
            if not self.interaction_allowed(channel):
                self.interaction_api.channels.finish_reply(reply, "failed")
                return True
            current = self.interaction_job(channel)
            event = self.restore_event(current)
            token, _ = await self.plugin._activate_persona_for_event_context(event)
            identity_token = await bind_event_bot_identity(self.plugin, event)
            notification = SimpleNamespace(
                kind="interaction",
                payload={
                    "message": reply["message"],
                    "observed_message_seq": reply["based_on_seq"],
                },
            )
            text = _task_chat_text(
                await self.render_notification(notification, current)
            )
            if not text or not self.interaction_allowed(channel):
                self.interaction_api.channels.finish_reply(reply, "failed")
                return True
            if not self.interaction_api.channels.begin_reply(reply):
                return True
            sent = await self.plugin.context.send_message(
                channel["session_key"],
                MessageChain(chain=[Plain(self._safe_text(text, 4500))]),
            )
            self.interaction_api.channels.finish_reply(
                reply, "sent" if sent is True else "unknown"
            )
            if sent is True:
                manager = getattr(self.plugin.context, "conversation_manager", None)
                cid = current.metadata.get("chat_context", {}).get("conversation_id")
                if manager is not None and cid:
                    try:
                        if (
                            await manager.get_curr_conversation_id(
                                channel["session_key"]
                            )
                            == cid
                        ):
                            await manager.add_message_pair(
                                cid=cid,
                                user_message={
                                    "role": "user",
                                    "content": "（此前授权脚本的后续互动，无新增用户要求）",
                                },
                                assistant_message={
                                    "role": "assistant",
                                    "content": self._safe_text(text, 4500),
                                },
                            )
                    except Exception as exc:
                        logger.warning(
                            "Sent script chat archive failed: %s", type(exc).__name__
                        )
        except asyncio.CancelledError:
            self.interaction_api.channels.finish_reply(reply, "unknown")
            raise
        except Exception as exc:
            self.interaction_api.channels.finish_reply(reply, "unknown")
            logger.warning("Script reply failed: %s", type(exc).__name__)
        finally:
            if identity_token is not None:
                ACTIVE_BOT_IDENTITY.reset(identity_token)
            self.plugin._deactivate_persona_for_event(token)
        return True
