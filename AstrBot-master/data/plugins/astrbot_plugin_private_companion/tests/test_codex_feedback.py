"""Verify explicit Gemini recovery resumes one actual Codex thread."""

import json
from unittest.mock import AsyncMock

import pytest
from astrbot.core.provider.entities import LLMResponse

from astrbot_plugin_private_companion.task_feedback import (
    RecoverBackgroundTool,
    render_error_feedback,
)
from astrbot_plugin_private_companion.tests import (
    test_codex_dispatch as dispatch_helpers,
)
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

runtime = dispatch_helpers.runtime
routed = dispatch_helpers.routed


async def fail_turn(rt, calls, created):
    await calls[-1].emit(
        "turn/completed",
        {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "failed"},
        },
    )
    return rt.service.get(created["task_id"], rt.scope(event()))


@pytest.mark.asyncio
async def test_gemini_explicit_recovery_resumes_same_thread_with_guidance(routed):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    try:
        created = await rt.submit(e, {"goal": "fix original task"})
        failed = await fail_turn(rt, calls, created)
        assert failed.status == "failed" and len(calls) == 1
        provider = AsyncMock()
        provider.text_chat.side_effect = [
            LLMResponse(
                role="tool",
                tools_call_name=["pc_recover_background"],
                tools_call_args=[
                    {
                        "expected_revision": failed.revision,
                        "guidance": "核对旧回执后只修复缺失依赖",
                    }
                ],
                tools_call_ids=["fix"],
            ),
            LLMResponse(role="assistant", completion_text="已经接着修复。"),
        ]
        before = dict(providers["catalog"].provider_config)
        result = await render_error_feedback(rt, provider, failed, prompt="fix task")
        current = rt.service.get(failed.task_id, scope)
        assert current.status == "running", current
        assert len(calls) == 2
        resumed = calls[-1]
        assert (
            next(p["threadId"] for m, p in resumed.calls if m == "thread/resume")
            == created["thread_id"]
        )
        assert not any(m == "thread/start" for m, _ in resumed.calls)
        inputs = next(p["input"] for m, p in resumed.calls if m == "turn/start")
        assert "核对旧回执后只修复缺失依赖" in repr(inputs) and "不重复" in repr(inputs)
        assert result.recovery_key == current.metadata["feedback_recovery_key"]
        assert result.recovery_revision is not None
        assert providers["catalog"].provider_config == before
        assert rt.service.get(failed.task_id, scope).goal == failed.goal
        assert len(rt.service.list(scope)) == 1
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["revision", "cancelled", "paused", "unknown_result", "revoked"]
)
async def test_codex_feedback_unsafe_recovery_never_starts(routed, kind):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    try:
        created = await rt.submit(e, {"goal": "protected"})
        failed = await fail_turn(rt, calls, created)
        if kind in {"cancelled", "paused", "unknown_result"}:
            with rt.service._transaction() as db:
                db.execute(
                    "UPDATE pt_tasks SET status=? WHERE task_id=?",
                    (kind, failed.task_id),
                )
        elif kind == "revoked":
            rt.authorize_job = lambda _: False
        result = json.loads(
            await RecoverBackgroundTool(rt, failed).call(
                None,
                expected_revision=failed.revision - (kind == "revision"),
                guidance="specific fix",
            )
        )
        assert not result["ok"]
        assert len(calls) == 1
        assert rt.service.get(failed.task_id, failed.scope).status != "queued"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["unbound", "disconnected_model", "connect_failure", "revoked_after_commit"]
)
async def test_codex_recovery_failure_is_true_state_not_queued(routed, kind):
    rt, calls, providers = routed
    e = event()
    register(rt, e)
    try:
        created = await rt.submit(e, {"goal": "failure cleanup"})
        failed = await fail_turn(rt, calls, created)
        if kind == "unbound":
            with rt.service._transaction() as db:
                db.execute(
                    "DELETE FROM pc_codex_threads WHERE task_id=?", (failed.task_id,)
                )
        elif kind == "disconnected_model":
            providers["catalog"].provider_config.pop("api_base")
        elif kind == "connect_failure":
            rt.codex.connect_factory = AsyncMock(side_effect=ConnectionError("offline"))
        else:
            original = rt.service.recover_with_guidance

            def revoke(*args):
                value = original(*args)
                rt.authorize_job = lambda _: False
                return value

            rt.service.recover_with_guidance = revoke
        result = json.loads(
            await RecoverBackgroundTool(rt, failed).call(
                None, expected_revision=failed.revision, guidance="specific fix"
            )
        )
        assert not result["ok"], result
        current = rt.service.get(failed.task_id, failed.scope)
        assert current.status in {"failed", "unknown_result"}
        assert current.lease_token is None and failed.task_id not in rt.codex.active
        assert len(calls) == 1
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_recovery_same_failure_guidance_is_idempotent(routed):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    try:
        created = await rt.submit(e, {"goal": "idempotent"})
        failed = await fail_turn(rt, calls, created)
        assert json.loads(
            await RecoverBackgroundTool(rt, failed).call(
                None, expected_revision=failed.revision, guidance="specific fix"
            )
        )["ok"]
        resumed = rt.codex.read(failed.scope, task_id=failed.task_id)
        again = await fail_turn(rt, calls, resumed)
        result = json.loads(
            await RecoverBackgroundTool(rt, again).call(
                None, expected_revision=again.revision, guidance="specific fix"
            )
        )
        assert not result["ok"] and len(calls) == 2
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_native_recovery_remains_queue_only(runtime):
    rt = runtime
    e = event()
    register(rt, e)
    created = await rt.submit(e, {"goal": "native"})
    job = rt.service.claim("native-worker")
    rt.service.fail(job, "task_incomplete")
    failed = rt.service.get(created["task_id"], job.scope)
    result = json.loads(
        await RecoverBackgroundTool(rt, failed).call(
            None, expected_revision=failed.revision, guidance="specific fix"
        )
    )
    assert result == {"ok": True, "code": "recovery_queued"}
    assert rt.service.get(job.task_id, job.scope).status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["cancelled", "paused", "revoked"])
async def test_recovery_checks_control_after_connection_before_turn(routed, control):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    try:
        created = await rt.submit(e, {"goal": "startup control"})
        failed = await fail_turn(rt, calls, created)
        original = rt.codex.connect_factory

        async def controlled(**kwargs):
            rpc = await original(**kwargs)
            if control == "revoked":
                rt.authorize_job = lambda _: False
            else:
                getattr(rt.service, "cancel" if control == "cancelled" else "pause")(
                    failed.task_id, failed.scope
                )
            return rpc

        rt.codex.connect_factory = controlled
        result = json.loads(
            await RecoverBackgroundTool(rt, failed).call(
                None, expected_revision=failed.revision, guidance="specific fix"
            )
        )
        assert not result["ok"]
        assert not any(m == "turn/start" for m, _ in calls[-1].calls)
        assert not rt.codex.active
    finally:
        await rt.codex.close()
