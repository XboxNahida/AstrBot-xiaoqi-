"""Exercise classified live tasks without touching real schedules or accounts."""

import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.interaction_channels import InteractionChannels
from astrbot_plugin_private_companion.interaction_scripts import InteractionScripts
from astrbot_plugin_private_companion.task_service import TaskScope, TaskService

try:
    from astrbot_plugin_private_companion.task_catalog import TaskCatalog
except ImportError:
    TaskCatalog = None


@pytest.fixture
def catalog_env(tmp_path):
    service = TaskService(tmp_path / "tasks.sqlite3")
    channels = InteractionChannels(service.path)
    api = SimpleNamespace(channels=channels)
    api.script_runner = InteractionScripts(api)
    rt = SimpleNamespace(
        service=service,
        interaction_api=api,
        cron_manager=SimpleNamespace(list_jobs=AsyncMock(return_value=[])),
        _safe_text=lambda value, limit=16000: str(value or "")[:limit],
    )
    scope = TaskScope("bot", "bot:GroupMessage:group", "owner")
    return rt, scope


def make_task(rt, scope, index, status="queued"):
    task = rt.service.submit(
        scope, str(index), "main", f"work {index}", workdir="tasks"
    )
    with rt.service._transaction() as db:
        db.execute(
            "UPDATE pt_tasks SET status=? WHERE task_id=?", (status, task.task_id)
        )
        db.execute("UPDATE pt_outbox SET state='sent' WHERE task_id=?", (task.task_id,))
    return task


@pytest.mark.asyncio
async def test_catalog_exposes_actual_tool_wait_and_issue_without_claiming_progress(catalog_env):
    rt, scope = catalog_env
    task = make_task(rt, scope, "activity")
    job = rt.service.claim("worker")
    assert rt.service.record_activity(job, "tool_result", "install", outcome={"status": "unknown", "content": "verify installation"})
    assert rt.service.record_activity(job, "tool_running", "check_files")
    data = await TaskCatalog(rt).read(scope, category="background")
    item = next(item for item in data["items"] if item["item_id"] == task.task_id)
    assert item["execution"]["phase"] == "tool_running"
    assert item["execution"]["name"] == "check_files"
    assert item["execution"]["last_tool_issue"]["name"] == "install"
    assert item["execution"]["worker_lease_valid"]


def cron(scope, key, **kwargs):
    return SimpleNamespace(
        job_id=key,
        name=key,
        payload={
            "session": scope.session_key,
            "sender_id": scope.sender_id,
            "note": "query news",
        },
        run_once=kwargs.get("run_once", False),
        enabled=kwargs.get("enabled", True),
        status=kwargs.get("status", "scheduled"),
        cron_expression="0 8 * * *",
        next_run_time=kwargs.get("next_run_time"),
        last_run_at=None,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_four_definitions_and_old_active_task_are_not_hidden_by_history(
    catalog_env,
):
    rt, scope = catalog_env
    assert TaskCatalog is not None, "classified catalog is not implemented"
    catalog = TaskCatalog(rt)
    active = make_task(rt, scope, 0, "waiting_user")
    for index in range(1, 18):
        make_task(rt, scope, index, "succeeded")
    rt.cron_manager.list_jobs.return_value = [
        cron(scope, "daily", status="completed"),
        cron(scope, "tomorrow", run_once=True),
    ]
    before = list(closing_dump(rt.service.path))
    data = await catalog.read(scope, limit=1)
    assert data["counts"] == {
        "long_term": 1,
        "short_term": 1,
        "background": 1,
        "history": 17,
    }
    assert data["active_total"] == 3 and data["total"] == 3
    assert data["next_offset"] == 1 and len(data["items"]) == 1
    background = await catalog.read(scope, category="background")
    assert background["items"][0]["item_id"] == active.task_id
    assert background["items"][0]["needs_attention"]
    assert background["items"][0]["status"] == "waiting_user"
    assert "长期" in data["definitions"]["long_term"]
    assert "定时" in data["definitions"]["short_term"]
    assert "用户" in data["definitions"]["background"]
    assert list(closing_dump(rt.service.path)) == before


def closing_dump(path):
    import sqlite3

    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


@pytest.mark.asyncio
async def test_classify_schedule_never_changes_real_execution_status(catalog_env):
    rt, scope = catalog_env
    assert TaskCatalog is not None
    catalog = TaskCatalog(rt)
    rt.cron_manager.list_jobs.return_value = [cron(scope, "one", run_once=True)]
    changed = await catalog.change(
        scope, "schedule", "one", "classify", category="long_term"
    )
    assert changed["ok"] and changed["changed"]
    assert (await catalog.read(scope, category="long_term"))["total"] == 1
    rt.cron_manager.list_jobs.return_value[0].status = "running"
    assert (await catalog.read(scope, category="long_term"))["items"][0][
        "status"
    ] == "running"
    assert not (await catalog.change(scope, "schedule", "one", "archive"))["ok"]


@pytest.mark.asyncio
async def test_history_archive_restore_and_identity_isolation(catalog_env):
    rt, scope = catalog_env
    assert TaskCatalog is not None
    catalog = TaskCatalog(rt)
    task = make_task(rt, scope, 1, "succeeded")
    assert (await catalog.change(scope, "background", task.task_id, "archive"))["ok"]
    assert (await catalog.read(scope, category="history"))["total"] == 0
    archived = await catalog.read(scope, category="history", include_archived=True)
    assert archived["total"] == 1 and archived["archived_count"] == 1
    assert archived["items"][0]["archived"]
    other = TaskScope(scope.bot_id, scope.session_key, "other")
    assert (await catalog.read(other))["active_total"] == 0
    assert (await catalog.change(other, "background", task.task_id, "unarchive"))[
        "code"
    ] == "not_found"
    assert (await catalog.change(scope, "background", task.task_id, "unarchive"))["ok"]
    assert (await catalog.read(scope, category="history"))["total"] == 1


@pytest.mark.asyncio
async def test_scripts_are_future_work_and_credentials_never_enter_catalog(catalog_env):
    rt, scope = catalog_env
    assert TaskCatalog is not None
    catalog = TaskCatalog(rt)
    channel = rt.interaction_api.channels.open(
        scope, "source", {"source_task_id": "parent"}
    )
    with rt.interaction_api.channels.transaction() as db:
        db.execute(
            "INSERT INTO ic_scripts(id,channel_id,request_key,digest,path,arguments,run_at,timeout,status) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "script",
                channel["id"],
                "key",
                "hash",
                "news.py",
                "[]",
                123456,
                60,
                "pending",
            ),
        )
    data = await catalog.read(scope)
    item = data["items"][0]
    assert item["source"] == "script" and item["category"] == "short_term"
    assert item["source_task_id"] == "parent"
    assert channel["token"] not in json.dumps(data)
    assert not (await catalog.change(scope, "script", "script", "archive"))["ok"]


@pytest.mark.asyncio
async def test_missing_schedule_source_is_reported_instead_of_complete_empty_list(
    catalog_env,
):
    rt, scope = catalog_env
    assert TaskCatalog is not None
    rt.cron_manager = None
    data = await TaskCatalog(rt).read(scope)
    assert not data["complete"] and "schedule" in data["unavailable_sources"]


@pytest.mark.asyncio
async def test_delete_requires_archive_preserves_replay_guard_and_erases_details(
    catalog_env,
):
    rt, scope = catalog_env
    catalog = TaskCatalog(rt)
    task = make_task(rt, scope, "done", "failed")
    assert hasattr(catalog, "delete"), "history deletion is not implemented"
    assert (await catalog.delete(scope, "background", task.task_id))[
        "code"
    ] == "archive_required"
    assert (await catalog.change(scope, "background", task.task_id, "archive"))["ok"]
    assert (await catalog.delete(scope, "background", task.task_id))["ok"]
    assert (await catalog.read(scope, category="all", include_archived=True))[
        "total"
    ] == 0
    tombstone = rt.service.get(task.task_id, scope)
    assert tombstone.metadata == {"history_deleted": True}
    assert (
        tombstone.goal != task.goal and not tombstone.steps and tombstone.result is None
    )
    assert rt.service.retry(task.task_id, scope) is False
    same = rt.service.submit(scope, "done", "main", "must not replay", workdir="tasks")
    assert same.task_id == task.task_id and same.status == "failed"
    assert (await catalog.delete(scope, "background", task.task_id))[
        "code"
    ] == "already_deleted"


@pytest.mark.asyncio
async def test_history_deletion_refuses_pending_delivery_or_live_child_script(
    catalog_env,
):
    rt, scope = catalog_env
    catalog = TaskCatalog(rt)
    task = make_task(rt, scope, "parent", "succeeded")
    assert hasattr(catalog, "delete")
    await catalog.change(scope, "background", task.task_id, "archive")
    with rt.service._transaction() as db:
        db.execute(
            "UPDATE pt_outbox SET state='pending' WHERE task_id=?", (task.task_id,)
        )
    assert (await catalog.delete(scope, "background", task.task_id))[
        "code"
    ] == "pending_delivery"
    with rt.service._transaction() as db:
        db.execute("UPDATE pt_outbox SET state='sent' WHERE task_id=?", (task.task_id,))
    channel = rt.interaction_api.channels.open(
        scope, "live", {"source_task_id": task.task_id}
    )
    assert (await catalog.delete(scope, "background", task.task_id))[
        "code"
    ] == "active_related_work"
    rt.interaction_api.channels.control(channel["id"], "close")
    assert (await catalog.delete(scope, "background", task.task_id))["ok"]
