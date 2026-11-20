"""客户端主动连接云端，持久回执去重和报告补发。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future
from pathlib import Path

import aiohttp
from PyQt6.QtCore import QObject, pyqtSignal

from companion_config import validate_endpoint
from desktop_remote import RemoteDesktopTools


class CommandJournal:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.scope = ""
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,status TEXT,result TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT)"
        )
        self.db.execute(
            "UPDATE commands SET status='failed',result=? WHERE status IN ('received','running')",
            (
                json.dumps(
                    {"error": "客户端上次执行中断，结果未确认，不会重复执行"},
                    ensure_ascii=False,
                ),
            ),
        )
        self.db.commit()

    def _key(self, message_id):
        return self.scope + "\x1f" + message_id

    def begin(self, message):
        with self.lock:
            row = self.db.execute(
                "SELECT status,result FROM commands WHERE id=?",
                (self._key(message["id"]),),
            ).fetchone()
            if row:
                return {
                    "id": message["id"],
                    "status": row[0],
                    "result": json.loads(row[1]),
                }
            try:
                expiry = float(message.get("expires_at", 0))
                expired = not math.isfinite(expiry) or expiry <= time.time()
            except (TypeError, ValueError):
                expired = True
            status, result = (
                ("failed", {"error": "指令已过期"}) if expired else ("received", {})
            )
            self.db.execute(
                "INSERT INTO commands VALUES(?,?,?)",
                (
                    self._key(message["id"]),
                    status,
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            self.db.commit()
            return (
                {"id": message["id"], "status": status, "result": result}
                if expired
                else None
            )

    def complete(self, command_id, status, result):
        with self.lock:
            self.db.execute(
                "UPDATE commands SET status=?,result=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False), self._key(command_id)),
            )
            self.db.commit()

    def queue_report(self, message):
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO outbox VALUES(?,?)",
                (self._key(message["id"]), json.dumps(message, ensure_ascii=False)),
            )
            self.db.execute(
                "DELETE FROM outbox WHERE json_extract(body,'$.facts._record_type') IS NULL "
                "AND rowid NOT IN (SELECT rowid FROM outbox WHERE json_extract(body,'$.facts._record_type') IS NULL ORDER BY rowid DESC LIMIT 1000)"
            )
            now = time.time()
            day = ((int(now) + 28800) // 86400) * 86400 - 28800
            self.db.execute("DELETE FROM outbox WHERE (json_extract(body,'$.facts._record_type')='qa_message' AND json_extract(body,'$.observed_at')<?) "
                            "OR (json_extract(body,'$.facts._record_type')='observation' AND json_extract(body,'$.observed_at')<?)", (day, now-172800))
            self.db.commit()

    def reports(self, include_records=True):
        with self.lock:
            return [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT body FROM outbox WHERE substr(id,1,?)=? "
                    "AND (? OR json_extract(body,'$.facts._record_type') IS NULL) ORDER BY rowid LIMIT 100",
                    (len(self.scope) + 1, self.scope + "\x1f", include_records),
                )
            ]

    def acknowledge_report(self, report_id):
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE id=?", (self._key(report_id),))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


class DeviceLink(QObject):
    status_changed = pyqtSignal(str)
    command_received = pyqtSignal(dict)
    report_sent = pyqtSignal(str)

    @staticmethod
    def scope_for(cloud):
        return hashlib.sha256(
            json.dumps(
                [
                    cloud.get("url", ""),
                    cloud.get("device_id", ""),
                    cloud.get("token", ""),
                ]
            ).encode()
        ).hexdigest()

    def __init__(self, state_path=None, parent=None):
        super().__init__(parent)
        self.journal = CommandJournal(
            state_path or Path(__file__).with_name("companion_state.sqlite")
        )
        self.connected = False
        self.thread = None
        self.loop = None
        self.run_task = None
        self.ws = None
        self.stopping = threading.Event()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.config = {}
        self.capabilities = set()
        self.desktop = None
        self.desktop_remote = RemoteDesktopTools(lambda: self.desktop)

    def start(self, cloud):
        self.stop()
        self.config = dict(cloud)
        self.capabilities = set()
        self.journal.scope = self.scope_for(cloud)
        if not cloud.get("url") or not cloud.get("token"):
            self.status_changed.emit("未配对：本机模型仍可独立使用")
            return
        validate_endpoint(cloud["url"], websocket=True)
        self.stopping.clear()
        self.thread = threading.Thread(
            target=self._thread_main, name="桌宠云端连接", daemon=True
        )
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.loop and self.loop.is_running() and self.run_task:
            self.loop.call_soon_threadsafe(self.run_task.cancel)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=4)
            if self.thread.is_alive():
                raise ValueError("旧连接正在停止，请稍后再保存")
        self.thread = None
        self.loop = None
        self.run_task = None
        self.connected = False

    def _thread_main(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        finally:
            self.connected = False
            self.status_changed.emit("云端连接已停止")

    async def _run(self):
        self.loop = asyncio.get_running_loop()
        self.run_task = asyncio.current_task()
        delay = 1
        while not self.stopping.is_set():
            self.status_changed.emit("正在连接云端…")
            try:
                timeout = aiohttp.ClientTimeout(total=12)
                async with (
                    aiohttp.ClientSession(timeout=timeout, trust_env=True) as session,
                    session.ws_connect(
                        self.config["url"],
                        headers={"Authorization": "Bearer " + self.config["token"]},
                        heartbeat=25,
                        max_msg_size=12 * 1024 * 1024,
                    ) as ws,
                ):
                    self.ws = ws
                    hello = await ws.receive_json(timeout=8)
                    if (
                        hello.get("type") != "hello"
                        or hello.get("device_id") != self.config["device_id"]
                    ):
                        raise ValueError("配对设备不一致")
                    self.connected = True
                    self.capabilities = set(hello.get('capabilities', []))
                    self.status_changed.emit("已连接云端")
                    await self.desktop_remote.connected(ws, self.journal.scope)
                    delay = 1
                    pump = asyncio.create_task(self._pump_reports(ws))
                    try:
                        async for packet in ws:
                            if packet.type == aiohttp.WSMsgType.TEXT:
                                message = json.loads(packet.data)
                                await self._receive(message)
                            elif packet.type == aiohttp.WSMsgType.ERROR:
                                break
                    finally:
                        pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError, KeyError):
                if not self.stopping.is_set():
                    self.status_changed.emit("云端未连接，将自动重试；可切换本机直连")
            finally:
                if self.ws is not None:
                    await self.desktop_remote.disconnected(self.ws, self.journal.scope)
                self.connected = False
                self.ws = None
                with self.pending_lock:
                    futures = list(self.pending.values())
                    self.pending.clear()
                for future in futures:
                    if not future.done():
                        future.set_exception(
                            ValueError("云端连接已断开，本次回复未确认")
                        )
            if not self.stopping.is_set():
                for _ in range(delay * 10):
                    if self.stopping.is_set():
                        break
                    await asyncio.sleep(0.1)
                delay = min(15, delay * 2)
        self.status_changed.emit("云端连接已停止")

    async def _pump_reports(self, ws):
        while not self.stopping.is_set():
            for report in self.journal.reports(include_records='records' in self.capabilities):
                if report.get('facts', {}).get('_record_type') and 'records' not in self.capabilities:
                    continue
                await ws.send_json(report)
            await asyncio.sleep(5)

    async def _receive(self, message):
        if await self.desktop_remote.receive(message, self.ws, self.journal.scope):
            return
        kind = message.get("type")
        if kind == "hello":
            if message.get("device_id") != self.config.get("device_id"):
                await self.ws.close()
                self.status_changed.emit("配对设备不一致，请重新配对")
        elif kind == "command":
            if not isinstance(message.get("id"), str):
                return
            cached = self.journal.begin(message)
            if cached:
                await self.ws.send_json({"type": "ack", **cached})
                return
            await self.ws.send_json(
                {"type": "ack", "id": message["id"], "status": "received", "result": {}}
            )
            self.journal.complete(message["id"], "running", {})
            await self.ws.send_json(
                {"type": "ack", "id": message["id"], "status": "running", "result": {}}
            )
            self.command_received.emit({**message, "_scope": self.journal.scope})
        elif kind == "report_ack":
            self.journal.acknowledge_report(message.get("id", ""))
            self.report_sent.emit(message.get("id", ""))
        elif kind in ("chat_result", "rpc_result"):
            with self.pending_lock:
                future = self.pending.pop(message.get("id"), None)
            if future and not future.done():
                if message.get("error"):
                    future.set_exception(ValueError(str(message["error"])[:180]))
                elif kind == 'rpc_result' and isinstance(message.get('result'), dict):
                    future.set_result(message['result'])
                elif isinstance(message.get("text"), str) and message["text"].strip():
                    future.set_result(message["text"])
                else:
                    future.set_exception(ValueError("云端没有返回文字"))

    def _send(self, message, expected_scope=None):
        loop, ws = self.loop, self.ws
        scope = self.journal.scope if expected_scope is None else expected_scope
        if not self.connected or not loop or not ws or scope != self.journal.scope:
            return False

        async def send_bound():
            if (
                not self.connected
                or self.stopping.is_set()
                or self.loop is not loop
                or self.ws is not ws
                or self.journal.scope != scope
            ):
                raise ValueError("云端连接已改变，本次请求未发送")
            if message.get('facts', {}).get('_record_type') and 'records' not in self.capabilities:
                return  # 新记录留在本地待发，不能被旧插件写进永久旧汇报。
            await ws.send_json(message)

        sending = send_bound()
        try:
            return asyncio.run_coroutine_threadsafe(sending, loop)
        except RuntimeError:
            sending.close()
            return False

    def complete(self, command_id, status, result):
        self.journal.complete(command_id, status, result)
        return self._send(
            {"type": "ack", "id": command_id, "status": status, "result": result}
        )

    def report(self, facts, summary="", identity=None):
        message = {
            "type": "report",
            "id": identity or uuid.uuid4().hex,
            "observed_at": facts.get("observed_at", time.time()),
            "facts": facts,
            "summary": summary,
        }
        self.journal.queue_report(message)
        self._send(message)
        return message["id"]

    def rpc(self, op, payload, expected_scope=None):
        scope = self.journal.scope if expected_scope is None else expected_scope
        if not self.connected or scope != self.journal.scope:
            raise ValueError('云端未连接，本地答疑仍可使用')
        if op not in self.capabilities:
            raise ValueError('云端插件需要安装本次新版安装包')
        request_id, future = uuid.uuid4().hex, Future()
        message = {'type': 'rpc', 'id': request_id, 'op': op, 'payload': payload}
        if len(json.dumps(message).encode('utf-8')) > 11 * 1024 * 1024:
            raise ValueError('本轮图文上下文超过11MiB，请开启新对话；原历史仍保留')
        with self.pending_lock:
            self.pending[request_id] = future
        try:
            sending = self._send(message, expected_scope=scope)
            if not sending:
                raise ValueError('连接改变，请求未发送')
            sending.result(timeout=12)
            return future.result(timeout=135)
        except TimeoutError as exc:
            raise ValueError('云端答疑超时，请手动重试') from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def chat(self, text, expected_scope=None):
        scope = self.journal.scope if expected_scope is None else expected_scope
        if not self.connected or scope != self.journal.scope:
            raise ValueError("云端尚未连接，请先配对或切换本机直连")
        request_id = uuid.uuid4().hex
        future = Future()
        with self.pending_lock:
            self.pending[request_id] = future
        try:
            sending = self._send(
                {"type": "chat", "id": request_id, "text": text},
                expected_scope=scope,
            )
            if not sending:
                raise ValueError("云端连接已改变，本次请求未发送")
            sending.result(timeout=12)
            return future.result(timeout=120)
        except TimeoutError as exc:
            raise ValueError("云端回复等待超时，本次未确认完成") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)
