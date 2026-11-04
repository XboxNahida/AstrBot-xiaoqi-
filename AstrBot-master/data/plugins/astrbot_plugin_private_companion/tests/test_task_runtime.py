import copy
import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.cron.events import CronMessageEvent
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.tools.cron_tools import FutureTaskTool

try:
    from astrbot_plugin_private_companion.task_runtime import CompanionTaskRuntime
except ImportError:
    CompanionTaskRuntime = None


def event(user="owner", mid="message"):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.sender = MessageMember(user_id=user, nickname="same nickname")
    msg.group_id = "group"
    msg.message_id = mid
    msg.message = []
    msg.message_str = "查最新消息"
    e = AstrMessageEvent(
        msg.message_str, msg, PlatformMetadata("aiocqhttp", "QQ", "bot"), "group"
    )
    e.is_at_or_wake_command = True
    return e


@pytest.fixture
def runtime(tmp_path):
    assert CompanionTaskRuntime is not None, "宿主接入尚未实现"
    tools = [
        FunctionTool(name=n, description=n, parameters={"type": "object"})
        for n in ("web_search_tavily", "localShell", "pc_generate_photo")
    ]
    context = SimpleNamespace(
        get_llm_tool_manager=lambda: SimpleNamespace(
            func_list=tools, builtin_func_list={}
        ),
        get_provider_by_id=lambda _: None,
        get_config=lambda **_: {
            "provider_settings": {
                "web_search": True,
                "websearch_provider": "tavily",
                "proactive_capability": {"add_cron_tools": False},
            }
        },
        send_message=AsyncMock(return_value=True),
    )
    plugin = SimpleNamespace(
        context=context,
        conversation_task_provider_id="task-primary",
        conversation_task_fallback_provider_id="task-backup",
        conversation_chat_provider_id="chat",
        task_dashboard_primary_user_id="owner",
        _event_permission_identity_id=lambda e: e.get_sender_id(),
        _is_private_companion_owner_user_id=lambda u: u == "owner",
        _activate_persona_for_event_context=AsyncMock(return_value=(None, "xiaoqi")),
        _deactivate_persona_for_event=lambda _: None,
    )
    rt = CompanionTaskRuntime(plugin, tmp_path / "data", workspace=tmp_path / "tasks")
    rt.test_tools = tools
    rt.test_request = SimpleNamespace(
        prompt="查最新消息 api_key=topsecret",
        system_prompt="persona",
        contexts=[],
        func_tool=ToolSet(tools),
        image_urls=[],
        required_tool_names=(),
    )
    return rt


def register(rt, e, **kwargs):
    return rt.register(
        e,
        rt.test_request,
        SimpleNamespace(route="chat", actions=(), reply=True),
        reply_eligible=True,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,actions", [("chat", ()), ("task", ()), ("task", ("schedule",))]
)
async def test_official_future_tool_registers_without_mutating_schema(
    runtime, route, actions
):
    official = FutureTaskTool()
    original = copy.deepcopy(official.parameters)
    runtime.test_tools.append(official)
    runtime.plugin.context.get_config = lambda **_: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": True}}
    }
    decision = SimpleNamespace(route=route, actions=actions, reply=True)
    e = event()
    assert runtime.register(e, runtime.test_request, decision, reply_eligible=True)
    registry = runtime._registry(e.unified_msg_origin)
    wrapped = registry["future_task"]
    assert wrapped.parameters["required"] == ["action"]
    assert wrapped.parameters["properties"]["task_kind"]["enum"] == [
        "reminder",
        "monitor",
    ]
    assert runtime._registry(e.unified_msg_origin)["future_task"] is wrapped
    assert official.parameters == original
    if route == "task":
        receipt = await runtime.submit(
            e, {"action": "submit", "goal": "检查功能"}, source="hub"
        )
        assert receipt["accepted"]
        job = runtime.service.get(receipt["task_id"], runtime.scope(e))
        assert job.metadata["request"]["required_tool_names"] == []
        if actions:
            assert job.metadata["request"]["required_tool_groups"] == [
                ["future_task", "pc_interaction_channel"]
            ]
    else:
        assert runtime.make_intent_tool(e).name == "pc_submit_task_intent"


@pytest.mark.asyncio
async def test_original_hub_and_reply_dedupe_with_durable_safe_request(runtime):
    e = event()
    assert register(runtime, e)
    hub = await runtime.submit(e, {"action": "submit", "goal": "查版本"}, source="hub")
    reply = await runtime.submit(e, {"action": "submit", "goal": "查版本"})
    assert hub["task_id"] == reply["task_id"]
    assert not reply["created"]
    task = runtime.service.get(hub["task_id"], runtime.scope(e))
    assert task.metadata["request"]["primary"] == "task-primary"
    assert task.metadata["request"]["fallback"] == "task-backup"
    assert "localShell" not in task.metadata["request"]["tool_names"]
    assert "topsecret" not in json.dumps(task.metadata)
    assert task.metadata["group_id"] == "group"
    assert "is_owner" not in task.metadata


@pytest.mark.asyncio
async def test_hub_chat_filter_does_not_hide_safe_registry_tools(runtime):
    e = event()
    runtime.test_request.func_tool = None
    register(runtime, e)
    accepted = await runtime.submit(e, {"goal": "查版本"})
    assert accepted["accepted"]
    task = runtime.service.get(accepted["task_id"], runtime.scope(e))
    assert "web_search_tavily" in task.metadata["request"]["tool_names"]
    assert "localShell" not in task.metadata["request"]["tool_names"]
    runtime.plugin.context.get_config = lambda **_: {
        "provider_settings": {"web_search": False}
    }
    assert "web_search_tavily" not in runtime.resolve_tools(task, ["web_search_tavily"])


@pytest.mark.asyncio
async def test_tool_execution_records_real_error_and_timeout_unknown(
    runtime, monkeypatch
):
    from astrbot_plugin_private_companion import task_runtime as module

    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查版本"})
    job = runtime.service.claim("test")
    monkeypatch.setattr(module, "AstrAgentContext", lambda **kw: SimpleNamespace(**kw))

    async def failure(tool, context, **args):
        assert context.context.event.get_sender_id() == "owner"
        yield SimpleNamespace(
            isError=False,
            content=[
                SimpleNamespace(
                    type="text", text='{"success":false,"reason":"real failure"}'
                )
            ],
        )

    monkeypatch.setattr(module.FunctionToolExecutor, "execute", failure)
    result = await runtime.execute_tool(job, runtime.test_tools[0], {})
    assert result["status"] == "error"

    async def timeout(tool, context, **args):
        raise TimeoutError("possibly committed")
        yield

    monkeypatch.setattr(module.FunctionToolExecutor, "execute", timeout)
    with pytest.raises(module.TaskFailure) as caught:
        await runtime.execute_tool(job, runtime.test_tools[0], {})
    assert caught.value.error_kind == "unknown_result"
    assert not caught.value.retryable


@pytest.mark.asyncio
async def test_real_context_and_function_tool_executor(runtime):
    from astrbot.core.star.context import Context

    class SearchTool(FunctionTool):
        async def call(self, context, **args):
            assert isinstance(context.context.context, Context)
            assert context.context.event.get_sender_id() == "owner"
            return '{"status":"success","items":["actual tool output"]}'

    tool = SearchTool(
        name="web_search_tavily", description="test", parameters={"type": "object"}
    )
    runtime.test_tools[0] = tool
    original = runtime.plugin.context
    context = Context.__new__(Context)
    context.get_config = original.get_config
    context.get_llm_tool_manager = original.get_llm_tool_manager
    runtime.plugin.context = context
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查版本"})
    job = runtime.service.claim("test")
    outcome = await runtime.execute_tool(job, tool, {})
    assert outcome["status"] == "success"
    assert "actual tool output" in outcome["content"]


@pytest.mark.asyncio
async def test_no_registration_cron_and_known_synthetic_fail_closed(runtime):
    assert not (await runtime.submit(event(), {"goal": "伪造"}))["accepted"]
    for flag in (
        "_private_companion_task_runtime_event",
        "_private_companion_proactive_event",
        "synthetic",
        "is_proactive",
    ):
        e = event(mid=flag)
        setattr(e, flag, True)
        assert not register(runtime, e)
        assert not (await runtime.submit(e, {"goal": "伪造"}))["accepted"]
    e = event()
    cron = CronMessageEvent(
        context=runtime.plugin.context, session=e.session, message="cron"
    )
    assert not register(runtime, cron)


@pytest.mark.asyncio
async def test_bound_function_tool_cannot_take_another_context_identity(runtime):
    e = event()
    register(runtime, e)
    tool = runtime.make_intent_tool(e)
    assert isinstance(tool, FunctionTool)
    assert tool.name == "pc_submit_task_intent"
    result = json.loads(
        await tool.call(
            SimpleNamespace(context=SimpleNamespace(event=event("attacker"))),
            action="submit",
            goal="查版本",
        )
    )
    assert result["accepted"]
    task = runtime.service.get(result["task_id"], runtime.scope(e))
    assert task.scope.sender_id == "owner"


@pytest.mark.asyncio
async def test_reply_reasoning_mode_has_no_tools_or_execution_receipts(runtime):
    e = event()
    register(runtime, e)
    result = json.loads(
        await runtime.make_intent_tool(e).call(
            None, action="submit", goal="写一篇短文", mode="reasoning"
        )
    )
    assert result["accepted"]
    job = runtime.service.get(result["task_id"], runtime.scope(e))
    assert job.metadata["request"]["mode"] == "reasoning"
    assert job.metadata["request"]["tool_names"] == []
    assert job.metadata["request"]["required_tool_names"] == []
    assert runtime.authorize_job(job)


@pytest.mark.asyncio
async def test_hub_text_only_task_does_not_require_an_unrelated_tool_receipt(runtime):
    e = event()
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=(), reply=True),
        reply_eligible=True,
    )
    receipt = await runtime.submit(e, {"goal": "写一篇短文"}, source="hub")
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["mode"] == "reasoning"
    assert job.metadata["request"]["tool_names"] == []


@pytest.mark.asyncio
async def test_reply_can_request_tools_after_hub_text_only_classification(runtime):
    e = event()
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=(), reply=True),
        reply_eligible=True,
    )
    receipt = await runtime.submit(e, {"goal": "查询版本", "mode": "tools"})
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["mode"] == "tools"
    assert "web_search_tavily" in job.metadata["request"]["tool_names"]
    assert runtime._events[e]["request"]["mode"] == "reasoning"


@pytest.mark.asyncio
async def test_workspace_intent_is_durable_before_account_setup(runtime):
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(
        e, {"goal": "读取共享工具并运行", "mode": "tools", "use_workspace": True}
    )
    assert receipt["accepted"]
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["use_workspace"] is True
    assert "pc_task_read_tool" in job.metadata["request"]["tool_names"]
    assert runtime.authorize_job(job)
    runtime.plugin._is_private_companion_owner_user_id = lambda _: False
    assert not runtime.authorize_job(job)


@pytest.mark.asyncio
@pytest.mark.parametrize("user,capabilities,expected", [
    ("owner", None, True),
    ("member", ["schedule"], False),
    ("member", ["schedule", "read", "write", "shell", "network"], True),
])
async def test_reply_omitted_workspace_uses_existing_grant(runtime, user, capabilities, expected):
    if capabilities is not None:
        runtime.permissions.grant(
            runtime.dashboard_actor("bot", "group", "web-admin"), user,
            capabilities=capabilities, roots=[runtime.workspace],
        )
    runtime.test_tools.append(FutureTaskTool())
    runtime.plugin.context.get_config = lambda **_: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": True}}
    }
    e = event(user)
    register(runtime, e)
    receipt = await runtime.submit(e, {"action": "submit", "goal": "这个任务，每分钟叫一次，回复就停止"})
    assert receipt["accepted"]
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["use_workspace"] is expected
    assert ("pc_task_write_file" in job.metadata["request"]["tool_names"]) is expected
    assert job.metadata["request"].get("completion_contract") == "status_v1"
    assert runtime._events[e]["request"]["use_workspace"] is False


@pytest.mark.asyncio
async def test_reply_explicit_workspace_false_is_preserved(runtime):
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "只查询", "use_workspace": False})
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["use_workspace"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["hub", "reply"])
async def test_read_authorized_task_needs_no_model_workspace_switch(runtime, source):
    member = event("reader")
    runtime.permissions.grant(
        runtime.dashboard_actor("bot", "group", "web-admin"), "reader",
        capabilities=["read"], roots=[runtime.workspace],
    )
    register(runtime, member)
    receipt = await runtime.submit(member, {"goal": "读取同群的任务文件"}, source=source)
    job = runtime.service.get(receipt["task_id"], runtime.scope(member))
    assert job.metadata["request"]["use_workspace"] is True
    assert "pc_task_workspace" in job.metadata["request"]["tool_names"]
    assert "pc_task_write_file" not in runtime._registry(member.unified_msg_origin, runtime.actor(member))


def test_workspace_prompt_contains_real_location_and_explicit_read_routes(runtime):
    actor = runtime.actor(event())
    prompt = runtime.workspace_prompt(actor)
    from astrbot_plugin_private_companion.task_execution import actor_workspace
    context, _ = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])
    assert context["working_directory"] == str(actor_workspace(runtime.workspace, actor))
    assert "pc_task_workspace" in prompt and "workspace_id" in prompt
    assert "C:/AstrBotTasks/tools" not in prompt
    assert "never guess" in prompt.lower()


@pytest.mark.asyncio
async def test_execution_injects_fresh_peer_catalog_before_model_call(runtime, monkeypatch):
    from astrbot_plugin_private_companion import task_agent_executor, task_runtime
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "检查各群友的文件"})
    job = runtime.service.claim("test")
    assert job.task_id == receipt["task_id"]
    runtime._account_setup = SimpleNamespace(prepare=AsyncMock(return_value=SimpleNamespace(ready=True)))
    runtime._workspace_directory = SimpleNamespace(call=AsyncMock(return_value={
        "success": True, "workspaces": [{"workspace_id": "peer", "user_id": "real-peer", "path": "C:/actual-peer"}],
        "total": 1, "next_offset": None,
    }))
    monkeypatch.setattr(task_runtime, "bind_event_bot_identity", AsyncMock(return_value=None))
    execute = AsyncMock(return_value={"text": "actual result"})
    monkeypatch.setattr(task_agent_executor.TaskAgentExecutor, "execute", execute)
    await runtime.execute(job)
    current_prompt = execute.await_args.args[0].metadata["request"]["system_prompt"]
    assert "real-peer" in current_prompt and "C:/actual-peer" in current_prompt
    assert current_prompt.count("HOST WORKSPACE CONTEXT") == 1


@pytest.mark.asyncio
async def test_hub_workspace_action_is_tool_task_before_account_exists(runtime):
    e = event()
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=("workspace",), reply=True),
        reply_eligible=True,
    )
    receipt = await runtime.submit(e, {"goal": "使用共享脚本"}, source="hub")
    assert receipt["accepted"]
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["mode"] == "tools"
    assert job.metadata["request"]["use_workspace"] is True
    assert "pc_task_read_tool" in job.metadata["request"]["tool_names"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities,expected",
    [
        (None, True),
        (["schedule"], False),
        (["schedule", "read", "write", "shell", "network"], True),
    ],
)
async def test_schedule_exposes_script_workspace_only_with_existing_grant(
    runtime, capabilities, expected
):
    user = "owner" if capabilities is None else "member"
    if capabilities is not None:
        runtime.permissions.grant(
            runtime.dashboard_actor("bot", "group", "web-admin"),
            user,
            capabilities=capabilities,
            roots=[runtime.workspace],
        )
    runtime.test_tools.append(FutureTaskTool())
    runtime.plugin.context.get_config = lambda **_: {
        "provider_settings": {"proactive_capability": {"add_cron_tools": True}}
    }
    e = event(user)
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=("schedule",), reply=True),
        reply_eligible=True,
    )
    receipt = await runtime.submit(e, {"goal": "按之前要求写互动脚本"}, source="hub")
    assert receipt["accepted"]
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["use_workspace"] is expected
    assert ("pc_task_read_tool" in job.metadata["request"]["tool_names"]) is expected


@pytest.mark.asyncio
async def test_explicit_text_only_intent_clears_hub_workspace_setup(runtime):
    e = event()
    runtime.register(
        e,
        runtime.test_request,
        SimpleNamespace(route="task", actions=("workspace",), reply=True),
        reply_eligible=True,
    )
    receipt = await runtime.submit(
        e, {"goal": "仅分析", "mode": "reasoning", "use_workspace": False}
    )
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert job.metadata["request"]["use_workspace"] is False
    assert job.metadata["request"]["tool_names"] == []


def test_account_refresh_resolves_owner_in_target_persona_context(runtime):
    from contextvars import ContextVar

    active = ContextVar("test_task_owner_persona", default="other")
    runtime.plugin._activate_persona_for_event = lambda e: (
        active.set(e.get_platform_id()),
        "persona",
    )
    runtime.plugin._deactivate_persona_for_event = active.reset
    runtime.plugin._is_private_companion_owner_user_id = lambda user: (
        active.get() == "bot" and user == "owner"
    )
    actor = runtime._account_actor("bot", "group", "owner")
    assert actor.is_primary_user is True
    assert active.get() == "other"


@pytest.mark.asyncio
async def test_unauthorized_workspace_request_is_not_accepted(runtime):
    e = event("stranger")
    register(runtime, e)
    receipt = await runtime.submit(
        e, {"goal": "运行脚本", "mode": "tools", "use_workspace": True}
    )
    assert receipt["accepted"] is False
    assert receipt["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_workspace_setup_failure_stops_before_model_execution(runtime):
    from astrbot_plugin_private_companion.task_service import TaskFailure

    e = event()
    register(runtime, e)
    await runtime.submit(
        e, {"goal": "运行共享脚本", "mode": "tools", "use_workspace": True}
    )
    job = runtime.service.claim("worker")
    runtime._account_setup = SimpleNamespace(
        prepare=AsyncMock(return_value=SimpleNamespace(ready=False))
    )
    with pytest.raises(TaskFailure) as failure:
        await runtime.execute(job)
    assert failure.value.error_kind == "execution_setup_failed"
    runtime._account_setup.prepare.assert_awaited_once_with(runtime.actor(e))
    runtime.plugin._activate_persona_for_event_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_permissions_and_registry_are_rechecked(runtime):
    e = event()
    register(runtime, e)
    result = await runtime.submit(e, {"goal": "查版本"})
    job = runtime.service.claim("test")
    tool = runtime.test_tools[0]
    assert runtime.authorize_tool(job, tool, {})
    assert not runtime.authorize_tool(
        job, runtime.test_tools[2], {"reference_image_path": "C:/secrets/private.png"}
    )
    assert not runtime.authorize_tool(job, runtime.test_tools[2], {"kind": "shell"})
    runtime.plugin._is_private_companion_owner_user_id = lambda _: False
    assert not runtime.authorize_tool(job, tool, {})
    runtime.plugin._is_private_companion_owner_user_id = lambda _: True
    tool.active = False
    assert not runtime.authorize_tool(job, tool, {})
    recovered = runtime.restore_event(job)
    assert recovered.get_sender_id() == "owner"
    assert recovered.get_group_id() == "group"
    assert recovered.get_platform_name() == "aiocqhttp"
    assert recovered.unified_msg_origin == e.unified_msg_origin
    assert result["accepted"]


@pytest.mark.asyncio
async def test_member_requires_group_capability_grant_and_revocation(runtime):
    member = event("member")
    register(runtime, member)
    assert not (await runtime.submit(member, {"goal": "查版本"}))["accepted"]
    actor = runtime.dashboard_actor("bot", "group", "web-admin")
    grant = runtime.permissions.grant(
        actor, "member", capabilities=["search"], roots=[runtime.workspace]
    )
    accepted = await runtime.submit(member, {"goal": "查版本"})
    assert accepted["accepted"]
    job = runtime.service.claim("test")
    assert runtime.authorize_tool(job, runtime.test_tools[0], {})
    assert not runtime.authorize_tool(job, runtime.test_tools[2], {})
    runtime.permissions.revoke(actor, grant.grant_id)
    assert not runtime.authorize_job(job)


@pytest.mark.asyncio
async def test_outbox_generation_rechecks_state_and_send_false_is_unknown(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查版本"})
    job = runtime.service.claim("test")
    runtime.service.progress(job, "working")
    await runtime.send_once()

    async def generate(notification, current):
        runtime.service.finish(job, {"text": "verified", "receipt": "real"})
        return "迟到进度"

    runtime.render_notification = generate
    await runtime.send_once()
    assert len(runtime.plugin.context.send_message.await_args_list) == 1
    runtime.plugin.context.send_message.return_value = False
    await runtime.send_once()
    with sqlite3.connect(runtime.service.path) as db:
        assert (
            db.execute("SELECT state FROM pt_outbox WHERE kind='result'").fetchone()[0]
            == "delivery_unknown"
        )
    assert runtime.service.get(job.task_id, job.scope).status == "succeeded"


@pytest.mark.asyncio
async def test_worker_and_sender_lifecycle_and_bounded_snapshot(runtime):
    e = event()
    register(runtime, e)
    for i in range(60):
        runtime.service.submit(
            runtime.scope(e), str(i), "main", "goal" * 500, runtime.workspace
        )
    snapshot = runtime.snapshot(e)
    assert len(snapshot) <= 1600
    assert snapshot.count("task_id") <= 6
    latest = runtime.service.submit(
        runtime.scope(e), "latest", "main", "最新任务", runtime.workspace
    )
    assert latest.task_id in runtime.snapshot(e)
    runtime.start()
    assert len(runtime._tasks) == 2
    await runtime.stop()
    assert all(task.done() for task in runtime._tasks)


@pytest.mark.asyncio
async def test_saved_cron_rule_bridge_dedupes_and_never_allows_intent(runtime):
    rule = SimpleNamespace(
        job_id="rule",
        run_once=True,
        payload={
            "session": "bot:GroupMessage:group",
            "sender_id": "owner",
            "origin": "tool",
            "note": "提醒喝水",
        },
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=rule))
    )
    started = datetime(2026, 11, 3, tzinfo=timezone.utc)
    first = await runtime.submit_cron(rule, started)
    again = await runtime.submit_cron(rule, started)
    assert first["task_id"] == again["task_id"]
    assert await runtime._cron_bridge(rule, started) is True
    scope = runtime.scope(event())
    job = runtime.service.get(first["task_id"], scope)
    assert job.metadata["schedule_rule_id"] == "rule"
    assert job.metadata["request"]["mode"] == "reasoning"
    assert job.metadata["request"]["tool_names"] == []
    assert runtime.authorize_job(job)
    assert not register(runtime, runtime.restore_event(job))
    assert await runtime.send_once()
    runtime.plugin.context.send_message.assert_not_awaited()
    runtime.plugin._is_private_companion_owner_user_id = lambda _: False
    assert not runtime.authorize_job(job)


@pytest.mark.asyncio
async def test_cron_bridge_leaves_other_framework_rules_untouched(runtime):
    rule = SimpleNamespace(job_id="web-rule", payload={"origin": "dashboard"})
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=rule))
    )
    assert await runtime._cron_bridge(rule, datetime.now(timezone.utc)) is False


@pytest.mark.asyncio
async def test_saved_cron_rule_invalid_identity_fails_closed(runtime):
    rule = SimpleNamespace(
        job_id="rule",
        run_once=False,
        payload={
            "session": "bot:GroupMessage:group",
            "sender_id": "",
            "origin": "tool",
            "note": "提醒",
        },
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=rule))
    )
    with pytest.raises(ValueError):
        await runtime.submit_cron(rule, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_future_tool_marks_real_persisted_monitor_rule(runtime):

    class OfficialFuture(FunctionTool):
        async def call(self, context, **kwargs):
            assert "task_kind" not in kwargs
            return "Scheduled future task real_rule (news) expression '* * * * *'."

    tool = OfficialFuture(
        name="future_task",
        description="official",
        parameters={"type": "object", "properties": {}},
    )
    e = event()
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(
            get_cron_job=AsyncMock(
                return_value=SimpleNamespace(
                    payload={
                        "session": e.unified_msg_origin,
                        "sender_id": "owner",
                        "origin": "tool",
                        "note": "news",
                    }
                )
            )
        ),
        update_job=AsyncMock(return_value=SimpleNamespace(job_id="real_rule")),
    )
    result = await runtime._call_future_tool(
        tool,
        SimpleNamespace(context=SimpleNamespace(event=e)),
        {"action": "create", "task_kind": "monitor"},
    )
    assert result.startswith("Scheduled")
    assert (
        runtime.cron_manager.update_job.await_args.kwargs["payload"]["source"]
        == "monitor"
    )


def test_execution_tool_discovery_never_scans_acl_and_keeps_cached_identity(
    runtime, monkeypatch
):
    from astrbot_plugin_private_companion import task_runtime as module

    actor = runtime.actor(event())
    assert "pc_task_read_file" not in runtime._registry("bot:GroupMessage:group", actor)
    monkeypatch.setattr(
        module.WindowsDeploymentVerifier,
        "verify",
        lambda *_: pytest.fail(
            "Discovery must not perform synchronous ACL attestation"
        ),
    )
    vault = runtime.workspace / "test.credential.dpapi"
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_bytes(b"host credential")
    vault.with_name("test.ready.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "get_execution_broker",
        lambda _: SimpleNamespace(
            backend=SimpleNamespace(credentials=SimpleNamespace(vault=vault)),
            verifier=module.WindowsDeploymentVerifier(
                runtime.workspace, protected_roots=[]
            ),
        ),
    )
    tools = runtime._registry("bot:GroupMessage:group", actor)
    assert "pc_task_read_file" in tools
    assert "localShell" not in tools
    assert (
        tools["pc_task_read_file"]
        is runtime._registry("bot:GroupMessage:group", actor)["pc_task_read_file"]
    )


@pytest.mark.asyncio
async def test_live_global_tool_permission_is_enforced_without_owner_escalation(
    runtime, monkeypatch
):
    from astrbot_plugin_private_companion import task_runtime as module

    manager = runtime.plugin.context.get_llm_tool_manager()
    manager._check_tool_permission = AsyncMock(return_value="error: admin required")
    runtime.plugin.context.get_llm_tool_manager = lambda: manager
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    job = runtime.service.claim("worker")
    monkeypatch.setattr(module, "AstrAgentContext", lambda **kw: SimpleNamespace(**kw))
    called = []

    async def execute(*args, **kwargs):
        called.append(True)
        yield SimpleNamespace(content=[SimpleNamespace(type="text", text="ran")])

    monkeypatch.setattr(module.FunctionToolExecutor, "execute", execute)
    with pytest.raises(module.TaskFailure) as caught:
        await runtime.execute_tool(job, runtime.test_tools[0], {})
    assert caught.value.error_kind == "permission_denied"
    assert not called
    checked_event = manager._check_tool_permission.await_args.args[1].context.event
    assert not checked_event.is_admin()
    runtime.plugin.context.get_config = lambda **_: {"admins_id": ["owner"]}
    assert runtime.restore_event(job).is_admin()


@pytest.mark.asyncio
async def test_cancel_during_global_permission_lookup_prevents_execution(
    runtime, monkeypatch
):
    from astrbot_plugin_private_companion import task_runtime as module

    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    job = runtime.service.claim("worker")
    manager = runtime.plugin.context.get_llm_tool_manager()

    async def permission(*_):
        runtime.service.cancel(job.task_id, job.scope)
        return None

    manager._check_tool_permission = permission
    runtime.plugin.context.get_llm_tool_manager = lambda: manager
    monkeypatch.setattr(module, "AstrAgentContext", lambda **kw: SimpleNamespace(**kw))

    async def execute(*_, **__):
        pytest.fail("Cancelled job must not execute")
        yield

    monkeypatch.setattr(module.FunctionToolExecutor, "execute", execute)
    with pytest.raises(module.TaskFailure) as caught:
        await runtime.execute_tool(job, runtime.test_tools[0], {})
    assert caught.value.error_kind == "lease_lost"


@pytest.mark.asyncio
async def test_recovered_group_type_and_dashboard_group_scope(runtime):
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "查公告"})
    job = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert not runtime.restore_event(job).is_private_chat()
    runtime.plugin._bot_scope_allows_event = lambda ev: not ev.is_private_chat()
    assert runtime.dashboard_actor("bot", "group", "web-admin") is not None


@pytest.mark.asyncio
async def test_notification_uses_chat_backup_and_actual_checkpoint_evidence(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    job = runtime.service.claim("worker")
    runtime.service.record_step(
        job,
        "main",
        {
            "kind": "tool",
            "receipts": [
                {"name": "web_search_tavily", "receipt": {"observed_output": True}}
            ],
            "contexts": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "content": "version 2 released",
                            "receipt": {"tool": "web_search_tavily"},
                        }
                    ),
                }
            ],
        },
        next_step="tool:2",
    )
    primary = SimpleNamespace(text_chat=AsyncMock(side_effect=TimeoutError))
    backup = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="查到了新版公告。", tools_call_name=[]
            )
        )
    )
    runtime.plugin.conversation_chat_fallback_provider_id = "backup"
    runtime.plugin.context.get_provider_by_id = lambda pid: {
        "chat": primary,
        "backup": backup,
    }.get(pid)
    current = runtime.service.get(job.task_id, job.scope)
    rendered = await runtime.render_notification(
        SimpleNamespace(kind="progress", payload={}), current
    )
    assert rendered == "查到了新版公告。"
    facts = json.loads(backup.text_chat.await_args.kwargs["prompt"].split("\n", 1)[1])
    assert "version 2 released" in json.dumps(facts)
    assert "web_search_tavily" in json.dumps(facts)


@pytest.mark.asyncio
async def test_monitor_policy_uses_actual_tool_result_for_no_update(runtime):
    rule = SimpleNamespace(
        job_id="monitor",
        run_once=False,
        payload={
            "session": "bot:GroupMessage:group",
            "sender_id": "owner",
            "origin": "tool",
            "source": "monitor",
            "note": "查版本",
        },
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=rule))
    )
    receipt = await runtime.submit_cron(rule, datetime.now(timezone.utc))
    job = runtime.service.claim("worker")
    assert "future_task" not in job.metadata["request"]["tool_names"]
    runtime.service.record_step(
        job,
        "main",
        {
            "kind": "tool",
            "contexts": [
                {
                    "role": "tool",
                    "content": json.dumps(
                        {
                            "status": "success",
                            "receipt": {"real": True},
                            "content": json.dumps(
                                {"has_update": False, "fingerprint": "unchanged"}
                            ),
                        }
                    ),
                }
            ],
        },
        next_step="tool:2",
    )
    runtime.service.finish(job, {"text": "model incorrectly claims update"})
    await runtime.send_once()
    await runtime.send_once()
    runtime.plugin.context.send_message.assert_not_awaited()
    assert runtime.snapshot_for_scope("bot", "bot:GroupMessage:group", "owner")
    assert not runtime.snapshot_for_scope("other", "bot:GroupMessage:group", "owner")
    assert runtime.service.get(receipt["task_id"], job.scope).status == "succeeded"


@pytest.mark.asyncio
async def test_notifications_keep_original_chat_memory_and_fresh_same_session(runtime):
    e = event()
    runtime.test_request.system_prompt = "原有小七人格和对话示例"
    runtime.test_request.contexts = [{"role": "user", "content": "叫我小林"}]
    runtime.test_request.extra_user_content_parts = [
        TextPart(text="偏好称呼小林；当前情绪嘴硬但愿意帮忙")
    ]
    runtime.test_request.conversation = SimpleNamespace(cid="same-session")
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "查公告"})
    runtime.test_request.system_prompt = "任务执行器的其他指令"
    runtime.test_request.contexts.clear()
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="same-session"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(
                history=json.dumps(
                    [
                        {"role": "user", "content": "刚才说的事别忘了"},
                        {"role": "assistant", "content": "记着呢。"},
                    ]
                )
            )
        ),
    )
    runtime.plugin.context.conversation_manager = manager
    chat = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="行，我看看。", tools_call_name=[]
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: chat
    current = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert (
        await runtime.render_notification(
            SimpleNamespace(kind="accepted", payload={}), current
        )
        == "行，我看看。"
    )
    request = chat.text_chat.await_args.kwargs
    assert "原有小七人格" in request["system_prompt"]
    assert "任务执行器的其他指令" not in request["system_prompt"]
    assert "刚才说的事别忘了" in json.dumps(request["contexts"], ensure_ascii=False)
    assert "偏好称呼小林" in str(request.get("extra_user_content_parts"))
    assert request["func_tool"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivered,switched", [(True, False), (False, False), (True, True)]
)
async def test_only_sent_notifications_are_remembered_in_the_original_chat(
    runtime, delivered, switched
):
    e = event()
    runtime.test_request.conversation = SimpleNamespace(cid="original")
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(
            return_value="new-chat" if switched else "original"
        ),
        get_conversation=AsyncMock(return_value=SimpleNamespace(history="[]")),
        add_message_pair=AsyncMock(),
    )
    runtime.plugin.context.conversation_manager = manager
    runtime.plugin.context.send_message.return_value = delivered
    await runtime.send_once()
    assert manager.add_message_pair.await_count == int(delivered and not switched)
    if delivered and not switched:
        saved = manager.add_message_pair.await_args.kwargs
        assert saved["cid"] == "original"
        assert saved["user_message"]["content"] == e.message_str
        assert str(saved["assistant_message"])


@pytest.mark.asyncio
async def test_sent_history_failure_never_requeues_already_delivered_notice(runtime):
    e = event()
    runtime.test_request.conversation = SimpleNamespace(cid="original")
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    runtime.plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="original"),
        get_conversation=AsyncMock(return_value=SimpleNamespace(history="[]")),
        add_message_pair=AsyncMock(side_effect=RuntimeError("locked")),
    )
    await runtime.send_once()
    assert not await runtime.send_once()
    assert runtime.plugin.context.send_message.await_count == 1
    with sqlite3.connect(runtime.service.path) as db:
        assert db.execute("SELECT state FROM pt_outbox").fetchone()[0] == "sent"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["accepted", "progress", "result", "state"])
async def test_all_task_notifications_strip_controls_but_preserve_literal_code(
    runtime, kind
):
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "写示例代码"})
    code = "```python\nprint('&&reply&&')\n```"
    chat = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant",
                completion_text="<think>内部推理</think>行，给你。 &&reply&&\n"
                + code
                + "\n[[PCTTS:private]]",
                tools_call_name=[],
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: chat
    current = runtime.service.get(receipt["task_id"], runtime.scope(e))
    text = await runtime.render_notification(
        SimpleNamespace(kind=kind, payload={}), current
    )
    assert "内部推理" not in text
    assert "[[PCTTS:" not in text
    assert "给你。 &&reply&&" not in text
    assert code in text


@pytest.mark.asyncio
async def test_send_boundary_also_cleans_non_model_notification(runtime):
    e = event()
    register(runtime, e)
    await runtime.submit(e, {"goal": "查公告"})
    runtime.render_notification = AsyncMock(return_value="行，我看看。 &&reply&&")
    await runtime.send_once()
    sent = runtime.plugin.context.send_message.await_args.args[1].chain[0].text
    assert sent == "行，我看看。"


@pytest.mark.parametrize(
    "text", ["查到了吗", "做完了吗？", "有结果了吗", "进度怎么样", "还要多久"]
)
def test_progress_questions_read_existing_task_even_if_hub_suggests_schedule(
    runtime, text
):
    e = event()
    register(runtime, e)
    task = runtime.service.submit(
        runtime.scope(e),
        "earlier-message",
        "main",
        "查询任务列表",
        workdir=runtime.workspace,
    )
    e.message_str = text
    decision = SimpleNamespace(
        reply=True, task_query=False, route="task", actions=("schedule",)
    )
    assert runtime.prepare_task_query(e, runtime.test_request, decision)
    assert len(runtime.service.list(runtime.scope(e))) == 1
    assert task.task_id in runtime.test_request.system_prompt
    assert "queued" in runtime.test_request.system_prompt


@pytest.mark.parametrize(
    "text",
    [
        "重新查一下",
        "查到了吗，顺便查天气",
        "取消任务",
        "继续做",
        "查一下有没有定时任务",
        "帮我查到了吗这句话怎么翻译",
    ],
)
def test_new_work_and_control_are_not_swallowed_as_status_questions(runtime, text):
    e = event()
    register(runtime, e)
    runtime.service.submit(
        runtime.scope(e),
        "earlier-message",
        "main",
        "查询任务列表",
        workdir=runtime.workspace,
    )
    e.message_str = text
    assert not runtime.prepare_task_query(
        e, runtime.test_request, SimpleNamespace(reply=True, task_query=False)
    )


def test_progress_query_does_not_cross_sender_or_bot(runtime):
    e = event()
    register(runtime, e)
    runtime.service.submit(
        runtime.scope(e),
        "earlier-message",
        "main",
        "查询任务列表",
        workdir=runtime.workspace,
    )
    other = event(user="other-user")
    register(runtime, other)
    other.message_str = "查到了吗"
    assert not runtime.prepare_task_query(
        other, runtime.test_request, SimpleNamespace(reply=True, task_query=True)
    )


@pytest.mark.asyncio
async def test_unfinished_task_cannot_announce_completion_even_if_chat_claims_it(
    runtime,
):
    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "查公告"})
    chat = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant",
                completion_text="已经完成了，全部搞定。",
                tools_call_name=[],
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: chat
    current = runtime.service.get(receipt["task_id"], runtime.scope(e))
    text = await runtime.render_notification(
        SimpleNamespace(kind="accepted", payload={}), current
    )
    assert "全部搞定" not in text
    assert "已经完成" not in text


def test_completed_query_uses_verified_result_and_hub_query_cannot_hide_new_work(
    runtime,
):
    e = event()
    register(runtime, e)
    runtime.service.submit(
        runtime.scope(e), "earlier-message", "main", "算结果", workdir=runtime.workspace
    )
    job = runtime.service.claim("worker")
    runtime.service.finish(job, {"text": "精确结果12.3400"})
    e.message_str = "查到了吗"
    assert runtime.prepare_task_query(
        e, runtime.test_request, SimpleNamespace(reply=True, task_query=False)
    )
    assert "精确结果12.3400" in runtime.test_request.system_prompt
    e.message_str = "查到了吗，顺便查天气"
    assert not runtime.prepare_task_query(
        e, runtime.test_request, SimpleNamespace(reply=True, task_query=True)
    )


@pytest.mark.asyncio
async def test_chat_snapshot_preserves_text_parts_without_persisting_media_payload(
    runtime,
):
    e = event()
    runtime.test_request.contexts = [
        {"role": "user", "content": [{"type": "text", "text": f"第{i}句"}]}
        for i in range(20)
    ]
    runtime.test_request.contexts[-1]["content"].append(
        {"type": "image_url", "image_url": {"url": "private-image-payload"}}
    )
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "查公告"})
    current = runtime.service.get(receipt["task_id"], runtime.scope(e))
    assert len(current.metadata["chat_context"]["contexts"]) == 20
    assert "private-image-payload" not in json.dumps(current.metadata["chat_context"])
    chat = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="行，我看看。", tools_call_name=[]
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: chat
    await runtime.render_notification(
        SimpleNamespace(kind="accepted", payload={}), current
    )
    assert chat.text_chat.await_args.kwargs["contexts"][-1]["content"] == "第19句"


@pytest.mark.parametrize("closing", ["</think>", ""])
@pytest.mark.parametrize("tag", ["think", "tts"])
def test_control_cleaner_never_preserves_code_from_internal_reasoning(closing, tag):
    from astrbot_plugin_private_companion.conversation_route import _task_chat_text

    raw = f"<{tag}>内部推理\n```text\n不可外发的推理\n```\n" + closing.replace(
        "think", tag
    )
    assert "不可外发" not in _task_chat_text(raw)
    assert "内部推理" not in _task_chat_text(raw)
