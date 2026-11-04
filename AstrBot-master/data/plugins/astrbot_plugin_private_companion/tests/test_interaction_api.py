import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_private_companion.interaction_api import InteractionAPI
from astrbot_plugin_private_companion.script_interaction_client import (
    InteractionClient,
    InteractionError,
)
from astrbot_plugin_private_companion.task_service import TaskScope, TaskService


@pytest_asyncio.fixture
async def api(tmp_path):
    runtime = SimpleNamespace(
        service=TaskService(tmp_path / "tasks.db"),
        plugin=SimpleNamespace(context=SimpleNamespace()),
        interaction_allowed=lambda _: True,
    )
    server = InteractionAPI(runtime)
    channel = server.channels.open(
        TaskScope("bot", "bot:FriendMessage:owner", "owner"), "source", {}
    )
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        yield server, client, channel
    finally:
        await client.close()


def headers(channel):
    return {"Authorization": "Bearer " + channel["token"]}


@pytest.mark.asyncio
async def test_api_requires_scoped_token_and_has_no_identity_override(api):
    server, client, channel = api
    assert (await client.get("/v1/events")).status == 401
    response = await client.get("/v1/events?sender_id=other", headers=headers(channel))
    assert response.status == 400
    server.runtime.interaction_allowed = lambda _: False
    assert (await client.get("/v1/events", headers=headers(channel))).status == 403


@pytest.mark.asyncio
async def test_api_collects_window_and_cancels_before_send(api):
    server, client, channel = api
    auth = headers(channel)
    queued = await (
        await client.post(
            "/v1/reply",
            headers=auth,
            json={"key": "one", "message": "提醒用户", "based_on_seq": 0},
        )
    ).json()
    claimed = server.channels.claim_reply()
    server.channels.observe(
        TaskScope("bot", "bot:FriendMessage:owner", "owner"), "m1", "收到", [], 1000
    )
    result = await (
        await client.get("/v1/events?after_seq=0&since=999&until=1001", headers=auth)
    ).json()
    assert result["events"][0]["text"] == "收到"
    assert result["total_messages"] == result["window_count"] == 1
    assert not server.channels.begin_reply(claimed)
    receipt = await (
        await client.get("/v1/replies/" + queued["id"], headers=auth)
    ).json()
    assert receipt["status"] == "superseded"
    assert (
        await client.post(
            "/v1/reply",
            headers=auth,
            json={"key": "two", "message": "旧请求", "based_on_seq": 0},
        )
    ).status == 409


@pytest.mark.asyncio
async def test_api_script_state_and_stop_are_separate_from_normal_chat(api):
    _, client, channel = api
    auth = headers(channel)
    state = await (
        await client.post(
            "/v1/state",
            headers=auth,
            json={"expected_revision": 0, "state": {"已处理": 2}},
        )
    ).json()
    assert state["state_revision"] == 1
    assert (
        await client.post(
            "/v1/state", headers=auth, json={"expected_revision": 0, "state": {}}
        )
    ).status == 409
    assert (
        await client.post("/v1/stop", headers=auth, json={"pause": True})
    ).status == 200
    assert (
        await client.post(
            "/v1/reply",
            headers=auth,
            json={"key": "x", "message": "暂停", "based_on_seq": 0},
        )
    ).status == 409
    assert (await client.post("/v1/resume", headers=auth, json={})).status == 200


@pytest.mark.asyncio
async def test_task_and_history_queries_stay_in_original_scope(api):
    server, client, channel = api
    auth = headers(channel)
    own = server.runtime.service.submit(
        TaskScope("bot", "bot:FriendMessage:owner", "owner"),
        "m1",
        "main",
        "自己的任务",
        "C:/Tasks",
    )
    other = server.runtime.service.submit(
        TaskScope("other", "other:FriendMessage:owner", "owner"),
        "m2",
        "main",
        "其他机器人任务",
        "C:/Tasks",
    )
    tasks = await (await client.get("/v1/tasks", headers=auth)).json()
    assert [t["task_id"] for t in tasks["tasks"]] == [own.task_id]
    assert (await client.get("/v1/tasks/" + other.task_id, headers=auth)).status == 404
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="cid"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(
                history='[{"role":"user","content":"你好"},{"role":"assistant","content":"嗨"},{"role":"system","content":"hidden"}]'
            )
        ),
    )
    server.runtime.plugin.context.conversation_manager = manager
    history = await (await client.get("/v1/history", headers=auth)).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"]
    manager.get_curr_conversation_id.assert_awaited_once_with(channel["session_key"])


@pytest.mark.asyncio
async def test_bad_inputs_do_not_leak_token(api):
    _, client, channel = api
    for route in (
        "/v1/events?after_seq=-1",
        "/v1/events?wait_seconds=nan",
        "/v1/events?limit=0",
    ):
        result = await client.get(route, headers=headers(channel))
        assert result.status == 400
        assert channel["token"] not in await result.text()
    assert (
        await client.post("/v1/close", headers=headers(channel), json={})
    ).status == 200
    assert (await client.get("/v1/events", headers=headers(channel))).status == 401


@pytest.mark.asyncio
async def test_large_task_queries_keep_valid_json_and_redact_leaves(api):
    server, client, channel = api
    payload = "长结果" * 3000 + channel["token"]
    job = SimpleNamespace(
        task_id="own",
        goal="自己的任务",
        status="succeeded",
        revision=1,
        error_kind="",
        result=payload,
        steps={"one": {"output": payload}},
    )
    server.runtime.service.list = lambda *args, **kwargs: [job] * 50
    server.runtime.service.get = lambda *args, **kwargs: job
    server.runtime._safe_text = lambda value, limit: value.replace(
        channel["token"], "[redacted]"
    )[:limit]
    response = await client.get("/v1/tasks", headers=headers(channel))
    listing = await response.json()
    assert len(listing["tasks"]) == 50
    assert "steps" not in listing["tasks"][0]
    response = await client.get("/v1/tasks/own", headers=headers(channel))
    detail = await response.json()
    assert detail["tasks"][0]["steps"]["one"]["output"].endswith("[redacted]")
    assert channel["token"] not in await response.text()


@pytest.mark.asyncio
async def test_long_poll_rechecks_revocation_and_receipts_are_channel_bound(api):
    server, client, channel = api
    other = server.channels.open(
        TaskScope("other", "other:FriendMessage:owner", "owner"), "other", {}
    )
    receipt = server.channels.request_reply(
        other["id"], "private", "其他机器人", based_on_seq=0
    )
    response = await client.get(
        "/v1/replies/" + receipt["id"], headers=headers(channel)
    )
    assert response.status == 400
    assert "其他机器人" not in await response.text()
    polling = asyncio.create_task(
        client.get("/v1/events?wait_seconds=5", headers=headers(channel))
    )
    await asyncio.sleep(0.02)
    server.runtime.interaction_allowed = lambda _: False
    server.changed.set()
    assert (await polling).status == 403


@pytest.mark.asyncio
async def test_standard_library_sdk_collects_fixed_window_and_stops(api, tmp_path):
    server, client, channel = api
    config = tmp_path / "channel.json"
    config.write_text(
        json.dumps(
            {
                "base_url": str(client.make_url("/")).rstrip("/"),
                "channel_id": channel["id"],
                "token": channel["token"],
            }
        ),
        encoding="utf-8",
    )
    sdk = InteractionClient(config)

    async def incoming():
        await asyncio.sleep(0.04)
        server.channels.observe(
            TaskScope("bot", "bot:FriendMessage:owner", "owner"),
            "m1",
            "确认",
            [],
            server.channels.clock(),
        )
        server.changed.set()

    producer = asyncio.create_task(incoming())
    messages = await asyncio.to_thread(sdk.collect, 0.15)
    await producer
    assert [m["text"] for m in messages] == ["确认"]
    assert sdk.cursor == 1
    queued = await asyncio.to_thread(sdk.reply, "仅一次", key="one")
    assert queued["status"] == "pending"
    await asyncio.to_thread(sdk.stop_replies)
    assert (await asyncio.to_thread(sdk.reply_status, queued["id"]))[
        "status"
    ] == "superseded"
    with pytest.raises(InteractionError) as caught:
        await asyncio.to_thread(sdk.reply, "暂停时不能发", key="two")
    assert caught.value.status == 409
    assert channel["token"] not in str(caught.value)
