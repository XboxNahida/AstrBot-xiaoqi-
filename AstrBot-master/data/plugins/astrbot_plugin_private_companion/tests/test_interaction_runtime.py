import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from astrbot_plugin_private_companion.tests.test_task_runtime import event, register
from astrbot_plugin_private_companion.tests.test_task_runtime import runtime as runtime


def channel(runtime, e=None):
    e = e or event()
    register(runtime, e)
    return runtime.interaction_api.channels.open(
        runtime.scope(e), "source", copy.deepcopy(runtime._events[e])
    )


def test_channel_data_observed_before_chat_and_ignores_bots(runtime):
    c = channel(runtime)
    e = event(mid="m1")
    e.message_str = "我醒了"
    assert runtime.observe_script_interactions(e) == 1
    assert runtime.observe_script_interactions(e) == 0
    runtime.plugin._known_bot_self_ids = lambda: {"owner"}
    assert runtime.observe_script_interactions(event(mid="m2")) == 0
    assert runtime.interaction_api.channels.events(c["id"])["total_messages"] == 1


@pytest.mark.asyncio
async def test_script_reply_uses_chat_and_rechecks_after_generation(runtime):
    c = channel(runtime)
    api = runtime.interaction_api
    api.channels.renew(runtime.scope(event()), c["id"], ttl_seconds=86400, cancel_on_input=True)
    api.channels.request_reply(c["id"], "one", "问问用户是否醒了", based_on_seq=0)

    async def render(*_):
        runtime.observe_script_interactions(event(mid="m2"))
        return "该起床啦&&reply&&"

    runtime.render_notification = render
    assert await runtime.send_script_reply_once()
    runtime.plugin.context.send_message.assert_not_awaited()
    api.channels.request_reply(c["id"], "two", "收到后续再说", based_on_seq=1)
    runtime.render_notification = AsyncMock(return_value="行，那就先这样。&&reply&&")
    assert await runtime.send_script_reply_once()
    sent = runtime.plugin.context.send_message.await_args.args[1].chain[0].text
    assert sent == "行，那就先这样。"


def test_channel_auth_revocation_is_not_overridden_by_script(runtime):
    c = channel(runtime)
    assert runtime.interaction_allowed(c)
    runtime.plugin._is_private_companion_owner_user_id = lambda _: False
    assert not runtime.interaction_allowed(c)


@pytest.mark.asyncio
async def test_missing_channel_id_is_correctable_input_not_communication_denial(runtime):
    from astrbot.core.star.context import Context

    original = runtime.plugin.context
    context = Context.__new__(Context)
    context.get_config = original.get_config
    context.get_llm_tool_manager = original.get_llm_tool_manager
    context.send_message = original.send_message
    runtime.plugin.context = context
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "建立互动", "use_workspace": True})
    job = runtime.service.claim("test")
    tool = runtime.resolve_tools(job, ("pc_interaction_channel",))["pc_interaction_channel"]
    result = await runtime.execute_tool(job, tool, {"action": "read"})
    data = json.loads(result["content"])
    assert data["code"] == "channel_id_required"
    assert result["recoverable"] is True
    assert "open" in data["hint"] and "list" in data["hint"]
    assert runtime.interaction_api.channels.list(job.scope) == []
    runtime.plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_open_tool_only_accepts_live_task_and_does_not_return_token(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "写互动脚本", "use_workspace": True})
    job = runtime.service.claim("test")
    runtime.interaction_api.base_url = "http://127.0.0.1:6196"
    runtime.ensure_workspace = AsyncMock()
    runtime.prepare_interaction_config = AsyncMock(
        return_value=runtime.workspace / "client.json"
    )
    result = await runtime.call_interaction_tool(job, {"action": "open", "key": "main"})
    assert result["success"]
    assert "token" not in json.dumps(result)
    c = runtime.interaction_api.channels.get(job.scope, result["channel_id"])
    assert c["metadata"]["source_task_id"] == job.task_id
    runtime.service.finish(job, {"text": "done"})
    denied = await runtime.call_interaction_tool(
        job, {"action": "open", "key": "other"}
    )
    assert not denied["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_success", [False, True])
async def test_real_open_error_reaches_model_before_model_reports_blocked(runtime, prior_success):
    from astrbot.core.star.context import Context
    from astrbot_plugin_private_companion.task_agent_executor import TaskAgentExecutor
    from astrbot_plugin_private_companion.task_service import TaskFailure
    from astrbot_plugin_private_companion.tests.test_task_agent_executor import (
        Provider,
        Response,
    )

    original = runtime.plugin.context
    context = Context.__new__(Context)
    context.get_config = original.get_config
    context.get_llm_tool_manager = original.get_llm_tool_manager
    runtime.plugin.context = context
    e = event()
    register(runtime, e)
    await runtime.submit(
        e, {"goal": "建立互动通道", "mode": "tools", "use_workspace": False}
    )
    job = runtime.service.claim("test")
    calls = [("pc_interaction_channel", {"action": "list"})] if prior_success else []
    calls.append(("pc_interaction_channel", {"action": "open", "key": "test"}))
    provider = Provider(
        Response(calls=calls),
        Response('{"task_status":"blocked","text":"互动接口当前未就绪，通道尚未建立"}'),
    )
    tool = runtime.resolve_tools(job, ("pc_interaction_channel",))[
        "pc_interaction_channel"
    ]
    observed = await runtime.execute_tool(job, tool, {"action": "open", "key": "probe"})
    assert observed["status"] == "error", observed
    assert "interaction_api_unavailable" in observed["content"], observed
    executor = TaskAgentExecutor(
        runtime.service,
        resolve_provider=lambda _: provider,
        resolve_tools=runtime.resolve_tools,
        execute_tool=runtime.execute_tool,
        authorize=runtime.authorize_tool,
    )
    with pytest.raises(TaskFailure) as failure:
        await executor.execute(job)
    assert failure.value.error_kind == "task_incomplete"
    assert failure.value.retryable is False
    assert runtime.service.fail(job, failure.value.error_kind)
    saved = runtime.service.get(job.task_id, job.scope)
    assert saved.status == "failed"
    errors = [s for s in saved.steps.values() if s.get("kind") == "tool" and s.get("outcome", {}).get("status") == "error"]
    assert len(errors) == 1
    assert errors[0]["outcome"]["status"] == "error"
    assert "interaction_api_unavailable" in errors[0]["outcome"]["content"]
    assert errors[0]["outcome"]["receipt"]["observed_output"] is True
    assert len(errors[0]["receipts"]) == int(prior_success)
    assert runtime.interaction_api.channels.list(job.scope) == []


@pytest.mark.asyncio
async def test_schedule_runs_real_open_tool_after_workspace_preparation(runtime):
    from astrbot.core.star.context import Context
    from astrbot.core.tools.cron_tools import FutureTaskTool
    from astrbot_plugin_private_companion.tests.test_task_agent_executor import (
        Provider,
        Response,
    )

    runtime.test_tools.append(FutureTaskTool())
    original = runtime.plugin.context
    context = Context.__new__(Context)
    context.get_config = lambda **_: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": True}}
    }
    context.get_llm_tool_manager = original.get_llm_tool_manager
    provider = Provider(
        Response(calls=[("pc_interaction_channel", {"action": "open", "key": "test"})]),
        Response('{"task_status":"completed","text":"通道已建立"}'),
    )
    context.get_provider_by_id = lambda _: provider
    runtime.plugin.context = context
    runtime._account_setup = SimpleNamespace(
        prepare=AsyncMock(return_value=SimpleNamespace(ready=True))
    )
    runtime._workspace_directory = SimpleNamespace(
        call=AsyncMock(return_value={"workspaces": [], "total": 0, "next_offset": None})
    )
    runtime.interaction_api.base_url = "http://127.0.0.1:6196"
    runtime.prepare_interaction_config = AsyncMock(
        return_value=runtime.workspace / "test.json"
    )
    e = event()
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=("schedule",), reply=True),
        reply_eligible=True,
    )
    await runtime.submit(e, {"goal": "建立互动通道"}, source="hub")
    job = runtime.service.claim("test")
    result = await runtime.execute(job)
    assert result["successful_tool_names"] == ["pc_interaction_channel"]
    assert len(runtime.interaction_api.channels.list(job.scope)) == 1
    runtime._account_setup.prepare.assert_awaited_once_with(runtime.actor(e))
    runtime._workspace_directory.call.assert_awaited_once_with(
        runtime.actor(e), action="info"
    )
    runtime.prepare_interaction_config.assert_awaited_once()


def test_script_tool_receipts_do_not_auto_send_a_second_message(runtime):
    e = event()
    register(runtime, e)
    metadata = {
        **runtime._events[e],
        "script_channel_id": "channel",
        "script_tool_call": {"name": "web_search_tavily", "arguments": {}},
    }
    runtime.service.submit(
        runtime.scope(e),
        "script",
        "one",
        "脚本查询",
        runtime.workspace,
        metadata=metadata,
    )
    job = runtime.service.claim("test")
    runtime.service.finish(job, {"text": "结果"})
    notice = runtime.service.claim_notification("test")
    assert runtime._notification_current(notice) is None
    runtime.service.ack_notification(notice)
    notice = runtime.service.claim_notification("test")
    assert notice.kind == "result"
    assert runtime._notification_current(notice) is None


@pytest.mark.asyncio
async def test_script_api_calls_original_tool_without_another_task_model_request(
    runtime,
):
    c = channel(runtime)
    client = TestClient(TestServer(runtime.interaction_api.app))
    await client.start_server()
    try:
        auth = {"Authorization": "Bearer " + c["token"]}
        response = await client.post(
            "/v1/tool-call",
            headers=auth,
            json={"key": "search", "name": "web_search_tavily", "arguments": {}},
        )
        assert response.status == 200
        receipt = await response.json()
        job = runtime.service.claim("test")
        assert job.task_id == receipt["task_id"]
        runtime.execute_tool = AsyncMock(
            return_value={
                "status": "success",
                "content": "真实搜索结果",
                "receipt": {"observed": True},
            }
        )
        result = await runtime.execute(job)
        assert result["text"] == "真实搜索结果"
        runtime.execute_tool.assert_awaited_once()
        forbidden = await client.post(
            "/v1/tool-call",
            headers=auth,
            json={"key": "bad", "name": "localShell", "arguments": {}},
        )
        assert forbidden.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_early_message_hook_does_not_require_llm_or_consume_chat(runtime):
    from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

    c = channel(runtime)
    plugin = SimpleNamespace(enabled=True, _persistent_task_runtime=runtime)
    e = event(mid="early")
    await PrivateCompanionPlugin.observe_script_interaction_input.__wrapped__(plugin, e)
    assert runtime.interaction_api.channels.events(c["id"])["total_messages"] == 1
    assert not e.is_stopped()


@pytest.mark.asyncio
async def test_script_scheduler_failure_does_not_stop_existing_notifications(runtime):
    api = runtime.interaction_api
    api.start = AsyncMock()
    api.stop = AsyncMock()
    api.base_url = "http://127.0.0.1:6196"
    api.script_runner.tick = AsyncMock(
        side_effect=RuntimeError("script scheduler failed")
    )
    api.script_runner.stop = AsyncMock()

    async def original_notifications():
        runtime._stop.set()
        return False

    runtime.send_once = AsyncMock(side_effect=original_notifications)
    await asyncio.gather(runtime._interaction_loop(), runtime._sender_loop())
    runtime.send_once.assert_awaited_once()
    api.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_tool_renews_expired_owned_channel_without_recreating(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "修正已有互动", "use_workspace": True})
    job = runtime.service.claim("test")
    api = runtime.interaction_api
    c = api.channels.open(job.scope, "existing", copy.deepcopy(job.metadata), ttl_seconds=10)
    api.channels.clock = lambda: c["expires_at"] + 1
    result = await runtime.call_interaction_tool(job, {
        "action": "renew", "channel_id": c["id"], "ttl_seconds": 100,
        "cancel_on_input": True,
    })
    assert result["success"] is True, result
    assert result["channel_id"] == c["id"]
    assert result["expires_at"] == c["expires_at"] + 101
    assert result["cancel_on_input"] is True
    assert c["token"] not in json.dumps(result)
    assert len(api.channels.list(job.scope)) == 1
    api.channels.control(c["id"], "close")
    denied = await runtime.call_interaction_tool(job, {
        "action": "renew", "channel_id": c["id"], "ttl_seconds": 100,
    })
    assert denied["code"] == "channel_closed"


@pytest.mark.asyncio
async def test_channel_tool_explains_future_expiry_and_lists_existing_script(runtime, tmp_path):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "调整已有未来脚本", "use_workspace": True})
    job = runtime.service.claim("test")
    api = runtime.interaction_api
    c = api.channels.open(job.scope, "existing", copy.deepcopy(job.metadata), ttl_seconds=100)
    path = tmp_path / "own.py"
    path.write_text("print('ok')", encoding="utf-8")
    runtime.get_execution_broker = lambda _: SimpleNamespace(checked_path=lambda *_, **__: path)
    first = await api.script_runner.schedule(c, path="own.py", key="existing", run_at=c["expires_at"] - 10)
    result = await runtime.call_interaction_tool(job, {
        "action": "run", "channel_id": c["id"], "path": "own.py", "key": "future",
        "run_at": c["expires_at"] + 100,
    })
    assert result["code"] == "script_starts_after_channel_expiry"
    assert result["expires_at"] == c["expires_at"]
    assert result["run_at"] == c["expires_at"] + 100
    assert "renew" in result["hint"]
    rows = await runtime.call_interaction_tool(job, {"action": "list_scripts", "channel_id": c["id"]})
    assert rows["success"] is True
    assert [r["id"] for r in rows["scripts"]] == [first["id"]]
    runtime.plugin._is_private_companion_owner_user_id = lambda _: False
    denied = await runtime.call_interaction_tool(job, {"action": "renew", "channel_id": c["id"], "ttl_seconds": 100})
    assert denied["success"] is False


@pytest.mark.asyncio
async def test_owner_can_close_expired_channel_without_renewing_it(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "停止旧互动", "use_workspace": True})
    job = runtime.service.claim("test")
    api = runtime.interaction_api
    c = api.channels.open(job.scope, "expired", copy.deepcopy(job.metadata), ttl_seconds=10)
    api.channels.clock = lambda: c["expires_at"] + 1
    closed = await runtime.call_interaction_tool(job, {"action": "close", "channel_id": c["id"]})
    assert closed["success"] is True, closed
    stored = api.channels.get(job.scope, c["id"], include_inactive=True)
    assert stored["status"] == "closed"
    assert stored["expires_at"] == c["expires_at"]


@pytest.mark.asyncio
async def test_open_prepares_real_account_without_semantic_workspace_flag(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "建立用户要求的互动", "use_workspace": False})
    job = runtime.service.claim("test")
    runtime.interaction_api.base_url = "http://127.0.0.1:6196"
    order = []

    async def ready(actual_job):
        assert actual_job.task_id == job.task_id
        order.append("prepare")

    async def config(_):
        order.append("config")
        return runtime.workspace / "client.json"

    runtime.ensure_workspace = AsyncMock(side_effect=ready)
    runtime.prepare_interaction_config = AsyncMock(side_effect=config)
    result = await runtime.call_interaction_tool(job, {"action": "open", "key": "main"})
    assert result["success"] is True, result
    assert order == ["prepare", "config"]
    runtime.ensure_workspace.assert_awaited_once_with(job)


@pytest.mark.asyncio
async def test_script_tools_use_current_authorized_registry_not_saved_snapshot(runtime):
    e = event()
    register(runtime, e)
    metadata = copy.deepcopy(runtime._events[e])
    metadata["request"]["tool_names"] = []
    c = runtime.interaction_api.channels.open(runtime.scope(e), "old-empty-catalog", metadata)
    client = TestClient(TestServer(runtime.interaction_api.app))
    await client.start_server()
    try:
        auth = {"Authorization": "Bearer " + c["token"]}
        listing = await (await client.get("/v1/tools", headers=auth)).json()
        names = {tool["name"] for tool in listing["tools"]}
        assert "web_search_tavily" in names
        assert "pc_interaction_channel" not in names
        response = await client.post("/v1/tool-call", headers=auth, json={
            "key": "current-tool", "name": "web_search_tavily", "arguments": {},
        })
        assert response.status == 200, await response.text()
        receipt = await response.json()
        job = runtime.service.get(receipt["task_id"], runtime.scope(e))
        assert job.metadata["script_channel_id"] == c["id"]
        assert job.metadata["script_tool_call"]["name"] == "web_search_tavily"
        runtime.test_tools[:] = [tool for tool in runtime.test_tools if tool.name != "web_search_tavily"]
        unavailable = await client.post("/v1/tool-call", headers=auth, json={
            "key": "removed-tool", "name": "web_search_tavily", "arguments": {},
        })
        assert unavailable.status == 403
        runtime.plugin._is_private_companion_owner_user_id = lambda _: False
        assert (await client.get("/v1/tools", headers=auth)).status == 403
        runtime.plugin.context.send_message.assert_not_awaited()
    finally:
        await client.close()
