"""Transient desktop primitives over an already authenticated device socket."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import time
import uuid
from collections import OrderedDict

import jsonschema
from PIL import Image

MAX_MESSAGE_BYTES = 10 * 1024 * 1024
INPUTS = {"move", "click", "drag", "scroll", "type", "hotkey"}
TOOLS = INPUTS | {"begin", "status", "stop", "monitors", "screenshot", "controls"}
KEYS = r"^(ctrl|alt|shift|win|enter|tab|esc|space|backspace|delete|left|right|up|down|home|end|[a-zA-Z0-9]|F([1-9]|1[0-9]|2[0-4]))$"


def validate_call(tool, payload):
    """Validate primitive arguments before any transmission.

    Args:
        tool: A documented primitive name.
        payload: A JSON object with no caller identity or executable code.

    Raises:
        ValueError: The name or arguments are invalid.
    """
    if not isinstance(tool, str) or tool not in TOOLS or not isinstance(payload, dict):
        raise ValueError("invalid_arguments")
    props, required = {}, []
    if tool in INPUTS:
        props.update(
            {
                name: {"type": "string", "minLength": 1, "maxLength": 200}
                for name in ("request_id", "observation_id")
            }
        )
        required.extend(props)
    if tool in {"move", "click", "drag", "scroll"}:
        props.update({name: {"type": "integer"} for name in ("x", "y")})
        required.extend(("x", "y"))
    if tool in {"click", "drag"}:
        props["button"] = {"enum": ["left", "right", "middle"]}
    if tool == "click":
        props["count"] = {"type": "integer", "minimum": 1, "maximum": 3}
    elif tool == "drag":
        props.update({name: {"type": "integer"} for name in ("to_x", "to_y")})
        props["duration"] = {"type": "number", "minimum": 0, "maximum": 5}
        required.extend(("to_x", "to_y"))
    elif tool == "scroll":
        props["delta"] = {"type": "integer", "minimum": -12000, "maximum": 12000}
        required.append("delta")
    elif tool == "type":
        props["text"] = {"type": "string", "maxLength": 10000}
        required.append("text")
    elif tool == "hotkey":
        props["keys"] = {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {"type": "string", "pattern": KEYS},
        }
        required.append("keys")
    elif tool == "screenshot":
        props = {
            "original": {"type": "boolean"},
            "monitor_id": {"type": ["string", "integer"]},
            "region": {
                "type": "object",
                "properties": {
                    name: {
                        "type": "integer",
                        **({"minimum": 1} if name in {"width", "height"} else {}),
                    }
                    for name in ("left", "top", "width", "height")
                },
                "required": ["left", "top", "width", "height"],
                "additionalProperties": False,
            },
        }
    try:
        jsonschema.validate(
            payload,
            {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        )
        if len(json.dumps(payload, allow_nan=False).encode()) > 65536:
            raise ValueError("invalid_arguments")
    except (jsonschema.ValidationError, TypeError, ValueError) as exc:
        raise ValueError("invalid_arguments") from exc


class DesktopControlChannel:
    """Keep live requests, session ownership and bounded metadata in memory.

    Args:
        server: Existing device server with authenticated sockets and online probe.
    """

    def __init__(self, server):
        self.server = server
        self.capabilities = {}
        self.pending = {}
        self.sessions = {}
        self.receipts = OrderedDict()
        self.busy = set()
        self.screenshots = OrderedDict()
        self.screenshot_expiry = None
        self.closed = False

    def prune_screenshots(self):
        """删除到期的内存截图，不写磁盘或普通回执。"""
        if self.screenshot_expiry is not None:
            self.screenshot_expiry.cancel()
            self.screenshot_expiry = None
        for identity, entry in list(self.screenshots.items()):
            if entry["expires_at"] <= time.monotonic():
                self.screenshots.pop(identity, None)
        if self.screenshots:
            delay = max(
                0.01,
                min(e["expires_at"] for e in self.screenshots.values())
                - time.monotonic(),
            )
            self.screenshot_expiry = asyncio.get_running_loop().call_later(
                delay, self.prune_screenshots
            )

    def close(self):
        """插件退出立即释放短时图片和定时清理句柄。"""
        self.closed = True
        if self.screenshot_expiry is not None:
            self.screenshot_expiry.cancel()
            self.screenshot_expiry = None
        self.screenshots.clear()

    def keep_screenshot(self, device_id, owner_scope, result):
        """保存可再次引用的私聊截图，最多8张/48MiB、10分钟。

        Args:
            device_id: 已鉴权设备。
            owner_scope: 来自真实事件的用户私聊范围。
            result: 已验证的截图及元信息。

        Returns:
            不包含图片正文的随机引用编号。
        """
        if self.closed:
            raise RuntimeError("channel_closed")
        self.prune_screenshots()
        identity = uuid.uuid4().hex
        self.screenshots[identity] = {
            "device": device_id,
            "owner": owner_scope,
            "expires_at": time.monotonic() + 600,
            "result": {**result, "screenshot_id": identity, "retention_seconds": 600},
            "delivery": "not_sent",
        }
        while (
            len(self.screenshots) > 8
            or sum(len(e["result"]["image_b64"]) for e in self.screenshots.values())
            > 48 * 1024 * 1024
        ):
            self.screenshots.popitem(last=False)
        self.prune_screenshots()
        return identity

    def read_screenshot(self, device_id, owner_scope, identity):
        """仅向同设备、同用户私聊返回尚有效的截图。"""
        self.prune_screenshots()
        entry = self.screenshots.get(identity)
        if not entry or entry["device"] != device_id or entry["owner"] != owner_scope:
            return {"ok": False, "code": "screenshot_not_available"}
        return {"ok": True, "code": "completed", "result": dict(entry["result"])}

    def receipt(self, device_id, owner_scope, request_id):
        """Read metadata only, without replaying actions or returning screenshots.

        Args:
            device_id: Authenticated device identity selected by the caller.
            owner_scope: Host-created private conversation identity.
            request_id: Transport ID from an earlier call.

        Returns:
            Safe metadata or a scoped not-found result.
        """
        entry = self.receipts.get(request_id)
        if (
            entry is None
            or entry["device_id"] != device_id
            or entry["owner"] != owner_scope
        ):
            return {"ok": False, "code": "request_not_in_scope"}
        return dict(entry["receipt"])

    async def call(self, device_id, owner_scope, tool, payload, *, timeout=15):
        """Send once on the current socket and await the matching bounded result.

        Args:
            device_id: Paired device ID.
            owner_scope: Trusted private conversation identity, never model supplied.
            tool: Documented primitive name.
            payload: Primitive arguments; screenshots are never stored in SQLite.
            timeout: Total live response budget in seconds, capped at 20.

        Returns:
            Actual result or a repairable failure with a request ID when sent.
        """
        if self.closed:
            return {"ok": False, "code": "channel_closed"}
        try:
            validate_call(tool, payload)
            if isinstance(timeout, bool) or not 0 < timeout <= 20:
                raise ValueError("invalid_arguments")
        except (ValueError, TypeError):
            return {"ok": False, "code": "invalid_arguments"}
        ws = self.server._connected_socket(device_id)
        if ws is None:
            return {"ok": False, "code": "offline"}
        if self.capabilities.get(device_id) is not ws:
            return {
                "ok": False,
                "code": "client_not_adapted",
                "notice": "桌宠尚未接入底层工具传输；不能执行桌面操作。",
            }
        session = self.sessions.get(device_id)
        if session and session[:2] != (owner_scope, ws):
            return {"ok": False, "code": "session_owned_elsewhere"}
        if tool == "begin" and session:
            return {"ok": False, "code": "session_already_active"}
        if tool not in {"begin", "status"} and session is None:
            return {"ok": False, "code": "no_control_session"}
        if device_id in self.busy and tool != "stop":
            return {"ok": False, "code": "busy"}
        reserved = tool != "stop"
        if reserved:
            self.busy.add(device_id)
        identity = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        entry = {
            "device_id": device_id,
            "owner": owner_scope,
            "ws": ws,
            "future": future,
            "tool": tool,
            "payload": dict(payload),
            "receipt": {"ok": False, "code": "not_sent", "request_id": identity},
        }
        sent = False
        try:
            check = await self.server.check_connection(
                device_id, timeout=min(5, timeout)
            )
            if not check["ok"]:
                return {"ok": False, "code": check["status"]}
            if self.server._connected_socket(device_id) is not ws:
                return {"ok": False, "code": "connection_changed"}
            if tool == "begin":
                self.sessions[device_id] = (owner_scope, ws, identity)
            entry["session"] = self.sessions.get(device_id)
            self.pending[identity] = entry
            self.receipts[identity] = entry
            for old in list(self.receipts):
                if len(self.receipts) <= 256:
                    break
                if old not in self.pending:
                    del self.receipts[old]
            entry["receipt"] = {
                "ok": False,
                "code": "result_unknown",
                "request_id": identity,
                "tool": tool,
                "checked_at": time.time(),
            }
            sent = True
            await asyncio.wait_for(
                ws.send_json(
                    {
                        "type": "desktop_tool",
                        "id": identity,
                        "tool": tool,
                        "payload": payload,
                        "expires_at": time.time() + timeout,
                    }
                ),
                timeout=min(5, timeout),
            )
            response = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            if self.closed:
                return {"ok": False, "code": "channel_closed", "request_id": identity}
            if tool == "screenshot" and response.get("ok"):
                identity_ref = self.keep_screenshot(
                    device_id, owner_scope, response["result"]
                )
                response = self.read_screenshot(
                    device_id, owner_scope, identity_ref
                ) | {"request_id": identity}
            return response
        except (TimeoutError, ConnectionError, RuntimeError):
            return dict(entry["receipt"])
        finally:
            self.pending.pop(identity, None)
            if reserved:
                self.busy.discard(device_id)
            if sent and not future.done():
                future.cancel()
                try:
                    await asyncio.wait_for(
                        ws.send_json({"type": "desktop_tool_cancel", "id": identity}),
                        timeout=1,
                    )
                except (TimeoutError, ConnectionError, RuntimeError):
                    pass
            entry.pop("payload", None)
            entry.pop("future", None)

    async def receive(self, device_id, ws, message):
        """Accept capabilities and matched results only from the bound socket.

        Args:
            device_id: Identity established by WebSocket authentication.
            ws: Current authenticated socket.
            message: Incoming protocol object.

        Raises:
            ValueError: A response belongs to another device or socket.
        """
        if self.server._connected_socket(device_id) is not ws:
            raise ValueError("connection_changed")
        if message["type"] == "desktop_tool_hello":
            if type(message.get("version")) is not int or message["version"] != 1:
                raise ValueError("unsupported_protocol")
            self.capabilities[device_id] = ws
            await ws.send_json(
                {
                    "type": "desktop_tool_ready",
                    "version": 1,
                    "max_message_bytes": MAX_MESSAGE_BYTES,
                }
            )
            return
        entry = self.receipts.get(message.get("id"))
        if entry is None:
            return
        if entry["device_id"] != device_id or entry["ws"] is not ws:
            raise ValueError("result_not_in_scope")
        if entry["receipt"].get("code") != "result_unknown":
            return
        result, ok = message.get("result"), message.get("ok")
        try:
            if type(ok) is not bool or not isinstance(result, dict):
                raise ValueError("invalid_result")
            limit = MAX_MESSAGE_BYTES if entry["tool"] == "screenshot" else 256 * 1024
            if (
                len(json.dumps(message, ensure_ascii=False, allow_nan=False).encode())
                > limit
            ):
                raise ValueError("result_too_large")
            if ok and entry["tool"] == "screenshot":
                if result.get("mime") not in {"image/png", "image/jpeg"}:
                    raise ValueError("invalid_image")
                data = base64.b64decode(result["image_b64"], validate=True)
                for name in (
                    "width",
                    "height",
                    "source_width",
                    "source_height",
                    "physical_width",
                    "physical_height",
                ):
                    if (
                        type(result.get(name)) is not int
                        or not 0 < result[name] <= 100_000
                    ):
                        raise ValueError("invalid_dimensions")
                if (
                    any(type(result.get(name)) is not int for name in ("left", "top"))
                    or not isinstance(result.get("observation_id"), str)
                    or not result["observation_id"]
                ):
                    raise ValueError("invalid_observation")
                with Image.open(io.BytesIO(data)) as image:
                    if (
                        image.width * image.height > 100_000_000
                        or image.size != (result["width"], result["height"])
                        or Image.MIME.get(image.format) != result["mime"]
                    ):
                        raise ValueError("image_metadata_mismatch")
                    image.verify()
                if entry.get("payload", {}).get("original") and (
                    result["mime"] != "image/png"
                    or result["width"] != result["source_width"]
                    or result["height"] != result["source_height"]
                ):
                    raise ValueError("original_image_changed")
            elif "image_b64" in result:
                raise ValueError("unexpected_image")
        except (
            ValueError,
            TypeError,
            KeyError,
            OSError,
            binascii.Error,
            Image.DecompressionBombError,
        ):
            ok, result = False, {"code": "invalid_result"}
        response = {
            "ok": ok,
            "code": "completed" if ok else str(result.get("code", "client_error"))[:80],
            "request_id": message["id"],
            "result": result,
        }
        entry["receipt"] = {
            key: value for key, value in response.items() if key != "result"
        }
        future = entry.get("future")
        if future is not None and not future.done():
            future.set_result(response)
        if self.sessions.get(device_id) is entry.get("session") and (
            (entry["tool"] == "begin" and not ok) or (entry["tool"] == "stop" and ok)
        ):
            self.sessions.pop(device_id, None)

    def disconnected(self, device_id, ws):
        """Invalidate a departing socket without affecting its replacement.

        Args:
            device_id: Departing authenticated identity.
            ws: Exact old socket, never a replacement connection.
        """
        if self.capabilities.get(device_id) is ws:
            self.capabilities.pop(device_id, None)
        if self.sessions.get(device_id, (None, None))[1] is ws:
            self.sessions.pop(device_id, None)
        for entry in self.receipts.values():
            if entry["device_id"] != device_id or entry["ws"] is not ws:
                continue
            future = entry.get("future")
            if future is not None and not future.done():
                entry["receipt"] = {
                    "ok": False,
                    "code": "connection_changed",
                    "request_id": entry["receipt"]["request_id"],
                }
                future.set_result(dict(entry["receipt"]))
