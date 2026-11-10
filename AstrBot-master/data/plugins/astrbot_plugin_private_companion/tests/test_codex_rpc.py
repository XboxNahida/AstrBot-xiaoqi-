import asyncio
import ctypes
import importlib
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes

import pytest


class Channel:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.killed = self.closed = False

    async def read(self):
        return await self.incoming.get()

    async def write(self, data):
        await self.sent.put(json.loads(data))

    async def wait(self):
        return 0

    def kill_tree(self):
        self.killed = True

    async def close(self):
        self.closed = True

    async def feed(self, value):
        await self.incoming.put(json.dumps(value).encode() + b"\n")


def client(channel, **kwargs):
    assert importlib.util.find_spec("codex_rpc"), "RPC transport is missing"
    return importlib.import_module("codex_rpc").CodexRpcClient(channel, **kwargs)


def test_handshake_and_out_of_order_responses():
    async def run():
        channel = Channel()
        rpc = client(channel)
        startup = asyncio.create_task(rpc.start())
        hello = await channel.sent.get()
        assert hello == {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "astrbot", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        }
        await channel.feed({"id": 1, "result": {"userAgent": "test"}})
        await startup
        assert await channel.sent.get() == {"method": "initialized"}
        one = asyncio.create_task(rpc.request("thread/list"))
        two = asyncio.create_task(rpc.request("thread/read", {"threadId": "x"}))
        a, b = await channel.sent.get(), await channel.sent.get()
        await channel.feed({"id": b["id"], "result": "second"})
        await channel.feed({"id": a["id"], "result": "first"})
        assert await one == "first"
        assert await two == "second"
        await rpc.close()
        assert channel.killed and channel.closed

    asyncio.run(asyncio.wait_for(run(), 3))


def test_notifications_do_not_block_and_server_requests_keep_id():
    async def run():
        channel = Channel()
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def notification(method, params):
            assert method == "turn/started"
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def tool(method, params):
            assert method == "item/tool/call" and params["name"] == "read"
            return {"success": True, "contentItems": []}

        rpc = client(channel, on_notification=notification, on_request=tool)
        query = asyncio.create_task(rpc.request("thread/list"))
        request = await channel.sent.get()
        await channel.feed({"method": "turn/started", "params": {}})
        await entered.wait()
        await channel.feed(
            {"id": "server-9", "method": "item/tool/call", "params": {"name": "read"}}
        )
        assert await channel.sent.get() == {
            "id": "server-9",
            "result": {"success": True, "contentItems": []},
        }
        await channel.feed({"id": request["id"], "result": 7})
        assert await query == 7
        await rpc.close()
        assert cancelled.is_set()

    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.parametrize("failure", [b"", b"{invalid}\n", b"[]\n", b'{"id":1}\n'])
def test_disconnect_fails_every_pending_and_closes(failure):
    async def run():
        channel = Channel()
        rpc = client(channel)
        pending = [asyncio.create_task(rpc.request("thread/read")) for _ in range(2)]
        await channel.sent.get()
        await channel.sent.get()
        await channel.incoming.put(failure)
        results = await asyncio.gather(*pending, return_exceptions=True)
        assert all(isinstance(result, ConnectionError) for result in results)
        await rpc.close()
        assert channel.closed and channel.killed and not rpc._pending

    asyncio.run(asyncio.wait_for(run(), 3))


def test_timeout_cancellation_and_structured_error():
    async def run():
        channel = Channel()
        rpc = client(channel)
        with pytest.raises(TimeoutError):
            await rpc.request("thread/list", timeout=0.01)
        await channel.sent.get()
        assert not rpc._pending
        pending = asyncio.create_task(rpc.request("thread/list"))
        await channel.sent.get()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not rpc._pending
        pending = asyncio.create_task(
            rpc.request("thread/read", {"secret": "private-value"})
        )
        frame = await channel.sent.get()
        await channel.feed(
            {
                "id": frame["id"],
                "error": {"code": -1, "message": "denied", "data": {"retry": False}},
            }
        )
        with pytest.raises(Exception) as caught:
            await pending
        assert caught.value.code == -1 and caught.value.data == {"retry": False}
        assert "private-value" not in str(caught.value)
        await rpc.close()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_fragmented_utf8_and_frame_limit():
    async def run():
        channel = Channel()
        rpc = client(channel)
        pending = asyncio.create_task(rpc.request("thread/list"))
        frame = await channel.sent.get()
        encoded = (
            json.dumps(
                {"id": frame["id"], "result": "中文"}, ensure_ascii=False
            ).encode()
            + b"\n"
        )
        for byte in encoded:
            await channel.incoming.put(bytes([byte]))
        assert await pending == "中文"
        rpc.MAX_FRAME_BYTES = 128
        pending = asyncio.create_task(rpc.request("thread/list"))
        await channel.sent.get()
        await channel.incoming.put(b"x" * 129)
        with pytest.raises(ConnectionError, match="limit"):
            await pending
        await rpc.close()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_external_close_joins_callback_that_closes_in_finally():
    async def run():
        channel = Channel()
        entered = asyncio.Event()

        async def callback(method, params):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await rpc.close()

        rpc = client(channel, on_notification=callback)
        pending = asyncio.create_task(rpc.request("thread/list"))
        frame = await channel.sent.get()
        await channel.feed({"id": frame["id"], "result": []})
        await pending
        await channel.feed({"method": "turn/started", "params": {}})
        await entered.wait()
        closing = asyncio.create_task(rpc.close())
        try:
            done, _ = await asyncio.wait({closing}, timeout=0.1)
            assert closing in done, "Shutdown and callback cleanup deadlocked"
            assert channel.closed and not rpc._callbacks
        finally:
            for task in tuple(rpc._callbacks):
                task.cancel()
            await asyncio.wait_for(asyncio.shield(closing), 1)

    asyncio.run(asyncio.wait_for(run(), 3))


def test_oversized_tool_response_returns_bounded_error():
    async def run():
        channel = Channel()

        async def tool(method, params):
            return "private-result" * 30

        rpc = client(channel, on_request=tool)
        rpc.MAX_FRAME_BYTES = 128
        pending = asyncio.create_task(rpc.request("thread/list"))
        frame = await channel.sent.get()
        await channel.feed({"id": frame["id"], "result": []})
        await pending
        await channel.feed({"id": "tool-1", "method": "item/tool/call"})
        try:
            try:
                result = await asyncio.wait_for(channel.sent.get(), 0.1)
            except TimeoutError:
                pytest.fail("Tool result was silently dropped")
            assert result["id"] == "tool-1" and "error" in result
            assert "private-result" not in json.dumps(result)
        finally:
            await rpc.close()

    asyncio.run(asyncio.wait_for(run(), 3))


def test_duplex_native_credentials_are_checked(monkeypatch, tmp_path):
    import task_execution as api

    backend = api.WindowsNativeBackend(lambda identity: ("other", "user", "secret"))
    assert hasattr(backend, "start_duplex"), "Native duplex entrypoint is missing"
    identity = api.ExecutionIdentity("machine", "user", "S-1-test")
    with pytest.raises(PermissionError, match="identity"):
        backend.start_duplex(identity, ("exe",), tmp_path, {})


@pytest.fixture
def native_kernel(monkeypatch):
    import task_execution as api

    class Function:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    class Kernel:
        def __init__(self):
            self.next = 100
            self.pipes, self.buffers, self.inherit, self.closed = [], {}, [], []
            self.dead = False
            self.written = bytearray()
            self.inherited = None

        def __getattr__(self, name):
            function = Function(lambda *args: 1)
            setattr(self, name, function)
            return function

    kernel = Kernel()

    def pipe(read, write, *_):
        kernel.next += 2
        read._obj.value, write._obj.value = kernel.next, kernel.next + 1
        kernel.pipes.append((read._obj.value, write._obj.value))
        kernel.buffers[read._obj.value] = bytearray()
        return 1

    def peek(handle, _, __, ___, available, _____):
        available._obj.value = len(kernel.buffers[handle.value])
        return bool(available._obj.value or not kernel.dead)

    def read(handle, buffer, size, count, _):
        while not kernel.buffers[handle.value] and not kernel.dead:
            time.sleep(0.001)
        data = kernel.buffers[handle.value][:size]
        del kernel.buffers[handle.value][:size]
        ctypes.memmove(buffer, bytes(data), len(data))
        count._obj.value = len(data)
        return 1

    def write(handle, buffer, size, count, _):
        count._obj.value = min(2, size)
        kernel.written.extend(ctypes.string_at(buffer, count._obj.value))
        return 1

    def create(*args):
        args[-1]._obj.process, args[-1]._obj.thread = 500, 501
        return 1

    def attributes(pointer, count, flags, needed):
        needed._obj.value = 64
        return 1

    def update(*args):
        kernel.inherited = list(args[3])
        return 1

    kernel.CreatePipe = Function(pipe)
    kernel.PeekNamedPipe = Function(peek)
    kernel.ReadFile = Function(read)
    kernel.WriteFile = Function(write)
    kernel.CreateProcessAsUserW = Function(create)
    kernel.InitializeProcThreadAttributeList = Function(attributes)
    kernel.UpdateProcThreadAttribute = Function(update)
    kernel.SetHandleInformation = Function(
        lambda handle, *args: kernel.inherit.append(handle.value) or 1
    )
    kernel.CloseHandle = Function(
        lambda handle: kernel.closed.append(getattr(handle, "value", handle)) or 1
    )
    kernel.TerminateJobObject = Function(
        lambda *args: setattr(kernel, "dead", True) or 1
    )
    kernel.WaitForSingleObject = Function(lambda *args: 0 if kernel.dead else 0x102)
    kernel.GetExitCodeProcess = Function(
        lambda handle, code: setattr(code._obj, "value", 7) or 1
    )
    monkeypatch.setattr(api.ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    monkeypatch.setattr(api.ctypes, "get_last_error", lambda: 109)
    monkeypatch.setattr(api, "_check_token_sid", lambda *args: None)
    return api, kernel


def test_native_duplex_streaming_partial_writes_and_inheritance(
    native_kernel, tmp_path
):
    api, kernel = native_kernel

    async def run():
        backend = api.WindowsNativeBackend(
            lambda identity: ("machine", "user", "secret")
        )
        assert hasattr(backend, "start_duplex"), "Native duplex entrypoint is missing"
        channel = backend.start_duplex(
            api.ExecutionIdentity("machine", "user", "S-1-test"), ("exe",), tmp_path, {}
        )
        assert len(kernel.pipes) == 3
        stdout, stderr, stdin = kernel.pipes
        assert kernel.inherited == [stdin[0], stdout[1], stderr[1]]
        assert set(kernel.inherit) == {stdout[0], stderr[0], stdin[1]}
        assert not set(kernel.inherited) & set(kernel.inherit)
        kernel.buffers[stdout[0]].extend(b'{"result":1}\n')
        kernel.buffers[stderr[0]].extend(b"diagnostic" * 150000)
        assert await channel.read() == b'{"result":1}\n'
        assert not kernel.dead
        await channel.write(b"abcde")
        assert kernel.written == b"abcde"
        kernel.dead = True
        assert await channel.wait() == 7
        assert len(channel.stderr) <= 1024 * 1024
        assert not kernel.buffers[stderr[0]]
        await channel.close()
        assert stdin[1] in kernel.closed

    asyncio.run(asyncio.wait_for(run(), 3))


def test_native_duplex_cancelled_read_closes_without_blocking_thread(
    native_kernel, tmp_path
):
    api, kernel = native_kernel

    async def run():
        backend = api.WindowsNativeBackend(
            lambda identity: ("machine", "user", "secret")
        )
        assert hasattr(backend, "start_duplex"), "Native duplex entrypoint is missing"
        channel = backend.start_duplex(
            api.ExecutionIdentity("machine", "user", "S-1-test"), ("exe",), tmp_path, {}
        )
        task = asyncio.create_task(channel.read())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await channel.close()
        assert kernel.dead and channel._process.handles == []

    asyncio.run(asyncio.wait_for(run(), 3))


def test_callback_can_close_its_client_without_self_deadlock():
    async def run():
        channel = Channel()
        finished = asyncio.Event()

        async def notification(method, params):
            await rpc.close()
            finished.set()

        rpc = client(channel, on_notification=notification)
        pending = asyncio.create_task(rpc.request("thread/list"))
        await channel.sent.get()
        await channel.feed({"method": "turn/completed"})
        await finished.wait()
        with pytest.raises(ConnectionError):
            await pending
        assert channel.closed

    asyncio.run(asyncio.wait_for(run(), 1))


def test_native_cancelled_write_retains_cancellation(native_kernel, tmp_path):
    import threading

    api, kernel = native_kernel

    async def run():
        backend = api.WindowsNativeBackend(
            lambda identity: ("machine", "user", "secret")
        )
        channel = backend.start_duplex(
            api.ExecutionIdentity("machine", "user", "S-1-test"), ("exe",), tmp_path, {}
        )
        entered = threading.Event()

        def blocked_write(*args):
            entered.set()
            while not kernel.dead:
                time.sleep(0.001)
            return 0

        kernel.WriteFile.fn = blocked_write
        pending = asyncio.create_task(channel.write(b"x" * 100000))
        await asyncio.to_thread(entered.wait)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await channel.close()
        assert kernel.dead and not channel._writes

    asyncio.run(asyncio.wait_for(run(), 2))


@pytest.mark.skipif(os.name != "nt", reason="Requires real Windows anonymous pipes")
def test_real_windows_pipe_capacity_and_stderr_drain():
    import msvcrt

    import task_execution as api

    async def run():
        child = await asyncio.to_thread(
            subprocess.Popen,
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.buffer.write(b'e'*2000000); sys.stderr.flush(); data=sys.stdin.buffer.read(200000); sys.stdout.buffer.write(data); sys.stdout.flush()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        process = api._NativeProcess.__new__(api._NativeProcess)
        process.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        process.kernel.ReadFile.argtypes = [
            handle,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        process.kernel.WriteFile.argtypes = process.kernel.ReadFile.argtypes
        process.kernel.PeekNamedPipe.argtypes = [
            handle,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        process.kernel.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
        process.kernel.GetExitCodeProcess.argtypes = [
            handle,
            ctypes.POINTER(wintypes.DWORD),
        ]
        process.process = handle(int(child._handle))
        process.reads = [
            handle(msvcrt.get_osfhandle(stream.fileno()))
            for stream in (child.stdout, child.stderr)
        ]
        process.stdin = handle(msvcrt.get_osfhandle(child.stdin.fileno()))
        process.output_limit = 1024 * 1024
        process.kill_tree = lambda: child.kill() if child.poll() is None else None

        def close():
            process.kill_tree()
            child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

        process.close = close
        channel = api._NativeDuplexChannel(process)
        try:
            payload = b"x" * 200000
            await channel.write(payload)
            result = bytearray()
            while chunk := await channel.read():
                result.extend(chunk)
            assert result == payload
            assert await channel.wait() == 0
            assert len(channel.stderr) == 1024 * 1024
        finally:
            await channel.close()
        assert child.poll() == 0

    asyncio.run(asyncio.wait_for(run(), 10))
