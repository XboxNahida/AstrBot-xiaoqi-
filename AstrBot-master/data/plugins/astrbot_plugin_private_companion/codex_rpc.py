"""Bounded JSONL transport for the locally installed Codex app-server."""

from __future__ import annotations

import asyncio
import inspect
import json


class CodexRpcError(Exception):
    """Expose the server error without retaining the original request.

    Args:
        error: The protocol error object returned by the server.
    """

    def __init__(self, error):
        self.code = error["code"]
        self.message = error["message"]
        self.data = error.get("data")
        super().__init__(f"Codex RPC error ({self.code})")


class CodexRpcClient:
    """Multiplex requests and asynchronous callbacks over an owned channel.

    Args:
        channel: Owned duplex process channel; closing the client terminates it.
        on_notification: Optional async callback receiving method and params.
        on_request: Optional async callback returning a server request result.
    """

    MAX_FRAME_BYTES = 32 * 1024 * 1024

    def __init__(self, channel, *, on_notification=None, on_request=None):
        self.channel = channel
        self.on_notification = on_notification
        self.on_request = on_request
        self._next_id = 0
        self._pending = {}
        self._callbacks = set()
        self._write_lock = asyncio.Lock()
        self._reader = None
        self._closing = None
        self._closed = False
        self._failure = None

    async def start(self):
        """Negotiate the verified initialize/initialized protocol.

        Returns:
            The server initialization result.
        """
        try:
            result = await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "astrbot", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._send({"method": "initialized"})
            return result
        except BaseException:
            await self.close()
            raise

    async def _send(self, frame):
        data = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(data) > self.MAX_FRAME_BYTES:
            raise ValueError("Codex RPC frame exceeds size limit")
        async with self._write_lock:
            if self._closed:
                raise self._failure or ConnectionError("Codex RPC is closed")
            try:
                await self.channel.write(data + b"\n")
            except asyncio.CancelledError:
                # A cancelled write can leave a partial JSON frame on the wire.
                self._begin_close(ConnectionError("Codex RPC write cancelled"))
                raise
            except Exception:
                self._begin_close(ConnectionError("Codex RPC write failed"))
                raise self._failure from None

    async def request(self, method, params=None, timeout=60):
        """Send one request and match its independent response.

        Args:
            method: Protocol method name.
            params: Optional JSON parameters.
            timeout: Total request timeout in seconds, including write.

        Returns:
            The result value from the corresponding response.
        """
        if self._closed:
            raise self._failure or ConnectionError("Codex RPC is closed")
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        frame = {"id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        try:
            async with asyncio.timeout(timeout):
                await self._send(frame)
                return await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _callback(self, frame):
        request = "id" in frame
        callback = self.on_request if request else self.on_notification
        if not request:
            if callback is not None:
                await callback(frame["method"], frame.get("params"))
            return
        if callback is None:
            response = {"error": {"code": -32601, "message": "Method not supported"}}
        else:
            try:
                response = {
                    "result": await callback(frame["method"], frame.get("params"))
                }
            except Exception:
                response = {"error": {"code": -32603, "message": "Host tool failed"}}
        try:
            await self._send({"id": frame["id"], **response})
        except (TypeError, ValueError):
            try:
                await self._send(
                    {
                        "id": frame["id"],
                        "error": {
                            "code": -32603,
                            "message": "Host tool response unavailable",
                        },
                    }
                )
            except (TypeError, ValueError):
                self._begin_close(
                    ConnectionError("Codex RPC response cannot be encoded")
                )

    async def _read_loop(self):
        buffer = bytearray()
        try:
            while True:
                chunk = await self.channel.read()
                if not chunk:
                    raise ConnectionError("Codex RPC connection ended")
                buffer.extend(chunk)
                while True:
                    boundary = buffer.find(b"\n")
                    if boundary < 0:
                        if len(buffer) > self.MAX_FRAME_BYTES:
                            raise ConnectionError("Codex RPC frame exceeds size limit")
                        break
                    if boundary > self.MAX_FRAME_BYTES:
                        raise ConnectionError("Codex RPC frame exceeds size limit")
                    line = bytes(buffer[:boundary])
                    del buffer[: boundary + 1]
                    frame = json.loads(line.decode("utf-8"))
                    if not isinstance(frame, dict):
                        raise ValueError("Invalid frame")
                    if "id" in frame and (type(frame["id"]) not in (str, int)):
                        raise ValueError("Invalid response id")
                    if "method" in frame:
                        if not isinstance(frame["method"], str):
                            raise ValueError("Invalid method")
                        task = asyncio.create_task(self._callback(frame))
                        self._callbacks.add(task)
                        task.add_done_callback(self._callback_done)
                    else:
                        if "id" not in frame or ("result" in frame) == (
                            "error" in frame
                        ):
                            raise ValueError("Invalid response")
                        if "error" in frame:
                            error = frame["error"]
                            if (
                                not isinstance(error, dict)
                                or type(error.get("code")) is not int
                                or not isinstance(error.get("message"), str)
                            ):
                                raise ValueError("Invalid error response")
                        future = self._pending.get(frame["id"])
                        if future is not None and not future.done():
                            if "error" in frame:
                                future.set_exception(CodexRpcError(frame["error"]))
                            else:
                                future.set_result(frame["result"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, ConnectionError)
                else ConnectionError("Malformed Codex RPC frame")
            )
            self._begin_close(failure)

    def _callback_done(self, task):
        self._callbacks.discard(task)
        if not task.cancelled():
            task.exception()

    def _begin_close(self, failure=None):
        if self._closing is None:
            self._closed = True
            self._failure = failure or ConnectionError("Codex RPC is closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(self._failure)
            self._closing = asyncio.create_task(self._shutdown(asyncio.current_task()))

    async def _shutdown(self, initiator):
        tasks = [
            task
            for task in [self._reader, *self._callbacks]
            if task is not None and task is not initiator
        ]
        for task in tasks:
            task.cancel()
        try:
            result = self.channel.kill_tree()
            if inspect.isawaitable(result):
                await result
        finally:
            try:
                result = self.channel.close()
                if inspect.isawaitable(result):
                    await result
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        """Terminate the channel and join reader/callback cleanup."""
        was_closing = self._closing is not None
        self._begin_close()
        # Shutdown joins callbacks. Their cancellation cleanup must not wait
        # for the same shutdown task to finish joining themselves.
        if was_closing and asyncio.current_task() in self._callbacks:
            return
        await asyncio.shield(self._closing)
