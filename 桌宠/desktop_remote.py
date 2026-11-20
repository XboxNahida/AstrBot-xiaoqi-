"""复用已鉴权WSS的桌面底层传输；不规划任务、不持久化截图。"""

from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from collections import OrderedDict


class RemoteDesktopTools:
    """绑定连接与云控制会话，在工作线程调用现有external_tool。

    所有异步入口只能由DeviceLink的网络事件循环调用。
    desktop_provider延迟获取已装配的DesktopControl，不另建控制器。
    """

    TOOLS = frozenset(
        {
            "begin",
            "status",
            "stop",
            "monitors",
            "screenshot",
            "controls",
            "move",
            "click",
            "drag",
            "scroll",
            "type",
            "hotkey",
        }
    )
    LIMIT = 10 * 1024 * 1024

    def __init__(self, desktop_provider):
        self.desktop_provider = desktop_provider
        self.connection = None
        self.ready = False
        self.tasks = set()
        self.jobs = {}
        self.seen = OrderedDict()
        self.origins = {}
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.owned = None

    async def connected(self, ws, scope):
        """鉴权成功后协商能力；不自动开启本地授权或桌面会话。"""
        if self.connection:
            await self.disconnected(*self.connection[:2])
        self.connection = (ws, scope, threading.Event())
        self.ready = False
        await ws.send_json({"type": "desktop_tool_hello", "version": 1})

    async def disconnected(self, ws, scope):
        """只停止该连接拥有的云会话，不停止后来的连接或本地任务。"""
        token = self.connection
        if token is None or token[0] is not ws or token[1] != scope:
            return
        token[2].set()
        self.connection = None
        self.ready = False
        self._cancel_jobs(token)
        await asyncio.to_thread(self._release, token)

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _cancel_jobs(self, token):
        for entry in self.jobs.values():
            if entry[0] is token:
                entry[1].set()

    def _release(self, token, expected=None):
        with self.lock:
            if self.owned is None or self.owned[0] is not token:
                return {"ok": True, "status": "stopped"}
            if expected is not None and self.owned is not expected:
                return {"ok": True, "status": "session_changed"}
            _, desktop, identity = self.owned
            self.owned = None
            try:
                state = desktop.external_tool("status", {})
                if (
                    state.get("session_id") == identity
                    and state.get("source") == "cloud"
                ):
                    return desktop.external_tool("stop", {})
            except Exception:  # noqa: BLE001 -- 外部后端异常统一脱敏，停止未知不重放。
                # 不泄漏可能包含窗口内容、文件名或密钥的底层异常。
                return {"ok": False, "code": "stop_unconfirmed"}
            return {"ok": True, "status": "session_changed"}

    async def receive(self, message, ws, scope):
        """处理独立协议；返回False时由原DeviceLink处理普通消息。"""
        kind = message.get("type")
        if kind not in {"desktop_tool_ready", "desktop_tool", "desktop_tool_cancel"}:
            return False
        token = self.connection
        if token is None or token[0] is not ws or token[1] != scope:
            return True
        if kind == "desktop_tool_ready":
            self.ready = type(message.get("version")) is int and message["version"] == 1
            if self.ready:
                print("云端桌面底层工具通道已就绪", flush=True)
            return True
        identity = message.get("id")
        if not isinstance(identity, str) or not 1 <= len(identity) <= 200:
            return True
        if kind == "desktop_tool_cancel":
            entry = self.jobs.get(identity)
            origin = self.origins.get((scope, identity))
            if (entry is not None and entry[0] is token) or (
                origin is not None and origin is self.owned and origin[0] is token
            ):
                self._cancel_jobs(token)
                self._spawn(asyncio.to_thread(self._release, token, self.owned))
            return True
        if not self.ready:
            await self._reply(token, identity, False, {"code": "protocol_not_ready"})
            return True
        now = time.time()
        for old, expiry in list(self.seen.items()):
            if expiry < now:
                self.seen.pop(old)
                self.origins.pop(old, None)
        key = (scope, identity)
        if key in self.seen:
            if identity not in self.jobs:
                await self._reply(token, identity, False, {"code": "duplicate_request"})
            return True
        if len(self.seen) >= 256:
            await self._reply(token, identity, False, {"code": "receipt_capacity"})
            return True
        self.seen[key] = now + 60
        try:
            expiry = message["expires_at"]
            if (
                isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
                or expiry <= now
                or expiry > now + 30
            ):
                raise ValueError("expired")
            tool, payload = message["tool"], message["payload"]
            if (
                not isinstance(tool, str)
                or tool not in self.TOOLS
                or not isinstance(payload, dict)
            ):
                raise ValueError("invalid_arguments")
            if len(json.dumps(payload, allow_nan=False).encode()) > 65536:
                raise ValueError("invalid_arguments")
        except (KeyError, TypeError, ValueError) as exc:
            code = "expired" if str(exc) == "expired" else "invalid_arguments"
            await self._reply(token, identity, False, {"code": code})
            return True
        if tool != "stop" and any(entry[0] is token for entry in self.jobs.values()):
            await self._reply(token, identity, False, {"code": "busy"})
            return True
        cancelled = threading.Event()
        if tool == "stop":
            self._cancel_jobs(token)
        self.jobs[identity] = (token, cancelled)
        self._spawn(self._run(token, identity, tool, dict(payload), expiry, cancelled))
        return True

    def _execute(self, token, tool, payload, expiry, cancelled):
        if token[2].is_set() or cancelled.is_set() or time.time() >= expiry:
            raise ValueError("cancelled")
        if tool == "stop":
            return self._release(token)
        if not self.operation.acquire(blocking=False):
            raise ValueError("busy")
        try:
            with self.lock:
                if token[2].is_set() or cancelled.is_set() or time.time() >= expiry:
                    raise ValueError("cancelled")
                desktop = self.desktop_provider()
                if desktop is None:
                    raise ValueError("desktop_not_ready")
                def invoke():
                    contextual = getattr(desktop, "external_tool_context", None)
                    if callable(contextual):
                        return contextual(tool, payload, cancelled, expiry)
                    return desktop.external_tool(tool, payload)
                if tool == "begin":
                    if self.owned:
                        raise ValueError("busy")
                    result = invoke()
                    self.owned = (token, desktop, result["session_id"])
                    if token[2].is_set() or cancelled.is_set() or time.time() >= expiry:
                        self._release(token)
                        raise ValueError("cancelled")
                    return result
                if tool != "status":
                    if (
                        self.owned is None
                        or self.owned[0] is not token
                        or self.owned[1] is not desktop
                    ):
                        raise ValueError("no_control_session")
                    state = desktop.external_tool("status", {})
                    if (
                        not state.get("active")
                        or state.get("source") != "cloud"
                        or state.get("session_id") != self.owned[2]
                    ):
                        self.owned = None
                        raise ValueError("session_changed")
            # 不持有会话锁执行耗时采集/输入，使stop可立即释放键鼠。
            if token[2].is_set() or cancelled.is_set() or time.time() >= expiry:
                raise ValueError("cancelled")
            return invoke()
        finally:
            self.operation.release()

    async def _run(self, token, identity, tool, payload, expiry, cancelled):
        try:
            result = await asyncio.to_thread(
                self._execute, token, tool, payload, expiry, cancelled
            )
            ok = isinstance(result, dict) and result.get("ok", True) is not False
            if not isinstance(result, dict):
                result = {"code": "invalid_result"}
            elif not ok and "code" not in result:
                result = {**result, "code": str(result.get("status", "desktop_error"))}
            if not cancelled.is_set() and time.time() < expiry:
                await self._reply(token, identity, ok, result)
            elif tool == "begin":
                await asyncio.to_thread(self._release, token)
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.to_thread(self._release, token)
            raise
        except Exception as exc:  # noqa: BLE001 -- 网络边界只回传已知错误码。
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                and re.fullmatch(r"[a-z_]{1,64}", str(exc))
                else "desktop_error"
            )
            if not cancelled.is_set():
                await self._reply(token, identity, False, {"code": code})
        finally:
            if self.owned is not None and self.owned[0] is token:
                self.origins[(token[1], identity)] = self.owned
            self.jobs.pop(identity, None)

    async def _reply(self, token, identity, ok, result):
        if self.connection is not token or token[2].is_set() or token[0].closed:
            return
        message = {
            "type": "desktop_tool_result",
            "id": identity,
            "ok": ok,
            "result": result,
        }
        limit = self.LIMIT if "image_b64" in result else 256 * 1024
        try:
            size = len(
                json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
            )
            if size > limit:
                message = {
                    "type": "desktop_tool_result",
                    "id": identity,
                    "ok": False,
                    "result": {"code": "result_too_large"},
                }
            await token[0].send_json(message)
        except (ConnectionError, RuntimeError, OSError, ValueError, TypeError):
            # 未知投递不排队、不重发；断线清理由DeviceLink调用。
            pass
