import asyncio
import importlib.util
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def load_server():
    path = (
        Path(__file__).parents[1] / "cloud_plugin/astrbot_plugin_desktop_link/server.py"
    )
    assert path.exists(), "尚未实现云端桌宠协议服务"
    spec = importlib.util.spec_from_file_location("desktop_test_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DesktopLinkServer


@pytest.mark.asyncio
async def test_auth_and_device_binding(tmp_path):
    server = load_server()(tmp_path)
    paired = server.pair("pc-one", "本机")
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.get("/desktop/health")).status == 200
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/desktop/ws")
        assert exc.value.status == 401
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect(
                "/desktop/ws?device_id=other",
                headers={"Authorization": "Bearer " + paired["token"]},
            )
        assert exc.value.status == 403
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        assert (await ws.receive_json())["device_id"] == "pc-one"
        assert server.devices()[0]["online"] is True
        await ws.close()
    await server.stop()


@pytest.mark.asyncio
async def test_command_ack_report_and_persistence(tmp_path):
    server = load_server()(tmp_path)
    paired = server.pair("pc-one", "本机")
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await ws.receive_json()
        receipt = await server.command("pc-one", "say", {"text": "测试消息"})
        command = await ws.receive_json()
        assert command["id"] == receipt["id"]
        assert command["kind"] == "say"
        await ws.send_json(
            {"type": "ack", "id": receipt["id"], "status": "received", "result": {}}
        )
        await ws.send_json(
            {
                "type": "ack",
                "id": receipt["id"],
                "status": "succeeded",
                "result": {"shown": True},
            }
        )
        await ws.send_json(
            {
                "type": "report",
                "id": "report-1",
                "observed_at": 123.5,
                "facts": {"status": "就绪"},
                "summary": "桌宠就绪",
            }
        )
        assert (await ws.receive_json())["type"] == "report_ack"
        assert server.command_status(receipt["id"])["status"] == "succeeded"
        await ws.send_json(
            {
                "type": "report",
                "id": "report-1",
                "observed_at": 123.5,
                "facts": {"status": "就绪"},
                "summary": "重复",
            }
        )
        await ws.receive_json()
        assert len(server.reports("pc-one")) == 1
        assert server.devices()[0].get("token") is None
        await ws.close()
    await server.stop()
    restarted = load_server()(tmp_path)
    assert restarted.reports("pc-one")[0]["summary"] == "桌宠就绪"
    assert restarted.command_status(receipt["id"])["result"] == {"shown": True}
    assert restarted.devices()[0]["online"] is False
    await restarted.stop()


@pytest.mark.asyncio
async def test_expiry_unknown_operation_and_ack_ownership(tmp_path):
    server = load_server()(tmp_path)
    server.pair("one", "一")
    two = server.pair("two", "二")
    with pytest.raises(ValueError):
        await server.command("one", "shell", {"text": "anything"})
    queued = await server.command("one", "status", {}, ttl=0.01)
    await asyncio.sleep(0.03)
    assert server.command_status(queued["id"])["status"] == "expired"
    other = await server.command("one", "status", {})
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + two["token"]}
        )
        await ws.receive_json()
        await ws.send_json(
            {"type": "ack", "id": other["id"], "status": "succeeded", "result": {}}
        )
        assert (await ws.receive_json())["type"] == "error"
        assert server.command_status(other["id"])["status"] == "queued"
        await ws.close()
    await server.stop()


@pytest.mark.asyncio
async def test_chat_runs_without_blocking_heartbeat_and_deduplicates(tmp_path):
    calls = []
    gate = asyncio.Event()

    async def chat(device_id, text):
        calls.append((device_id, text))
        await gate.wait()
        return "云端回复"

    server = load_server()(tmp_path, chat_handler=chat)
    paired = server.pair("one", "一")
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await ws.receive_json()
        await ws.send_json({"type": "chat", "id": "chat-1", "text": "你好"})
        await ws.send_json({"type": "ping", "id": "ping-1"})
        assert (await ws.receive_json(timeout=1))["type"] == "pong"
        gate.set()
        reply = await ws.receive_json(timeout=1)
        assert reply["text"] == "云端回复"
        await ws.send_json({"type": "chat", "id": "chat-1", "text": "你好"})
        assert (await ws.receive_json())["text"] == "云端回复"
        assert calls == [("one", "你好")]
        await ws.close()
    await server.stop()


@pytest.mark.asyncio
async def test_reconnect_does_not_replay_delivered_operation(tmp_path):
    server = load_server()(tmp_path)
    paired = server.pair("one", "一")
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await ws.receive_json()
        receipt = await server.command("one", "say", {"text": "只展示一次"})
        await ws.receive_json()
        await ws.close()
        await asyncio.sleep(0.01)
        assert server.command_status(receipt["id"])["status"] == "interrupted"
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await ws.receive_json()
        await ws.send_json({"type": "ping", "id": "p"})
        assert (await ws.receive_json())["type"] == "pong"
        await ws.close()
    await server.stop()


@pytest.mark.asyncio
async def test_cancel_queue_and_terminal_ack_cannot_be_reversed(tmp_path):
    server = load_server()(tmp_path)
    paired = server.pair("one", "一")
    queued = await server.command("one", "status", {})
    assert (await server.cancel_command(queued["id"]))["status"] == "cancelled"
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await ws.receive_json()
        receipt = await server.command("one", "status", {})
        await ws.receive_json()
        await ws.send_json(
            {
                "type": "ack",
                "id": receipt["id"],
                "status": "succeeded",
                "result": {"ready": True},
            }
        )
        await ws.send_json(
            {"type": "ack", "id": receipt["id"], "status": "running", "result": {}}
        )
        assert (await ws.receive_json())["type"] == "error"
        assert server.command_status(receipt["id"])["status"] == "succeeded"
        await ws.close()
    assert paired["token"] not in (
        tmp_path / "desktop-link.sqlite3"
    ).read_bytes().decode("latin1")
    await server.stop()


@pytest.mark.asyncio
async def test_hello_failure_cleans_online_state_and_allows_reconnect(
    tmp_path, monkeypatch
):
    server = load_server()(tmp_path)
    paired = server.pair("one", "一")
    original = web.WebSocketResponse.send_json
    failed_once = False

    async def fail_first_hello(self, data, *args, **kwargs):
        nonlocal failed_once
        if data.get("type") == "hello" and not failed_once:
            failed_once = True
            raise ConnectionResetError("测试首帧发送时连接断开")
        return await original(self, data, *args, **kwargs)

    monkeypatch.setattr(web.WebSocketResponse, "send_json", fail_first_hello)
    async with TestClient(TestServer(server.app)) as client:
        first = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        await first.receive(timeout=1)
        assert server.devices()[0]["online"] is False
        second = await client.ws_connect(
            "/desktop/ws", headers={"Authorization": "Bearer " + paired["token"]}
        )
        assert (await second.receive_json())["type"] == "hello"
        await second.close()
    await server.stop()
