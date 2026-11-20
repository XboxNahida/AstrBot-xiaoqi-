"""桌宠设备通道：认证连接、持久回执与事实汇报。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import math
import re
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

TERMINAL = {"succeeded", "failed", "cancelled", "expired", "interrupted"}
DEVICE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")


def _json(value, limit=65536):
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode("utf-8")) > limit:
        raise ValueError("消息过长")
    return text


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("消息标识无效")
    return value


class DesktopLinkServer:
    """使用回环监听供反向代理转发，公开端不提供管理权限。"""

    def __init__(self, data_dir, chat_handler=None, *, rpc_handler=None, summarizer=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data_dir / "desktop-link.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                paired_at REAL NOT NULL, last_seen REAL NOT NULL DEFAULT 0,
                session_id TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, device_id TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL,
                device_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                received_at REAL NOT NULL, facts TEXT NOT NULL, summary TEXT NOT NULL,
                UNIQUE(device_id, id)
            );
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT NOT NULL, device_id TEXT NOT NULL, status TEXT NOT NULL,
                text TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(device_id,id)
            );
        """)
        self.db.execute(
            "UPDATE commands SET status='interrupted',updated_at=? WHERE status IN ('queued','sent','received','running')",
            (time.time(),),
        )
        self.db.execute(
            "UPDATE chats SET status='failed',error='服务重启，上一请求结果未知，请勿自动重发' WHERE status='running'"
        )
        self.db.commit()
        self.chat_handler = chat_handler
        self.sockets = {}
        self.chat_tasks = set()
        self.chat_busy = set()
        self.runner = None
        self.port = None
        self.closed = False
        spec = importlib.util.spec_from_file_location("desktop_extension", Path(__file__).with_name("extension.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.extension = module.Extension(self, rpc_handler, summarizer)
        spec = importlib.util.spec_from_file_location(
            "desktop_control_channel", Path(__file__).with_name("control_channel.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.control = module.DesktopControlChannel(self)
        self.app = web.Application(client_max_size=12 * 1024 * 1024)
        self.app.add_routes(
            [
                web.get("/desktop/health", self._health),
                web.get("/desktop/ws", self._ws),
            ]
        )

    def pair(self, device_id, name):
        if not isinstance(device_id, str) or not DEVICE_RE.fullmatch(device_id):
            raise ValueError("设备标识只允许字母、数字、点、横线和下划线，长度不超过80")
        if device_id in self.sockets:
            raise ValueError("请先断开设备，再重新配对")
        token = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO devices(id,name,token_hash,paired_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,token_hash=excluded.token_hash,paired_at=excluded.paired_at",
            (
                device_id,
                str(name or device_id)[:100],
                hashlib.sha256(token.encode()).hexdigest(),
                time.time(),
            ),
        )
        self.db.commit()
        return {
            "device_id": device_id,
            "name": str(name or device_id)[:100],
            "token": token,
        }

    def devices(self):
        return [
            {
                "device_id": row["id"],
                "name": row["name"],
                "online": self._connected_socket(row["id"]) is not None,
                "online_verified": False,
                "checked_at": time.time(),
                "last_seen": row["last_seen"],
                "paired_at": row["paired_at"],
            }
            for row in self.db.execute("SELECT * FROM devices ORDER BY paired_at")
        ]

    def _connected_socket(self, device_id):
        ws = self.sockets.get(device_id)
        return ws if ws is not None and not ws.closed else None

    def _connection_result(self, device_id, status, **extra):
        return {
            "ok": status == "online", "device_id": device_id,
            "status": status, "checked_at": time.time(),
            "notice": "不可达不等于关机；只有当前状态成功回执证明桌宠可响应。",
            **extra,
        }

    def _finish_probe(self, command_id):
        # 超时/取消后不让旧探测在重连时继续执行；不伪造客户端成功回执。
        self.db.execute(
            "UPDATE commands SET status='cancelled',updated_at=? WHERE id=? "
            "AND status IN ('queued','sent','received','running')",
            (time.time(), command_id),
        )
        self.db.commit()

    async def check_connection(self, device_id, timeout=5):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 5:
            raise ValueError("连接检查等待时间须在0至5秒之间")
        if not self.db.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            return self._connection_result(device_id, "unpaired")
        ws = self._connected_socket(device_id)
        if ws is None:
            return self._connection_result(device_id, "offline")
        command_id = None
        try:
            async with asyncio.timeout(timeout):
                receipt = await self._submit_connected(device_id, "status", {}, timeout, ws)
                command_id = receipt["id"]
                while True:
                    if self._connected_socket(device_id) is not ws:
                        return self._connection_result(device_id, "connection_changed", command_id=command_id)
                    receipt = self.command_status(command_id)
                    if receipt["status"] == "succeeded":
                        return self._connection_result(device_id, "online", command_id=command_id, result=receipt["result"])
                    if receipt["status"] in TERMINAL:
                        state = "probe_timeout" if receipt["status"] == "expired" else "probe_failed"
                        return self._connection_result(device_id, state, command_id=command_id)
                    await asyncio.sleep(.02)
        except TimeoutError:
            return self._connection_result(device_id, "probe_timeout", command_id=command_id)
        except (ConnectionError, RuntimeError):
            return self._connection_result(device_id, "connection_changed", command_id=command_id)
        finally:
            if command_id is not None:
                self._finish_probe(command_id)

    def _expire(self):
        self.db.execute(
            "UPDATE commands SET status='expired',updated_at=? WHERE status IN ('queued','sent') AND expires_at<=?",
            (time.time(), time.time()),
        )
        self.db.commit()

    def command_status(self, command_id):
        self._expire()
        row = self.db.execute(
            "SELECT * FROM commands WHERE id=?", (command_id,)
        ).fetchone()
        if row is None:
            raise ValueError("指令不存在")
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        data["result"] = json.loads(data["result"])
        return data

    async def command(self, device_id, kind, payload, ttl=60):
        if kind not in {"say", "status", "report"}:
            raise ValueError("首版仅支持say、status、report")
        if not isinstance(payload, dict):
            raise TypeError("指令参数须为对象")
        if kind == "say" and (
            not isinstance(payload.get("text"), str)
            or not 0 < len(payload["text"]) <= 4000
        ):
            raise ValueError("消息文字须为1至4000字")
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl)
            or not 0 < ttl <= 86400
        ):
            raise ValueError("有效期须为0至86400秒")
        ws = self._connected_socket(device_id)
        check = await self.check_connection(device_id)
        if not check["ok"]:
            return check
        if kind == "status":
            receipt = self.command_status(check["command_id"])
        elif self._connected_socket(device_id) is not ws:
            return self._connection_result(device_id, "connection_changed")
        else:
            receipt = await self._submit_connected(device_id, kind, payload, ttl, ws)
        return {**receipt, "connection_check": check}

    async def _submit_connected(self, device_id, kind, payload, ttl, ws):
        if ws is None or self._connected_socket(device_id) is not ws:
            raise ConnectionError("设备当前未连接，不排队等待开机")
        now = time.time()
        command_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?)",
            (
                command_id,
                device_id,
                kind,
                _json(payload, 16384),
                "queued",
                "{}",
                now,
                now + ttl,
                now,
            ),
        )
        self.db.commit()
        try:
            await asyncio.wait_for(self._deliver(self.command_status(command_id), ws), timeout=5)
        except BaseException as exc:
            self.db.execute(
                "UPDATE commands SET status='interrupted',updated_at=? WHERE id=? AND status IN ('queued','sent','received')",
                (time.time(), command_id),
            )
            self.db.commit()
            if isinstance(exc, (TimeoutError, ConnectionError, RuntimeError)):
                # 发送可能已到达，保留编号供查询；不能重发或声称已完成。
                return self.command_status(command_id)
            raise
        return self.command_status(command_id)

    async def _deliver(self, command, ws):
        if command["status"] != "queued":
            return
        self.db.execute(
            "UPDATE commands SET status='sent',updated_at=? WHERE id=? AND status='queued'",
            (time.time(), command["id"]),
        )
        self.db.commit()
        try:
            await ws.send_json(
                {
                    "type": "command",
                    "id": command["id"],
                    "kind": command["kind"],
                    "payload": command["payload"],
                    "expires_at": command["expires_at"],
                }
            )
        except (ConnectionError, RuntimeError):
            self.db.execute(
                "UPDATE commands SET status='interrupted',updated_at=? WHERE id=? AND status='sent'",
                (time.time(), command["id"]),
            )
            self.db.commit()

    async def cancel_command(self, command_id):
        data = self.command_status(command_id)
        if data["status"] in TERMINAL:
            return data
        if data["status"] == "queued":
            self.db.execute(
                "UPDATE commands SET status='cancelled',updated_at=? WHERE id=?",
                (time.time(), command_id),
            )
            self.db.commit()
        elif data["device_id"] in self.sockets:
            await self.sockets[data["device_id"]].send_json(
                {"type": "cancel", "id": command_id}
            )
        return self.command_status(command_id)

    def reports(self, device_id, limit=20):
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("汇报数量须为1至100")
        output = []
        for row in self.db.execute(
            "SELECT * FROM reports WHERE device_id=? ORDER BY seq DESC LIMIT ?",
            (device_id, limit),
        ):
            data = dict(row)
            data["facts"] = json.loads(data["facts"])
            data["observed_at"] = json.loads(data["observed_at"])
            output.append(data)
        return output

    def get_session(self, device_id):
        row = self.db.execute(
            "SELECT session_id FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        return row[0] if row else ""

    def set_session(self, device_id, session_id):
        self.db.execute(
            "UPDATE devices SET session_id=? WHERE id=?", (str(session_id), device_id)
        )
        self.db.commit()

    async def _health(self, request):
        return web.json_response({"ok": True, "service": "desktop-link", "protocol": 1})

    async def _ws(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not 20 <= len(auth[7:]) <= 256:
            raise web.HTTPUnauthorized()
        hashed = hashlib.sha256(auth[7:].encode()).hexdigest()
        row = self.db.execute(
            "SELECT id FROM devices WHERE token_hash=?", (hashed,)
        ).fetchone()
        if row is None:
            raise web.HTTPUnauthorized()
        device_id = row[0]
        if request.query.get("device_id", device_id) != device_id:
            raise web.HTTPForbidden()
        if device_id in self.sockets:
            raise web.HTTPConflict(text="设备已有活动连接")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=12 * 1024 * 1024)
        await ws.prepare(request)
        self.sockets[device_id] = ws
        try:
            self._touch(device_id)
            await ws.send_json(
                {
                    "type": "hello",
                    "device_id": device_id,
                    "server_time": time.time(),
                    "protocol": 1,
                    "capabilities": ["records", "qa", "personas", "qa_info"],
                }
            )
            self._expire()
            # 旧版本留下的离线指令也不能在本次重连后突然执行。
            self.db.execute(
                "UPDATE commands SET status='interrupted',updated_at=? WHERE device_id=? AND status='queued'",
                (time.time(), device_id),
            )
            self.db.commit()
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        raise TypeError("消息须为对象")
                    self._touch(device_id)
                    await self._message(device_id, ws, data)
                except (ValueError, TypeError, KeyError, OverflowError):
                    await ws.send_json({"type": "error", "error": "消息格式或状态无效"})
        finally:
            self.control.disconnected(device_id, ws)
            if self.sockets.get(device_id) is ws:
                self.sockets.pop(device_id, None)
                self.db.execute(
                    "UPDATE commands SET status='interrupted',updated_at=? WHERE device_id=? AND status IN ('sent','received','running')",
                    (time.time(), device_id),
                )
                self._touch(device_id)
        return ws

    def _touch(self, device_id):
        self.db.execute(
            "UPDATE devices SET last_seen=? WHERE id=?", (time.time(), device_id)
        )
        self.db.commit()

    async def _message(self, device_id, ws, data):
        kind = data.get("type")
        if kind in {"desktop_tool_hello", "desktop_tool_result"}:
            await self.control.receive(device_id, ws, data)
        elif kind in {"ping", "status", "hello"}:
            await ws.send_json(
                {
                    "type": "pong" if kind == "ping" else "status",
                    "id": data.get("id"),
                    "server_time": time.time(),
                }
            )
        elif kind == "ack":
            command_id = _identifier(data.get("id"))
            status = data.get("status")
            current = self.command_status(command_id)
            if current["device_id"] != device_id or status not in {
                "received",
                "running",
                "succeeded",
                "failed",
                "cancelled",
            }:
                raise ValueError("回执不属于当前设备")
            allowed = {
                "sent": {"received", "running", "succeeded", "failed", "cancelled"},
                "received": {"running", "succeeded", "failed", "cancelled"},
                "running": {"succeeded", "failed", "cancelled"},
                "interrupted": {"succeeded", "failed", "cancelled"},
            }
            if not isinstance(data.get("result", {}), dict):
                raise TypeError("执行结果须为对象")
            result = _json(data.get("result", {}), 16384)
            if status == current["status"]:
                return
            if status not in allowed.get(current["status"], set()):
                raise ValueError("回执状态不可逆")
            self.db.execute(
                "UPDATE commands SET status=?,result=?,updated_at=? WHERE id=?",
                (status, result, time.time(), command_id),
            )
            self.db.commit()
        elif kind == "report":
            if await self.extension.report(device_id, ws, data):
                return
            report_id = _identifier(data.get("id"))
            facts = data.get("facts", {})
            summary = data.get("summary", "")
            observed = data.get("observed_at")
            if (
                not isinstance(facts, dict)
                or not isinstance(summary, str)
                or len(summary) > 8000
            ):
                raise ValueError("汇报内容无效")
            if isinstance(observed, bool) or not isinstance(
                observed, (str, int, float)
            ):
                raise ValueError("汇报缺少采集时间")
            self.db.execute(
                "INSERT OR IGNORE INTO reports(id,device_id,observed_at,received_at,facts,summary) VALUES(?,?,?,?,?,?)",
                (
                    report_id,
                    device_id,
                    _json(observed, 256),
                    time.time(),
                    _json(facts, 32768),
                    summary,
                ),
            )
            self.db.commit()
            await ws.send_json({"type": "report_ack", "id": report_id})
        elif kind == "rpc":
            await self.extension.rpc(device_id, ws, data)
        elif kind == "chat":
            chat_id = _identifier(data.get("id"))
            text = data.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                raise ValueError("聊天内容无效")
            old = self.db.execute(
                "SELECT * FROM chats WHERE device_id=? AND id=?", (device_id, chat_id)
            ).fetchone()
            if old:
                if old["status"] != "running":
                    await ws.send_json(
                        {
                            "type": "chat_result",
                            "id": chat_id,
                            "text": old["text"],
                            "error": old["error"],
                        }
                    )
                return
            if device_id in self.chat_busy:
                await ws.send_json(
                    {
                        "type": "chat_result",
                        "id": chat_id,
                        "text": "",
                        "error": "上一条云端请求尚未结束",
                    }
                )
                return
            self.db.execute(
                "INSERT INTO chats(id,device_id,status) VALUES(?,?,'running')",
                (chat_id, device_id),
            )
            self.db.commit()
            self.chat_busy.add(device_id)
            task = asyncio.create_task(self._chat(device_id, ws, chat_id, text))
            self.chat_tasks.add(task)
            task.add_done_callback(self.chat_tasks.discard)
        else:
            raise ValueError("未知消息类型")

    async def _chat(self, device_id, ws, chat_id, text):
        reply, error = "", ""
        try:
            if self.chat_handler is None:
                raise RuntimeError("未配置云端聊天")
            reply = await asyncio.wait_for(
                self.chat_handler(device_id, text), timeout=180
            )
            if not isinstance(reply, str) or not reply.strip():
                raise ValueError("云端无文字回复")
        except asyncio.CancelledError:
            error = "云端服务停止，请求已取消"
        except Exception:  # noqa: BLE001 -- 用户配置的回调边界只回传固定错误，避免泄露密钥。
            error = "云端聊天失败或超时，请检查服务器日志与模型配置"
        finally:
            self.chat_busy.discard(device_id)
            self.db.execute(
                "UPDATE chats SET status=?,text=?,error=? WHERE device_id=? AND id=?",
                ("failed" if error else "succeeded", reply, error, device_id, chat_id),
            )
            self.db.commit()
        destination = self.sockets.get(device_id, ws)
        if not destination.closed:
            try:
                await destination.send_json(
                    {
                        "type": "chat_result",
                        "id": chat_id,
                        "text": reply,
                        "error": error,
                    }
                )
            except (ConnectionError, RuntimeError):
                pass

    async def start(self, host="127.0.0.1", port=18210):
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("服务只允许回环监听，请通过HTTPS反向代理接入")
        if self.runner is not None:
            return
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, host, port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        self.extension._task(self.extension.maintain())

    async def stop(self):
        if self.closed:
            return
        self.control.close()
        for ws in list(self.sockets.values()):
            await ws.close(code=1001, message=b"service stopped")
        for task in list(self.chat_tasks):
            task.cancel()
        if self.chat_tasks:
            await asyncio.gather(*self.chat_tasks, return_exceptions=True)
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
        self.db.close()
        self.closed = True
