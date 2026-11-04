"""Loopback-only script primitives; no hardcoded interaction scenarios."""

from __future__ import annotations

import asyncio
import copy
import json
import math

import jsonschema
from aiohttp import web

from .interaction_channels import InteractionChannels
from .task_service import TaskScope

CHANNEL = web.RequestKey("interaction_channel", dict)


class InteractionAPI:
    """Expose real observations and explicit operations to owner-bound scripts.

    Args:
        runtime: Existing durable task runtime and permission authority.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.channels = InteractionChannels(
            runtime.service.path, clock=runtime.service.clock
        )
        self.changed = asyncio.Event()
        self.runner = None
        self.base_url = None

        @web.middleware
        async def authorize(request, handler):
            auth = request.headers.get("Authorization", "")
            channel = self.channels.authenticate(
                auth[7:] if auth.startswith("Bearer ") else ""
            )
            if channel is None:
                return web.json_response({"error": "unauthorized"}, status=401)
            if self.runtime.interaction_allowed(channel) is not True:
                return web.json_response({"error": "permission_denied"}, status=403)
            request[CHANNEL] = channel
            try:
                return await handler(request)
            except (ValueError, TypeError, KeyError, jsonschema.ValidationError) as exc:
                code = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and len(str(exc)) < 80
                    and str(exc).replace("_", "").isalnum()
                    else "invalid_request"
                )
                return web.json_response(
                    {"error": code},
                    status=409
                    if code
                    in {
                        "stale_cursor",
                        "state_conflict",
                        "request_key_conflict",
                        "channel_not_open",
                    }
                    else 400,
                )
            except PermissionError:
                return web.json_response({"error": "permission_denied"}, status=403)
            except web.HTTPException:
                raise
            except Exception:
                return web.json_response({"error": "internal_error"}, status=500)

        self.app = web.Application(middlewares=[authorize], client_max_size=262144)
        self.app.add_routes(
            [
                web.get("/v1/events", self.events),
                web.get("/v1/status", self.events),
                web.get("/v1/state", self.state),
                web.post("/v1/state", self.state),
                web.post("/v1/reply", self.reply),
                web.get("/v1/replies/{request_id}", self.reply_status),
                web.post("/v1/{action:stop|resume|close}", self.control),
                web.get("/v1/tasks", self.tasks),
                web.get("/v1/tasks/{task_id}", self.tasks),
                web.get("/v1/history", self.history),
                web.get("/v1/tools", self.tools),
                web.post("/v1/tool-call", self.tools),
                web.post("/v1/scripts", self.scripts),
                web.get("/v1/scripts/{script_id}", self.scripts),
                web.post("/v1/scripts/{script_id}/stop", self.scripts),
            ]
        )

    async def start(self, port=6196):
        """Listen only on loopback; the scoped token never uses dashboard auth.

        Args:
            port: Local API port; zero requests an ephemeral port for tests.
        """
        if self.runner is not None:
            return
        runner = web.AppRunner(self.app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            self.base_url = "http://127.0.0.1:" + str(runner.addresses[0][1])
            self.runner = runner
        except BaseException:
            await runner.cleanup()
            raise

    async def stop(self):
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.base_url = None

    async def events(self, request):
        allowed = {"after_seq", "since", "until", "limit", "wait_seconds"}
        if set(request.query) - allowed:
            raise ValueError("invalid_query")
        options = {
            "after_seq": int(request.query.get("after_seq", 0)),
            "limit": int(request.query.get("limit", 500)),
        }
        for key in ("since", "until"):
            if key in request.query:
                options[key] = float(request.query[key])
        wait = float(request.query.get("wait_seconds", 0))
        if not math.isfinite(wait) or not 0 <= wait <= 30:
            raise ValueError("invalid_wait_seconds")
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            self.changed.clear()
            data = self.channels.events(request[CHANNEL]["id"], **options)
            if data["events"] or asyncio.get_running_loop().time() >= deadline:
                return web.json_response(data)
            try:
                await asyncio.wait_for(
                    self.changed.wait(),
                    min(1, deadline - asyncio.get_running_loop().time()),
                )
            except TimeoutError:
                pass
            refreshed = self.channels.authenticate(request[CHANNEL]["token"])
            if (
                refreshed is None
                or self.runtime.interaction_allowed(refreshed) is not True
            ):
                raise PermissionError("permission_denied")

    async def state(self, request):
        channel = request[CHANNEL]
        if request.method == "POST":
            data = await request.json()
            result = self.channels.set_state(
                channel["id"], data["expected_revision"], data["state"]
            )
        else:
            result = {
                "state": channel["state"],
                "state_revision": channel["state_revision"],
            }
        return web.json_response(result)

    async def reply(self, request):
        data = await request.json()
        if set(data) - {"key", "message", "based_on_seq"}:
            raise ValueError("invalid_reply_fields")
        message = data["message"]
        cleaner = getattr(self.runtime, "_safe_text", None)
        if callable(cleaner) and isinstance(message, str):
            message = cleaner(message, 4000)
        return web.json_response(
            self.channels.request_reply(
                request[CHANNEL]["id"],
                data["key"],
                message,
                based_on_seq=data["based_on_seq"],
            )
        )

    async def reply_status(self, request):
        return web.json_response(
            self.channels.reply_status(
                request[CHANNEL]["id"], request.match_info["request_id"]
            )
        )

    async def control(self, request):
        data = await request.json()
        if set(data) - {"pause"}:
            raise ValueError("invalid_control_fields")
        action = request.match_info["action"]
        result = self.channels.control(
            request[CHANNEL]["id"], action, pause=data.get("pause", True)
        )
        scripts = getattr(self, "script_runner", None)
        if action == "close" and scripts is not None:
            await scripts.stop_channel(request[CHANNEL]["id"])
        self.changed.set()
        return web.json_response(result)

    async def tasks(self, request):
        channel = request[CHANNEL]
        scope = TaskScope(
            channel["bot_id"], channel["session_key"], channel["sender_id"]
        )
        task_id = request.match_info.get("task_id")
        jobs = (
            [self.runtime.service.get(task_id, scope)]
            if task_id
            else self.runtime.service.list(scope, limit=50, latest=True)
        )
        if task_id and jobs[0] is None:
            raise web.HTTPNotFound()
        result = [
            {
                "task_id": j.task_id,
                "goal": j.goal,
                "status": j.status,
                "revision": j.revision,
                "error_kind": j.error_kind,
                "result": j.result,
                **({"steps": j.steps} if task_id else {}),
            }
            for j in jobs
            if j is not None
        ]
        cleaner = getattr(self.runtime, "_safe_text", lambda value, _: value)

        def redact(value):
            # Preserve JSON structure while redacting text at every leaf.
            if isinstance(value, str):
                return cleaner(
                    value.replace(channel["token"], "[redacted]"), len(value)
                )
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            return value

        if not task_id:
            for item in result:
                item["result"] = cleaner(str(item["result"] or ""), 2000)
        return web.json_response({"tasks": redact(result)})

    async def history(self, request):
        if set(request.query) - {"limit"}:
            raise ValueError("invalid_history_query")
        limit = int(request.query.get("limit", 24))
        if not 1 <= limit <= 100:
            raise ValueError("invalid_history_limit")
        channel = request[CHANNEL]
        manager = getattr(self.runtime.plugin.context, "conversation_manager", None)
        messages = []
        if manager is not None:
            cid = await manager.get_curr_conversation_id(channel["session_key"])
            if cid:
                conversation = await manager.get_conversation(
                    channel["session_key"], cid
                )
                history = json.loads(getattr(conversation, "history", "") or "[]")
                cleaner = getattr(self.runtime, "_safe_text", lambda text, _: text)
                for item in history[-limit:]:
                    if not isinstance(item, dict) or item.get("role") not in {
                        "user",
                        "assistant",
                    }:
                        continue
                    content = item.get("content", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            str(p.get("text", ""))
                            for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    if isinstance(content, str):
                        messages.append(
                            {"role": item["role"], "content": cleaner(content, 8000)}
                        )
        return web.json_response({"messages": messages})

    async def tools(self, request):
        channel = request[CHANNEL]
        saved = channel["metadata"].get("request", {}).get("tool_names", [])
        actor = self.runtime.actor(
            self.runtime.restore_event(self.runtime.interaction_job(channel))
        )
        registry = self.runtime._registry(channel["session_key"], actor)
        allowed = {
            name: registry[name]
            for name in saved
            if name in registry and name != "pc_interaction_channel"
        }
        if request.method == "GET":
            return web.json_response(
                {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        }
                        for t in allowed.values()
                    ]
                }
            )
        data = await request.json()
        if (
            set(data) - {"key", "name", "arguments"}
            or not isinstance(data.get("key"), str)
            or not 1 <= len(data["key"]) <= 128
        ):
            raise ValueError("invalid_tool_call")
        tool = allowed.get(data.get("name"))
        if tool is None:
            raise PermissionError("tool_not_allowed")
        arguments = data.get("arguments", {})
        jsonschema.validate(arguments, tool.parameters)
        current = self.runtime.interaction_job(channel)
        if not self.runtime.authorize_tool(current, tool, arguments):
            raise PermissionError("tool_not_allowed")
        metadata = copy.deepcopy(channel["metadata"])
        metadata.update(
            script_channel_id=channel["id"],
            script_tool_call={"name": tool.name, "arguments": arguments},
            silent=True,
        )
        metadata["request"].update(
            mode="tools", tool_names=[tool.name], required_tool_names=[tool.name]
        )
        scope = TaskScope(
            channel["bot_id"], channel["session_key"], channel["sender_id"]
        )
        job = self.runtime.service.submit(
            scope,
            "script:" + channel["id"] + ":" + data["key"],
            "tool",
            "脚本调用工具 " + tool.name,
            current.workdir,
            metadata=metadata,
        )
        if job.metadata.get("script_tool_call") != metadata["script_tool_call"]:
            raise ValueError("request_key_conflict")
        return web.json_response({"task_id": job.task_id, "status": job.status})

    async def scripts(self, request):
        runner = getattr(self, "script_runner", None)
        if runner is None:
            raise web.HTTPServiceUnavailable()
        if request.method == "GET":
            result = runner.status(request[CHANNEL], request.match_info["script_id"])
        elif "script_id" in request.match_info:
            result = await runner.cancel(
                request[CHANNEL], request.match_info["script_id"]
            )
        else:
            data = await request.json()
            result = await runner.schedule(request[CHANNEL], **data)
        return web.json_response(result)
