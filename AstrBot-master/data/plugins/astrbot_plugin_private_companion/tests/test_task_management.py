"""Check Gemini's bound management tools against actual synthetic task state."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.star.context import Context
from astrbot.core.tools.cron_tools import FutureTaskTool
from astrbot_plugin_private_companion.tests import test_task_runtime as runtime_helpers
from astrbot_plugin_private_companion.tests.test_task_catalog import cron, make_task
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

runtime = runtime_helpers.runtime


@pytest.mark.asyncio
async def test_management_definitions_live_counts_and_read_has_no_new_jobs(runtime):
    e = event()
    register(runtime, e)
    assert hasattr(runtime, "make_management_tool"), "Gemini management tool is missing"
    tool = runtime.make_management_tool(e)
    runtime.cron_manager = SimpleNamespace(
        list_jobs=AsyncMock(return_value=[cron(runtime.scope(e), "daily")])
    )
    result = json.loads(await tool.call(None, action="list"))
    assert result["counts"]["long_term"] == 1
    assert result["definitions"]["background"].startswith("后台任务")
    assert "长期任务" in tool.description and "短期任务" in tool.description
    assert "创建" in tool.description and "不等于" in tool.description
    assert runtime.service.list(runtime.scope(e)) == []


@pytest.mark.asyncio
async def test_cancel_has_receipt_and_never_claims_archive(runtime):
    e = event()
    register(runtime, e)
    assert hasattr(runtime, "make_management_tool")
    task = make_task(runtime, runtime.scope(e), 1)
    result = json.loads(
        await runtime.make_management_tool(e).call(
            None, action="cancel", source="background", item_id=task.task_id
        )
    )
    assert result["ok"] and result["status"] == "cancelled"
    assert runtime.service.get(task.task_id, runtime.scope(e)).status == "cancelled"
    assert not json.loads(
        await runtime.make_management_tool(e).call(
            None, action="list", category="history"
        )
    )["items"][0]["archived"]


@pytest.mark.asyncio
async def test_archive_and_delete_return_real_results_and_cannot_delete_active(runtime):
    e = event()
    register(runtime, e)
    assert hasattr(runtime, "make_management_tool")
    task = make_task(runtime, runtime.scope(e), 1)
    tool = runtime.make_management_tool(e)
    denied = json.loads(
        await tool.call(
            None, action="delete", source="background", item_id=task.task_id
        )
    )
    assert not denied["ok"]
    runtime.service.cancel(task.task_id, runtime.scope(e))
    with runtime.service._transaction() as db:
        db.execute("UPDATE pt_outbox SET state='sent' WHERE task_id=?", (task.task_id,))
    assert json.loads(
        await tool.call(
            None, action="archive", source="background", item_id=task.task_id
        )
    )["ok"]
    assert json.loads(
        await tool.call(
            None, action="delete", source="background", item_id=task.task_id
        )
    )["ok"]
    assert (
        json.loads(
            await tool.call(
                None, action="list", category="history", include_archived=True
            )
        )["total"]
        == 0
    )


@pytest.mark.asyncio
async def test_tool_cannot_change_another_sender_or_accept_forged_context(runtime):
    e = event()
    other = event(user="other")
    register(runtime, e)
    register(runtime, other)
    assert hasattr(runtime, "make_management_tool")
    task = make_task(runtime, runtime.scope(e), 1)
    tool = runtime.make_management_tool(other)
    result = json.loads(
        await tool.call(
            SimpleNamespace(event=e),
            action="cancel",
            source="background",
            item_id=task.task_id,
        )
    )
    assert result["code"] == "not_found"
    assert runtime.service.get(task.task_id, runtime.scope(e)).status == "queued"
    other.session_id = "forged"
    assert json.loads(await tool.call(None, action="list"))["code"] == "untrusted_event"


@pytest.mark.asyncio
async def test_schedule_actions_need_real_grants_and_do_not_create_gpt_job(runtime):
    e = event(user="unknown")
    register(runtime, e)
    assert hasattr(runtime, "make_management_tool")
    result = json.loads(
        await runtime.make_management_tool(e).call(
            None,
            action="schedule",
            schedule={
                "action": "create",
                "name": "test",
                "note": "remind",
                "task_kind": "reminder",
                "run_once": True,
                "run_at": "2027-01-01T08:00:00+08:00",
            },
        )
    )
    assert not result["ok"] and result["code"] == "permission_denied"
    assert not runtime.service.list(runtime.scope(e))


@pytest.mark.asyncio
async def test_frontend_schedule_pause_resume_and_classification_do_not_require_gpt(
    runtime,
):
    e = event()
    register(runtime, e)
    assert hasattr(runtime, "make_management_tool")
    job = cron(runtime.scope(e), "daily")

    async def update(job_id, **values):
        assert job_id == job.job_id
        for key, value in values.items():
            setattr(job, key, value)
        return job

    runtime.cron_manager = SimpleNamespace(
        list_jobs=AsyncMock(return_value=[job]), update_job=update
    )
    tool = runtime.make_management_tool(e)
    assert json.loads(
        await tool.call(
            None,
            action="classify",
            source="schedule",
            item_id="daily",
            category="short_term",
        )
    )["ok"]
    assert json.loads(
        await tool.call(None, action="pause", source="schedule", item_id="daily")
    )["ok"]
    assert not job.enabled
    assert json.loads(
        await tool.call(None, action="resume", source="schedule", item_id="daily")
    )["ok"]
    assert job.enabled
    assert not runtime.service.list(runtime.scope(e))


@pytest.mark.asyncio
async def test_direct_schedule_creation_is_bound_and_deduplicated(runtime):
    e = event()
    register(runtime, e)
    actual_context = Context.__new__(Context)
    actual_context.__dict__.update(vars(runtime.plugin.context))
    runtime.plugin.context = actual_context
    future = SimpleNamespace(
        call=AsyncMock(
            return_value="Scheduled future task scheduled-1 (test) one-time at tomorrow."
        )
    )
    runtime._registry = lambda *_: {"future_task": future}
    tool = runtime.make_management_tool(e)
    args = {
        "action": "schedule",
        "operation_key": "remind",
        "schedule": {
            "action": "create",
            "name": "test",
            "note": "remind",
            "task_kind": "reminder",
            "run_once": True,
            "run_at": "2027-01-01T08:00:00+08:00",
        },
    }
    first = json.loads(
        await tool.call(SimpleNamespace(event=event(user="forged")), **args)
    )
    second = json.loads(await tool.call(None, **args))
    assert first["ok"] and second["ok"]
    assert future.call.await_count == 1
    assert future.call.call_args.args[0].context.event is e
    assert not runtime.service.list(runtime.scope(e))


@pytest.mark.asyncio
async def test_unknown_schedule_result_does_not_repeat_side_effect(runtime):
    e = event()
    register(runtime, e)
    actual_context = Context.__new__(Context)
    actual_context.__dict__.update(vars(runtime.plugin.context))
    runtime.plugin.context = actual_context
    future = SimpleNamespace(
        call=AsyncMock(side_effect=RuntimeError("network result unknown"))
    )
    runtime._registry = lambda *_: {"future_task": future}
    tool = runtime.make_management_tool(e)
    args = {
        "action": "schedule",
        "operation_key": "remind",
        "schedule": {
            "action": "create",
            "note": "remind",
            "task_kind": "reminder",
            "run_once": True,
            "run_at": "2027-01-01T08:00:00+08:00",
        },
    }
    first = json.loads(await tool.call(None, **args))
    second = json.loads(await tool.call(None, **args))
    assert first["code"] == second["code"] == "schedule_result_unknown"
    assert future.call.await_count == 1


@pytest.mark.asyncio
async def test_failed_schedule_cancel_stays_unknown_instead_of_inventing_history(
    runtime,
):
    e = event()
    register(runtime, e)
    job = cron(runtime.scope(e), "daily")
    runtime.cron_manager = SimpleNamespace(
        list_jobs=AsyncMock(return_value=[job]),
        delete_job=AsyncMock(side_effect=RuntimeError("scheduler disconnected")),
    )
    tool = runtime.make_management_tool(e)
    receipt = json.loads(
        await tool.call(None, action="cancel", source="schedule", item_id="daily")
    )
    assert not receipt["ok"]
    runtime.cron_manager.list_jobs.return_value = []
    result = json.loads(await tool.call(None, action="list"))
    assert result["active_total"] == 1
    assert result["items"][0]["status"] == "cancel_result_unknown"


@pytest.mark.asyncio
async def test_successful_schedule_cancel_can_archive_but_not_delete_live_execution(
    runtime,
):
    e = event()
    register(runtime, e)
    scope = runtime.scope(e)
    job = cron(scope, "daily")
    jobs = [job]

    async def delete(_):
        jobs.clear()

    runtime.cron_manager = SimpleNamespace(
        list_jobs=AsyncMock(side_effect=lambda: list(jobs)), delete_job=delete
    )
    child = make_task(runtime, scope, "child")
    with runtime.service._transaction() as db:
        db.execute(
            "UPDATE pt_tasks SET metadata=? WHERE task_id=?",
            (json.dumps({"schedule_rule_id": "daily"}), child.task_id),
        )
    tool = runtime.make_management_tool(e)
    assert json.loads(
        await tool.call(None, action="cancel", source="schedule", item_id="daily")
    )["ok"]
    assert runtime.service.get(child.task_id, scope).status == "queued"
    assert json.loads(
        await tool.call(None, action="archive", source="schedule", item_id="daily")
    )["ok"]
    result = json.loads(
        await tool.call(None, action="delete", source="schedule", item_id="daily")
    )
    assert result["code"] == "active_related_work"


@pytest.mark.asyncio
async def test_management_uses_real_future_tool_and_runtime_wrapper_without_background_job(
    runtime,
):
    e = event()
    register(runtime, e)
    actual = Context.__new__(Context)
    actual.__dict__.update(vars(runtime.plugin.context))
    runtime.plugin.context = actual
    future = FutureTaskTool()
    actual.get_llm_tool_manager = lambda: SimpleNamespace(
        func_list=[future], builtin_func_list={}
    )
    actual.get_config = lambda **_: {
        "timezone": "Asia/Shanghai",
        "provider_settings": {"proactive_capability": {"add_cron_tools": True}},
    }
    job = cron(runtime.scope(e), "official-once", run_once=True)
    actual.cron_manager = SimpleNamespace(
        add_active_job=AsyncMock(return_value=job),
        get_next_run_time=lambda _: None,
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=job)),
        update_job=AsyncMock(return_value=job),
    )
    runtime.cron_manager = actual.cron_manager
    result = json.loads(
        await runtime.make_management_tool(e).call(
            None,
            action="schedule",
            schedule={
                "action": "create",
                "note": "明早提醒",
                "task_kind": "reminder",
                "run_once": True,
                "run_at": "2027-01-01T08:00:00+08:00",
            },
        )
    )
    assert result["ok"] and "official-once" in result["receipt"]
    payload = actual.cron_manager.add_active_job.call_args.kwargs["payload"]
    assert (
        payload["sender_id"] == e.get_sender_id()
        and payload["session"] == e.unified_msg_origin
    )
    assert (
        actual.cron_manager.update_job.call_args.kwargs["payload"]["source"]
        == "reminder"
    )
    assert runtime.service.list(runtime.scope(e)) == []
