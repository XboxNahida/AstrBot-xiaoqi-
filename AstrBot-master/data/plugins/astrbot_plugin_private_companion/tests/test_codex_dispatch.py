"""Exercise real submission routing with isolated fake Codex transports."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.codex_runtime import CodexRuntime
from astrbot_plugin_private_companion.tests import test_task_runtime as runtime_helpers
from astrbot_plugin_private_companion.tests.test_codex_runtime import Gateway, Rpc
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

runtime = runtime_helpers.runtime


@pytest.fixture
def routed(runtime):
    providers = {}
    for ident, name in (("catalog", "gpt-6-sol"), ("gpt-in-id", "gemini-3")):
        providers[ident] = SimpleNamespace(
            provider_config={
                "enable": True,
                "model": name,
                "api_base": "https://fake.invalid/v1",
            },
            get_model=lambda name=name: name,
            get_current_key=lambda: "fake-host-key",
            text_chat=AsyncMock(),
        )
    runtime.plugin.context.get_provider_by_id = providers.get
    runtime.plugin.conversation_task_provider_id = "catalog"
    runtime.plugin.page_api = SimpleNamespace(
        _available_provider_items=lambda: [
            {"id": key, "model": value.get_model()} for key, value in providers.items()
        ]
    )
    runtime.ensure_workspace = AsyncMock()
    calls = []

    async def connect(**kwargs):
        rpc = Rpc(len(calls))
        calls.append(rpc)
        return rpc

    runtime.codex = CodexRuntime(runtime, connect_factory=connect, gateway=Gateway())
    return runtime, calls, providers


@pytest.mark.asyncio
async def test_model_catalog_reports_live_independent_defaults(routed):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    rt.plugin.task_execution_engine = "native"
    rt.plugin.codex_reasoning_effort = "xhigh"
    rt.plugin.conversation_chat_provider_id = "gemini-chat"
    before = dict(providers["catalog"].provider_config)
    try:
        result = json.loads(await rt.make_model_tool(e).call(None))
        assert result["defaults"]["task_execution_engine"] == "native"
        assert result["defaults"]["codex_reasoning_effort"] == "xhigh"
        assert "task_execution_engine" not in rt.default_model_request()
        assert "codex_reasoning_effort" not in rt.default_model_request()
        assert providers["catalog"].provider_config == before
        assert rt.plugin.conversation_chat_provider_id == "gemini-chat"
        assert not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_default_gpt_marked_before_service_submit_and_not_native_claimed(routed):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    original = rt.service.submit

    def submit(*args, **kwargs):
        assert kwargs["metadata"]["engine"] == "codex"
        return original(*args, **kwargs)

    rt.service.submit = submit
    try:
        result = await rt.submit(e, {"goal": "执行目标"})
        assert result["accepted"] and result["thread_id"]
        assert result["engine"] == "codex" and result["status"] == "running"
        assert result["model"] == "gpt-6-sol" and result["reasoning_effort"] == "medium"
        assert rt.service.claim("native") is None
        duplicate = await rt.submit(e, {"goal": "执行目标"})
        assert duplicate["thread_id"] == result["thread_id"] and len(calls) == 1
        assert len([m for m, _ in calls[0].calls if m == "turn/start"]) == 1
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ident,setting",
    [("gpt-in-id", "codex"), ("catalog", "native"), ("unknown", "codex")],
)
async def test_non_gpt_disabled_and_unknown_stay_native(routed, ident, setting):
    rt, calls, _ = routed
    rt.plugin.conversation_task_provider_id = ident
    rt.plugin.task_execution_engine = setting
    e = event()
    register(rt, e)
    try:
        result = await rt.submit(e, {"goal": "执行目标", "reasoning_effort": "xhigh"})
        assert result["accepted"]
        job = rt.service.claim("native")
        assert job.task_id == result["task_id"] and job.metadata["engine"] == "native"
        assert "reasoning_effort" not in job.metadata["request"]
        assert not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_existing_native_task_never_migrates(routed):
    rt, calls, _ = routed
    rt.plugin.task_execution_engine = "native"
    e = event()
    register(rt, e)
    try:
        first = await rt.submit(e, {"goal": "原生旧目标"})
        await rt.submit(e, {"action": "pause", "task_id": first["task_id"]})
        rt.plugin.task_execution_engine = "codex"
        result = await rt.submit(e, {"action": "resume", "task_id": first["task_id"]})
        assert result["accepted"] and not calls
        assert rt.service.claim("native").task_id == first["task_id"]
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,target",
    [("pause", "stop"), ("cancel", "stop"), ("retry", "resume"), ("resume", "resume")],
)
async def test_codex_controls_dispatch_to_runtime_even_if_web_now_native(
    routed, action, target
):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    try:
        first = await rt.submit(e, {"goal": "执行目标"})
        assert first.get("thread_id")
        rt.plugin.task_execution_engine = "native"
        method = AsyncMock(
            return_value=rt.codex.read(rt.scope(e), task_id=first["task_id"])
        )
        setattr(rt.codex, target, method)
        result = await rt.submit(e, {"action": action, "task_id": first["task_id"]})
        assert result["accepted"] and result["engine"] == "codex"
        method.assert_awaited_once()
        if target == "stop":
            assert method.call_args.kwargs["cancel"] == (action == "cancel")
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_effort_is_bounded_and_per_thread_without_shared_provider_mutation(
    routed,
):
    rt, _, providers = routed
    before = copy.deepcopy(providers["catalog"].provider_config)
    e = event()
    register(rt, e)
    try:
        rejected = await rt.submit(e, {"goal": "目标", "reasoning_effort": " "})
        assert not rejected["accepted"] and not rt.service.list(rt.scope(e))
        a = await rt.submit(e, {"goal": "目标", "reasoning_effort": "xhigh"})
        b = await rt.submit(e, {"goal": "另一目标", "fragment_key": "second"})
        assert a["reasoning_effort"] == "xhigh" and b["reasoning_effort"] == "medium"
        assert providers["catalog"].provider_config == before
        assert rt.plugin.conversation_chat_provider_id == "chat"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unsupported", "exception", "empty_thread"])
async def test_create_failure_never_claims_created_or_leaves_queued_task(
    routed, failure
):
    rt, calls, providers = routed
    if failure == "unsupported":
        providers["catalog"].get_current_key = lambda: ""
    elif failure == "exception":
        rt.codex.connect_factory = AsyncMock(side_effect=OSError("真实启动失败"))
    else:
        rt.codex.create = AsyncMock(
            return_value={"status": "queued", "thread_id": None}
        )
    e = event()
    register(rt, e)
    try:
        result = await rt.submit(e, {"goal": "执行目标"})
        assert not result["accepted"] and not result["created"]
        assert result["code"] != "accepted"
        for job in rt.service.list(rt.scope(e)):
            assert job.status not in {"queued", "running", "waiting_retry"}
        again = await rt.submit(e, {"goal": "执行目标"})
        assert not again["accepted"]
        assert len(calls) <= 1
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_running_codex_revise_does_not_rewrite_goal(routed):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    try:
        first = await rt.submit(e, {"goal": "原始目标"})
        result = await rt.submit(
            e, {"action": "revise", "task_id": first["task_id"], "goal": "新目标"}
        )
        assert not result["accepted"] and result["code"] == "guide_required"
        assert rt.service.get(first["task_id"], rt.scope(e)).goal == "原始目标"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_runtime_stop_closes_codex(routed):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    await rt.submit(e, {"goal": "执行目标"})
    await rt.stop()
    assert rt.codex._closed and not rt.codex.active


@pytest.mark.asyncio
async def test_unresolved_provider_lookup_retains_native_path(routed):
    rt, calls, _ = routed

    def unavailable(_):
        raise AttributeError("Provider manager unavailable")

    rt.plugin.context.get_provider_by_id = unavailable
    e = event()
    register(rt, e)
    try:
        result = await rt.submit(e, {"goal": "目标"})
        assert result["accepted"] and result["engine"] == "native"
        assert rt.service.claim("native").task_id == result["task_id"]
        assert not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_explicit_retry_recovers_same_task_after_unbound_model_failure(routed):
    rt, calls, providers = routed
    providers["catalog"].get_current_key = lambda: ""
    e = event()
    register(rt, e)
    try:
        first = await rt.submit(e, {"goal": "目标"})
        assert first["code"] == "unsupported_model" and not first["accepted"]
        assert rt.service.get(first["task_id"], rt.scope(e)).status == "unknown_result"
        providers["catalog"].get_current_key = lambda: "fake-fixed-key"
        result = await rt.submit(
            e,
            {
                "action": "retry",
                "task_id": first["task_id"],
                "reasoning_effort": "high",
            },
        )
        assert result["accepted"] and result["task_id"] == first["task_id"]
        assert result["thread_id"] and result["reasoning_effort"] == "high"
        assert len(calls) == 1 and len(rt.service.list(rt.scope(e))) == 1
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_dialogue_resume_preserves_requested_next_turn_model_and_effort(routed):
    rt, calls, providers = routed
    providers["second-gpt"] = SimpleNamespace(
        provider_config={
            "enable": True,
            "model": "gpt-6.1-sol",
            "api_base": "https://fake.invalid/v1",
        },
        get_model=lambda: "gpt-6.1-sol",
        get_current_key=lambda: "fake-second-key",
        text_chat=AsyncMock(),
    )
    e = event()
    register(rt, e)
    try:
        tool = rt.make_codex_tool(e)
        first = json.loads(await tool.call(None, action="create", goal="目标"))
        await tool.call(None, action="stop", task_id=first["task_id"])
        await calls[0].emit(
            "turn/completed",
            {
                "threadId": first["thread_id"],
                "turn": {"id": first["turn_id"], "status": "interrupted"},
            },
        )
        assert rt.service.get(first["task_id"], rt.scope(e)).status == "paused"
        result = json.loads(
            await tool.call(
                None,
                action="resume",
                task_id=first["task_id"],
                model_id="second-gpt",
                reasoning_effort="high",
            )
        )
        assert result["ok"] and result["thread_id"] == first["thread_id"]
        assert result["model"] == "gpt-6.1-sol" and result["reasoning_effort"] == "high"
        resumed = next(p for m, p in calls[1].calls if m == "thread/resume")
        assert (
            resumed["threadId"] == first["thread_id"]
            and resumed["model"] == "gpt-6.1-sol"
        )
        turn = next(p for m, p in calls[1].calls if m == "turn/start")
        assert turn["effort"] == "high"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_dialogue_create_read_list_and_guide_same_thread(routed):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    try:
        tool = rt.make_codex_tool(e)
        assert tool.name == "pc_codex_dialogue"
        a = json.loads(
            await tool.call(None, action="create", goal="执行目标", model_id="catalog")
        )
        b = json.loads(
            await tool.call(
                None, action="create", goal="另一个目标", fragment_key="second"
            )
        )
        assert a["accepted"] and a["task_id"] and a["thread_id"]
        listed = json.loads(await tool.call(None, action="list", limit=1))
        assert listed["total"] == 2 and len(listed["items"]) == 1
        read = json.loads(
            await tool.call(
                None, action="read", thread_id=a["thread_id"], after=0, limit=1
            )
        )
        assert read["task_id"] == a["task_id"]
        guided = json.loads(
            await tool.call(
                None,
                action="guide",
                task_id=a["task_id"],
                thread_id=a["thread_id"],
                text="继续核实",
                reasoning_effort="xhigh",
            )
        )
        assert (
            guided["reasoning_effort"] == "xhigh"
            and guided["applies_to"] == "next_turn"
        )
        assert (
            rt.codex.read(rt.scope(e), task_id=b["task_id"])["reasoning_effort"]
            == "medium"
        )
        steer = next(p for m, p in calls[0].calls if m == "turn/steer")
        assert (
            steer["threadId"] == a["thread_id"]
            and steer["expectedTurnId"] == a["turn_id"]
        )
        wrong = json.loads(
            await tool.call(
                None, action="read", task_id=a["task_id"], thread_id=b["thread_id"]
            )
        )
        assert wrong["code"] == "not_found"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,setting,code",
    [
        ("gpt-in-id", "codex", "unsupported_model"),
        ("catalog", "native", "engine_disabled"),
    ],
)
async def test_explicit_dialogue_never_silently_creates_native(
    routed, model, setting, code
):
    rt, calls, _ = routed
    rt.plugin.task_execution_engine = setting
    e = event()
    register(rt, e)
    try:
        result = json.loads(
            await rt.make_codex_tool(e).call(
                None, action="create", goal="目标", model_id=model
            )
        )
        assert not result["accepted"] and result["code"] == code
        assert not rt.service.list(rt.scope(e)) and not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_dialogue_real_event_scope_and_arguments_are_host_owned(routed):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    other = event("foreign")
    register(rt, other)
    try:
        own = rt.make_codex_tool(e)
        a = json.loads(await own.call(None, action="create", goal="目标"))
        foreign = rt.make_codex_tool(other)
        assert (
            json.loads(await foreign.call(SimpleNamespace(event=e), action="list"))[
                "total"
            ]
            == 0
        )
        for action in ("read", "guide", "stop", "resume"):
            result = json.loads(
                await foreign.call(
                    SimpleNamespace(event=e), action=action, task_id=a["task_id"]
                )
            )
            assert result["code"] == "not_found"
        for args in (
            {"bot_id": "forged"},
            {"scope": {}},
            {"limit": 0},
            {"after": -1},
            {"limit": True},
            {"reasoning_effort": " "},
        ):
            assert (
                json.loads(await own.call(None, action="list", **args))["code"]
                == "invalid_arguments"
            )
        assert (
            json.loads(await foreign.call(None, action="create", goal="无权限任务"))[
                "code"
            ]
            == "permission_denied"
        )
        other.session_id = "forged"
        assert (
            json.loads(await foreign.call(None, action="list"))["code"]
            == "untrusted_event"
        )
        unregistered = rt.make_codex_tool(event("new"))
        assert (
            json.loads(await unregistered.call(None, action="list"))["code"]
            == "untrusted_event"
        )
        e.is_synthetic = True
        assert (
            json.loads(await own.call(None, action="read", task_id=a["task_id"]))[
                "code"
            ]
            == "untrusted_event"
        )
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_dialogue_catalog_is_existing_web_source_and_rejects_invented_id(routed):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    try:
        catalog = json.loads(await rt.make_model_tool(e).call(None))
        assert catalog["models"] == rt.plugin.page_api._available_provider_items()
        result = json.loads(
            await rt.make_codex_tool(e).call(
                None, action="create", goal="目标", model_id="invented"
            )
        )
        assert not result["accepted"] and result["code"] == "invalid_model_policy"
        assert not rt.service.list(rt.scope(e))
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "list", "reasoning_effort": "high"},
        {"action": "read", "task_id": "unknown", "model_id": "catalog"},
        {"action": "stop", "task_id": "unknown", "reasoning_effort": "high"},
    ],
)
async def test_dialogue_rejects_settings_not_applicable_to_action(routed, kwargs):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    try:
        result = json.loads(await rt.make_codex_tool(e).call(None, **kwargs))
        assert result["code"] == "invalid_arguments" and not result["ok"]
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_management_tool_codex_control_receipt_and_direct_stop_resume(routed):
    rt, _, _ = routed
    e = event()
    register(rt, e)
    try:
        a = await rt.submit(e, {"goal": "目标"})
        actual = rt.codex.read(rt.scope(e), task_id=a["task_id"])
        rt.codex.stop = AsyncMock(return_value=actual)
        rt.codex.resume = AsyncMock(return_value=actual)
        managed = json.loads(
            await rt.make_management_tool(e).call(
                None, action="pause", source="background", item_id=a["task_id"]
            )
        )
        assert managed["ok"] and rt.codex.stop.await_count == 1
        direct = rt.make_codex_tool(e)
        assert json.loads(
            await direct.call(None, action="stop", thread_id=a["thread_id"])
        )["ok"]
        assert json.loads(
            await direct.call(None, action="resume", task_id=a["task_id"])
        )["ok"]
        rt.codex.resume.assert_awaited_once()
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_main_mounts_dialogue_alongside_original_tools_and_keeps_persona():
    from astrbot.core.agent.tool import FunctionTool
    from astrbot_plugin_private_companion.tests.test_persistent_task_route import (
        PersistentTaskRouteTests,
    )

    harness = PersistentTaskRouteTests()
    plugin, e, req, rt = harness.setup_route("chat")
    tool = FunctionTool(
        name="pc_codex_dialogue",
        description="管理Codex对话",
        parameters={"type": "object"},
    )
    rt.make_codex_tool = lambda bound: tool
    req.system_prompt = "原始人格原文"
    await harness.invoke(plugin, e, req)
    names = [item.name for item in req.func_tool.tools]
    assert "pc_codex_dialogue" in names and "pc_submit_task_intent" in names
    assert "pc_manage_tasks" in names and "pc_read_background" in names
    assert (
        "原始人格原文" in req.system_prompt
        and "HOST TASK AUTHORIZATION" in req.system_prompt
    )
    assert "medium" in req.system_prompt and "自行压缩" in req.system_prompt
    rt.submit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,status", [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_unbound_failed_task_can_be_controlled_without_starting_or_touching_sibling(
    routed, action, status
):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    try:
        first = await rt.submit(e, {"goal": "尚未启动的目标"})
        sibling = await rt.submit(e, {"goal": "另一个目标", "fragment_key": "sibling"})
        old = rt.service.get(first["task_id"], rt.scope(e))
        other = rt.service.get(sibling["task_id"], rt.scope(e))
        assert (
            old.status == "unknown_result"
            and rt.codex.store.get(old.scope, task_id=old.task_id) is None
        )
        result = json.loads(
            await rt.make_management_tool(e).call(
                None, action=action, source="background", item_id=old.task_id
            )
        )
        assert result["ok"] and result["status"] == status
        current = rt.service.get(old.task_id, old.scope)
        assert current.status == status and current.revision == old.revision + 1
        assert current.lease_token is None and current.metadata == old.metadata
        assert rt.service.get(other.task_id, other.scope) == other
        assert not calls and not rt.codex.active
        assert rt.codex.store.get(old.scope, task_id=old.task_id) is None
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "cancel"])
@pytest.mark.parametrize("guard", ["lease", "active"])
async def test_unbound_control_rejects_live_lease_or_active_process(
    routed, action, guard
):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    try:
        first = await rt.submit(e, {"goal": "目标"})
        job = rt.service.get(first["task_id"], rt.scope(e))
        if guard == "lease":
            rt.service.claim_external(job.task_id, job.scope)
        else:
            rt.codex.active[job.task_id] = {"test_sentinel": True}
        before = rt.service.get(job.task_id, job.scope)
        result = await rt.submit(e, {"action": action, "task_id": job.task_id})
        assert not result["accepted"] and result["code"] == "codex_control_conflict"
        assert rt.service.get(job.task_id, job.scope) == before and not calls
    finally:
        if guard == "active":
            rt.codex.active.pop(first["task_id"], None)
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,status", [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_unbound_control_waits_for_creating_task_then_interrupts_real_turn(
    routed, action, status
):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    entered, release = asyncio.Event(), asyncio.Event()

    async def ensure(_):
        entered.set()
        await release.wait()

    rt.ensure_workspace = ensure
    try:
        first = await rt.submit(e, {"goal": "目标"})
        providers["catalog"].get_current_key = lambda: "fake-fixed-key"
        followup = event(mid="explicit-retry")
        register(rt, followup)
        retry = asyncio.create_task(
            rt.submit(followup, {"action": "retry", "task_id": first["task_id"]})
        )
        await asyncio.wait_for(entered.wait(), 2)
        control = asyncio.create_task(
            rt.submit(e, {"action": action, "task_id": first["task_id"]})
        )
        await asyncio.sleep(0)
        assert not control.done()
        leased = rt.service.get(first["task_id"], rt.scope(e))
        assert leased.status == "running" and leased.lease_token
        release.set()
        started, stopped = await asyncio.wait_for(asyncio.gather(retry, control), 3)
        assert started["accepted"] and stopped["accepted"]
        interrupt = next(p for m, p in calls[0].calls if m == "turn/interrupt")
        assert (
            interrupt["threadId"] == started["thread_id"]
            and interrupt["turnId"] == started["turn_id"]
        )
        assert rt.service.get(first["task_id"], rt.scope(e)).status == "running"
        await calls[0].emit(
            "turn/completed",
            {
                "threadId": started["thread_id"],
                "turn": {"id": started["turn_id"], "status": "interrupted"},
            },
        )
        assert rt.service.get(first["task_id"], rt.scope(e)).status == status
        assert len(calls) == 1 and not rt.codex.active
    finally:
        release.set()
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "cancel"])
async def test_unbound_control_cas_rejects_lease_acquired_after_snapshot(
    routed, action
):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    try:
        first = await rt.submit(e, {"goal": "目标"})
        original_get = rt.service.get
        claimed = []

        def get(task_id, scope):
            snapshot = original_get(task_id, scope)
            lock = rt.codex._locks.get(task_id)
            if task_id == first["task_id"] and lock and lock.locked() and not claimed:
                claimed.append(rt.service.claim_external(task_id, scope))
            return snapshot

        rt.service.get = get
        result = await rt.submit(e, {"action": action, "task_id": first["task_id"]})
        assert not result["accepted"] and result["code"] == "codex_control_conflict"
        assert claimed and claimed[0] is not None
        current = original_get(first["task_id"], rt.scope(e))
        assert (
            current.status == "running"
            and current.lease_token == claimed[0].lease_token
        )
        assert not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,status", [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_nil_thread_binding_failed_start_can_be_controlled(
    routed, action, status
):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    rt.codex.connect_factory = AsyncMock(side_effect=OSError("启动失败"))
    try:
        first = await rt.submit(e, {"goal": "目标"})
        binding = rt.codex.store.get(rt.scope(e), task_id=first["task_id"])
        assert binding and binding["thread_id"] is None and not rt.codex.active
        before = rt.service.get(first["task_id"], rt.scope(e))
        assert before.status == "unknown_result" and before.lease_token is None
        result = await rt.submit(e, {"action": action, "task_id": first["task_id"]})
        assert result["accepted"] and result["status"] == status
        assert rt.service.get(first["task_id"], rt.scope(e)).status == status
        assert rt.codex.store.get(rt.scope(e), task_id=first["task_id"]) == binding
        rt.codex.connect_factory.assert_awaited_once()
        assert not calls and not rt.codex.active
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "thread"])
async def test_nil_thread_control_cas_rejects_new_generation_or_thread(routed, change):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    rt.codex.connect_factory = AsyncMock(side_effect=OSError("启动失败"))
    try:
        first = await rt.submit(e, {"goal": "目标"})
        binding = rt.codex.store.get(rt.scope(e), task_id=first["task_id"])
        before = rt.service.get(first["task_id"], rt.scope(e))
        original_get = rt.service.get
        mutated = []

        def get(task_id, scope):
            snapshot = original_get(task_id, scope)
            lock = rt.codex._locks.get(task_id)
            if task_id == first["task_id"] and lock and lock.locked() and not mutated:
                mutated.append(True)
                if change == "generation":
                    rt.codex.store.next_generation(scope, task_id)
                else:
                    assert rt.codex.store.attach(
                        scope,
                        task_id,
                        thread_id="new-real-thread",
                        generation=binding["generation"],
                    )
            return snapshot

        rt.service.get = get
        result = await rt.submit(e, {"action": "cancel", "task_id": first["task_id"]})
        assert not result["accepted"] and result["code"] == "codex_control_conflict"
        assert mutated and not calls
        current = original_get(first["task_id"], rt.scope(e))
        assert current.status == before.status and current.revision == before.revision
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resume", "retry"])
async def test_cancelled_unbound_task_cannot_create_binding_or_restart(routed, action):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    try:
        first = await rt.submit(e, {"goal": "目标"})
        cancelled = await rt.submit(
            e, {"action": "cancel", "task_id": first["task_id"]}
        )
        assert cancelled["accepted"] and cancelled["status"] == "cancelled"
        providers["catalog"].get_current_key = lambda: "fake-fixed-key"
        result = await rt.submit(e, {"action": action, "task_id": first["task_id"]})
        assert not result["accepted"] and result["code"] == "invalid_transition"
        assert result["status"] == "cancelled"
        assert rt.service.get(first["task_id"], rt.scope(e)).status == "cancelled"
        assert rt.codex.store.get(rt.scope(e), task_id=first["task_id"]) is None
        assert not calls
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_resume_waiting_for_create_lock_cannot_override_external_cancellation(
    routed,
):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    providers["catalog"].get_current_key = lambda: ""
    try:
        first = await rt.submit(e, {"goal": "目标"})
        providers["catalog"].get_current_key = lambda: "fake-fixed-key"
        lock = rt.codex._locks[first["task_id"]]
        await lock.acquire()
        retry = asyncio.create_task(
            rt.submit(e, {"action": "retry", "task_id": first["task_id"]})
        )
        try:
            await asyncio.sleep(0)
            assert not retry.done()
            assert rt.service.cancel(first["task_id"], rt.scope(e))
        finally:
            lock.release()
        result = await asyncio.wait_for(retry, 2)
        assert not result["accepted"] and result["code"] == "invalid_transition"
        assert result["status"] == "cancelled"
        assert rt.service.get(first["task_id"], rt.scope(e)).status == "cancelled"
        assert not calls and not rt.codex.active
    finally:
        await rt.codex.close()
