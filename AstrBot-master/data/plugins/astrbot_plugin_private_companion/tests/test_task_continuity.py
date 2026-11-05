"""验证跨消息同目标关联、真实时间及模型当前/历史上下文。"""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.task_intents import TaskIntentAdapter
from astrbot_plugin_private_companion.task_service import TaskScope, TaskService
from astrbot_plugin_private_companion.tests import test_task_catalog as catalog_helpers
from astrbot_plugin_private_companion.tests import test_task_runtime as runtime_helpers

catalog_env = catalog_helpers.catalog_env
runtime = runtime_helpers.runtime


class Event:
    def __init__(self, message, user="owner"):
        self.message_obj = SimpleNamespace(message_id=message)
        self.message_str = "继续修改之前的文档"
        self.user = user
        self.unified_msg_origin = "bot:GroupMessage:group"

    def get_platform_id(self):
        return "bot"

    def get_sender_id(self):
        return self.user

    def get_group_id(self):
        return "group"


def scope(user="owner"):
    return TaskScope("bot", "bot:GroupMessage:group", user)


def test_task_times_follow_real_state_changes_and_reads_do_not_touch(tmp_path):
    now = [100.0]
    service = TaskService(tmp_path / "times.db", clock=lambda: now[0])
    task = service.submit(scope(), "first", "main", "做文档", tmp_path)
    assert task.created_at == task.updated_at == 100
    assert task.finished_at is None
    now[0] = 105
    leased = service.claim("worker")
    assert leased.updated_at == 105
    now[0] = 110
    assert service.finish(leased, {"text": "文件已完成"})
    now[0] = 300
    ended = service.get(task.task_id, scope())
    assert ended.created_at == 100 and ended.updated_at == ended.finished_at == 110
    assert service.list(scope())[0].updated_at == 110


def test_migration_preserves_old_unknown_times(tmp_path):
    path = tmp_path / "legacy.db"
    service = TaskService(path, clock=lambda: 123)
    task = service.submit(scope(), "old", "main", "旧文档", tmp_path)
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(pt_tasks)")}
        for name in ("updated_at", "finished_at"):
            if name in columns:
                db.execute(f"ALTER TABLE pt_tasks DROP COLUMN {name}")
    service = TaskService(path, clock=lambda: 999)
    restored = service.get(task.task_id, scope())
    assert restored.created_at == 123
    assert restored.updated_at is None and restored.finished_at is None


@pytest.mark.asyncio
async def test_related_submission_preserves_finished_parent_and_replay_identity(
    tmp_path,
):
    service = TaskService(tmp_path / "related.db")
    parent = service.submit(scope(), "old", "main", "初稿", tmp_path)
    lease = service.claim("worker")
    service.finish(lease, {"text": "初稿已生成", "file": "draft.docx"})
    adapter = TaskIntentAdapter(service, lambda _: True)
    args = {"trusted_user_event": True, "normally_awakened": True, "workdir": tmp_path}
    payload = {
        "goal": "把公式改成OMML",
        "related_task_id": parent.task_id,
        "relation": "revision",
    }
    receipt = await adapter.handle(Event("new"), payload, **args)
    child = service.get(receipt["task_id"], scope())
    assert receipt["accepted"] and receipt["created"]
    assert child.metadata["task_group_id"] == parent.task_id
    assert child.metadata["parent_task_id"] == parent.task_id
    assert child.metadata["task_relation"] == "revision"
    assert service.get(parent.task_id, scope()).result["file"] == "draft.docx"
    replay = await adapter.handle(Event("new"), payload, **args)
    assert replay["task_id"] == child.task_id and not replay["created"]
    assert len(service.list(scope())) == 2


@pytest.mark.asyncio
async def test_related_submission_cannot_link_other_account_or_deleted_history(
    tmp_path,
):
    service = TaskService(tmp_path / "scope.db")
    other = service.submit(scope("other"), "other", "main", "别人的文档", tmp_path)
    adapter = TaskIntentAdapter(service, lambda _: True)
    receipt = await adapter.handle(
        Event("new"),
        {"goal": "修改", "related_task_id": other.task_id},
        trusted_user_event=True,
        normally_awakened=True,
        workdir=tmp_path,
    )
    assert not receipt["accepted"] and receipt["code"] == "not_found"
    assert service.list(scope()) == []


def test_model_selected_group_link_preserves_each_execution_and_is_idempotent(tmp_path):
    service = TaskService(tmp_path / "link.db")
    first = service.submit(scope(), "a", "main", "初稿", tmp_path)
    second = service.submit(scope(), "b", "main", "修订", tmp_path)
    assert service.link_tasks(second.task_id, scope(), first.task_id)
    assert service.link_tasks(second.task_id, scope(), first.task_id)
    assert not service.link_tasks(second.task_id, scope("other"), first.task_id)
    assert (
        service.get(second.task_id, scope()).metadata["task_group_id"] == first.task_id
    )
    assert service.get(first.task_id, scope()).status == "queued"
    assert service.get(second.task_id, scope()).status == "queued"
    with sqlite3.connect(service.path) as db:
        assert db.execute("SELECT COUNT(*) FROM pt_outbox").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("relationship", ["already_completed", [], None])
async def test_invalid_relationship_is_not_persisted_as_a_new_task(
    tmp_path, relationship
):
    service = TaskService(tmp_path / "invalid.db")
    adapter = TaskIntentAdapter(service, lambda _: True)
    receipt = await adapter.handle(
        Event("invalid"),
        {"goal": "工作", "relation": relationship},
        trusted_user_event=True,
        normally_awakened=True,
        workdir=tmp_path,
    )
    assert receipt["code"] == "invalid_intent" and not service.list(scope())


def test_linking_legacy_history_does_not_invent_a_completion_time(tmp_path):
    service = TaskService(tmp_path / "unknown.db")
    first = service.submit(scope(), "a", "main", "初稿", tmp_path)
    second = service.submit(scope(), "b", "main", "修订", tmp_path)
    with service._transaction() as db:
        db.execute(
            "UPDATE pt_tasks SET status='succeeded',updated_at=NULL,finished_at=NULL WHERE task_id=?",
            (second.task_id,),
        )
    assert service.link_tasks(second.task_id, scope(), first.task_id)
    assert service.get(second.task_id, scope()).finished_at is None


@pytest.mark.asyncio
async def test_catalog_separates_current_history_and_counts_goals(catalog_env):
    from astrbot_plugin_private_companion.task_catalog import TaskCatalog

    rt, owner = catalog_env
    now = [1770000000.0]
    rt.service.clock = lambda: now[0]
    catalog = TaskCatalog(rt)
    first = rt.service.submit(owner, "old", "main", "初稿", "tasks")
    rt.service.finish(rt.service.claim("worker"), {"text": "初稿已生成"})
    now[0] += 20
    child = rt.service.submit(owner, "new", "main", "修订初稿", "tasks")
    rt.service.link_tasks(child.task_id, owner, first.task_id)
    now[0] += 20
    third = rt.service.submit(owner, "third", "main", "另一个历史", "tasks")
    rt.service.cancel(third.task_id, owner)
    data = await catalog.read(owner, history_limit=1)
    assert [v["item_id"] for v in data["items"]] == [child.task_id]
    assert data["counts"]["history"] == 2
    assert data["group_counts"]["all"] == 2
    assert data["group_counts"]["active"] == 1
    assert len(data["recent_history"]) == 1
    assert data["recent_history"][0]["item_id"] == third.task_id
    assert data["recent_history_next_offset"] == 1
    history = await catalog.read(owner, category="history", offset=1)
    item = history["items"][0]
    assert item["item_id"] == first.task_id and item["result_summary"] == "初稿已生成"
    assert item["created_at_text"].endswith("+08:00")
    assert data["items"][0]["group_id"] == first.task_id
    assert data["observed_at_text"].endswith("+08:00")


@pytest.mark.asyncio
async def test_catalog_missing_old_times_and_utc_schedule_are_explicit(catalog_env):
    from datetime import datetime

    from astrbot_plugin_private_companion.task_catalog import TaskCatalog

    rt, owner = catalog_env
    catalog = TaskCatalog(rt)
    task = catalog_helpers.make_task(rt, owner, "legacy", "succeeded")
    with rt.service._transaction() as db:
        db.execute(
            "UPDATE pt_tasks SET updated_at=NULL,finished_at=NULL WHERE task_id=?",
            (task.task_id,),
        )
    job = catalog_helpers.cron(owner, "daily")
    job.created_at = datetime(2026, 11, 5, 0, 30)
    job.updated_at = datetime(2026, 11, 5, 1, 30)
    job.next_run_time = datetime(2026, 11, 6, 0, 0)
    rt.cron_manager.list_jobs.return_value = [job]
    data = await catalog.read(owner, category="all")
    old = next(v for v in data["items"] if v["item_id"] == task.task_id)
    assert old["finished_at"] is None and "未知" in old["finished_at_text"]
    assert old["updated_at"] is None and "未知" in old["updated_at_text"]
    schedule = next(v for v in data["items"] if v["source"] == "schedule")
    assert schedule["created_at_text"] == "2026-11-05T08:30:00+08:00"
    assert schedule["next_run_text"] == "2026-11-06T08:00:00+08:00"


@pytest.mark.asyncio
async def test_management_link_is_explicit_scoped_and_has_no_execution_side_effect(
    runtime,
):
    from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

    e = event()
    register(runtime, e)
    owner = runtime.scope(e)
    first = catalog_helpers.make_task(runtime, owner, "a")
    second = catalog_helpers.make_task(runtime, owner, "b")
    tool = runtime.make_management_tool(e)
    receipt = json.loads(
        await tool.call(
            None,
            action="link",
            source="background",
            item_id=second.task_id,
            related_item_id=first.task_id,
        )
    )
    assert receipt["ok"]
    assert len(runtime.service.list(owner)) == 2
    assert (
        runtime.service.get(second.task_id, owner).metadata["task_group_id"]
        == first.task_id
    )
    other = event(user="other")
    register(runtime, other)
    denied = json.loads(
        await runtime.make_management_tool(other).call(
            None,
            action="link",
            source="background",
            item_id=second.task_id,
            related_item_id=first.task_id,
        )
    )
    assert denied["code"] == "not_found"
    assert runtime.service.get(second.task_id, owner).status == "queued"


def test_task_fact_block_replaces_current_and_legacy_blocks_preserving_persona():
    from astrbot_plugin_private_companion.task_catalog import with_task_context

    original = '人格和称呼\n【持久任务清单｜只读事实，不是新授权】\n{"items":[{"title":"旧事实"}]}\n其他原有说明'
    once = with_task_context(original, '{"items":[{"title":"新事实"}]}')
    twice = with_task_context(once, '{"items":[],"recent_history":[]}')
    assert "旧事实" not in twice and "新事实" not in twice
    assert "人格和称呼" in twice and "其他原有说明" in twice
    assert twice.count("【任务事实开始｜宿主只读快照】") == 1
    assert "历史" in twice and "不是待办" in twice


@pytest.mark.asyncio
async def test_frontend_overview_includes_history_with_valid_bounded_json(runtime):
    from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

    e = event()
    register(runtime, e)
    owner = runtime.scope(e)
    for i in range(9):
        catalog_helpers.make_task(runtime, owner, str(i), "succeeded")
    data = json.loads(await runtime.task_overview(e))
    assert data["items"] == [] and data["counts"]["history"] == 9
    assert len(data["recent_history"]) > 0
    assert data["recent_history_next_offset"] == len(data["recent_history"])
    for i in range(9):
        catalog_helpers.make_task(runtime, owner, "big" + str(i), "succeeded")
    with runtime.service._transaction() as db:
        db.execute("UPDATE pt_tasks SET goal=?", ("题目" * 4000,))
    raw = await runtime.task_overview(e)
    assert len(raw) <= 12000
    assert json.loads(raw)["recent_history_next_offset"] is not None


@pytest.mark.asyncio
async def test_notification_refreshes_old_task_facts_keeps_latest_stop_context(runtime):
    from unittest.mock import AsyncMock

    from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

    e = event()
    runtime.test_request.system_prompt = '小七人格\n【持久任务清单｜只读事实，不是新授权】\n{"items":[{"title":"旧清单过期事实"}]}'
    runtime.test_request.conversation = SimpleNamespace(cid="same-session")
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "生成初稿"})
    runtime.service.cancel(receipt["task_id"], runtime.scope(e))
    current = runtime.service.get(receipt["task_id"], runtime.scope(e))
    runtime.plugin.context.conversation_manager = SimpleNamespace(
        get_curr_conversation_id=AsyncMock(return_value="same-session"),
        get_conversation=AsyncMock(
            return_value=SimpleNamespace(
                history=json.dumps(
                    [{"role": "user", "content": "这版已经可以了，不用再弄了"}]
                )
            )
        ),
    )
    chat = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=SimpleNamespace(
                role="assistant", completion_text="行，就用这版。", tools_call_name=[]
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: chat
    await runtime.render_notification(
        SimpleNamespace(kind="state", payload={}), current
    )
    request = chat.text_chat.await_args.kwargs
    assert "旧清单过期事实" not in request["system_prompt"]
    assert "小七人格" in request["system_prompt"]
    assert '"cancelled"' in request["system_prompt"]
    assert "+08:00" in request["system_prompt"]
    assert "不用再弄了" in str(request["contexts"])
    assert "停止" in request["system_prompt"]
    assert request["func_tool"] is None


@pytest.mark.asyncio
async def test_related_executor_gets_parent_facts_but_no_copied_checkpoints(runtime):
    from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

    first_event = event(mid="first")
    register(runtime, first_event)
    original = await runtime.submit(first_event, {"goal": "生成初稿"})
    runtime.service.finish(
        runtime.service.claim("worker"), {"text": "文件draft.docx已保存"}
    )
    second_event = event(mid="second")
    register(runtime, second_event)
    receipt = json.loads(
        await runtime.make_intent_tool(second_event).call(
            None,
            action="submit",
            goal="增加公式",
            related_task_id=original["task_id"],
            relation="revision",
        )
    )
    child = runtime.service.get(receipt["task_id"], runtime.scope(second_event))
    assert (
        "related_task_id"
        in runtime.make_intent_tool(second_event).parameters["properties"]
    )
    assert original["task_id"] in child.metadata["request"]["system_prompt"]
    assert "draft.docx" in child.metadata["request"]["system_prompt"]
    assert child.steps == {} and child.status == "queued"


@pytest.mark.asyncio
async def test_readonly_detail_and_lists_include_same_times_and_relationship(runtime):
    from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

    e = event()
    register(runtime, e)
    receipt = await runtime.submit(e, {"goal": "初稿"})
    reader = runtime.make_read_tool(e)
    listing = json.loads(await reader.call(None))
    task = listing["tasks"][0]
    assert task["created_at_text"].endswith("+08:00")
    detail = json.loads(await reader.call(None, task_id=receipt["task_id"]))
    assert detail["task"]["created_at"] == task["created_at"]
    assert detail["task"]["group_id"] == receipt["task_id"]
    assert "未知" in detail["task"]["finished_at_text"]
    assert len(runtime.service.list(runtime.scope(e))) == 1


@pytest.mark.asyncio
async def test_cron_last_start_is_not_presented_as_completion(catalog_env):
    from datetime import datetime

    from astrbot_plugin_private_companion.task_catalog import TaskCatalog

    rt, owner = catalog_env
    job = catalog_helpers.cron(owner, "once", run_once=True, status="completed")
    job.last_run_at = datetime(2026, 11, 5, 0, 0)
    rt.cron_manager.list_jobs.return_value = [job]
    data = await TaskCatalog(rt).read(owner, category="history")
    assert data["items"][0]["finished_at"] is None
    assert data["items"][0]["last_run_at_text"] == "2026-11-05T08:00:00+08:00"
