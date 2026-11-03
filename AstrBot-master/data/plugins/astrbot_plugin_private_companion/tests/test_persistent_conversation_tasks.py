import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from astrbot_plugin_private_companion.conversation_task_state import (
    ConversationTaskStateStore,
)


@pytest.fixture
def db_path():
    with TemporaryDirectory() as directory:
        yield Path(directory) / "nested" / "tasks.sqlite3"


def start(store, **kwargs):
    return store.start_task(
        session_key=kwargs.pop("session_key", "bot-a:group-a"),
        task_text="检查工程并保留原始文件",
        provider_id="gpt-main",
        fallback_provider_id="gpt-backup",
        sender_id=kwargs.pop("sender_id", "user-a"),
        conversation_id=kwargs.pop("conversation_id", "branch-a"),
        **kwargs,
    )


def test_reopen_restores_task_fields_context_and_tool_evidence(db_path):
    store = ConversationTaskStateStore(db_path=db_path, clock=lambda: 42.5)
    state = start(store)
    store.record_tool_start(state.task_id, "read_file")
    store.record_tool_result(state.task_id, "read_file", "文件读取失败", failed=True)
    state.reply_context = {"contexts": [{"role": "user", "content": "原始请求"}]}
    state.notices.append("已确认请求")
    assert store.save_task(state) is state
    store.fail_task(state.task_id, "工具协议失败", unfinished_task="仍需核查编译")

    restored = ConversationTaskStateStore(db_path=db_path).get(state.task_id)
    assert restored is not state
    assert asdict(restored) == asdict(state)
    assert restored.status == "failed"
    assert restored.reply_context["contexts"][0]["content"] == "原始请求"


@pytest.mark.parametrize("limit", [1, 32, 64])
def test_cache_limit_does_not_delete_active_or_historical_records(db_path, limit):
    store = ConversationTaskStateStore(db_path=db_path, max_tasks=limit)
    active = start(store)
    history = start(store)
    store.complete_task(history.task_id, "结果已生成")
    for _ in range(limit + 3):
        start(store, session_key="other-bot:other-group")
    assert len(store._tasks) <= limit
    assert store.get(active.task_id).status == "running"
    assert store.get(history.task_id).result_preview == "结果已生成"
    assert (
        store.get_active_task("bot-a:group-a", sender_id="user-a").task_id
        == active.task_id
    )
    reopened = ConversationTaskStateStore(db_path=db_path, max_tasks=limit)
    assert (
        len(reopened.list_for_session("other-bot:other-group", limit=100)) == limit + 3
    )


def test_list_filters_bot_session_sender_and_conversation(db_path):
    store = ConversationTaskStateStore(db_path=db_path, clock=lambda: 5)
    first = start(store)
    second = start(store)
    start(store, sender_id="user-b")
    start(store, conversation_id="branch-b")
    start(store, session_key="bot-b:group-a")
    summaries = store.list_for_session(
        "bot-a:group-a", sender_id="user-a", conversation_id="branch-a"
    )
    assert [item["task_id"] for item in summaries] == [second.task_id, first.task_id]
    assert all("reply_context" not in item for item in summaries)
    assert (
        store.summary_for_session(
            "bot-a:group-a", sender_id="user-a", conversation_id="branch-a"
        )
        == summaries[0]
    )
    assert len(store.list_for_session("bot-a:group-a", limit=1)) == 1
    assert store.list_for_session("missing") == []
    assert store.list_for_session("bot-a:group-a", limit=0) == []


@pytest.mark.parametrize("terminal", ["complete", "failed", "timeout", "cancelled"])
def test_late_events_cannot_revive_terminal_tasks(db_path, terminal):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)
    state.status = terminal
    state.phase = terminal
    store.save_task(state)
    before = asdict(state)
    store.mark_progress(state.task_id, "迟到进度")
    store.record_tool_start(state.task_id, "late_tool")
    store.record_tool_result(state.task_id, "late_tool", "迟到结果")
    if terminal != "complete":
        store.complete_task(state.task_id, "迟到完成")
    else:
        store.fail_task(state.task_id, "迟到错误")
    assert (
        asdict(ConversationTaskStateStore(db_path=db_path).get(state.task_id)) == before
    )


def test_recovery_marks_interrupted_tasks_unknown_without_reexecuting(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    interrupted = []
    for status in ("queued", "running", "progress"):
        state = start(store)
        state.status = status
        store.save_task(state)
        interrupted.append(state.task_id)
    complete = start(store)
    store.complete_task(complete.task_id, "保留结果")
    failed = start(store)
    store.fail_task(failed.task_id, "保留错误")
    recovered_store = ConversationTaskStateStore(db_path=db_path)
    recovered = recovered_store.recover_interrupted()
    assert {state.task_id for state in recovered} == set(interrupted)
    assert all(state.status == "unknown_result" for state in recovered)
    assert recovered_store.get_active_task("bot-a:group-a") is None
    assert recovered_store.recover_interrupted() == []
    for task_id in interrupted:
        recovered_store.mark_progress(task_id, "旧执行器迟到进度")
        assert recovered_store.get(task_id).status == "unknown_result"
    reopened = ConversationTaskStateStore(db_path=db_path)
    assert reopened.get(complete.task_id).result_preview == "保留结果"
    assert reopened.get(failed.task_id).error == "保留错误"


def test_other_store_terminal_state_wins_and_cache_keeps_identity(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)
    other = ConversationTaskStateStore(db_path=db_path)
    other.fail_task(state.task_id, "实际错误")
    result = store.mark_progress(state.task_id, "过时进度")
    assert result is state
    assert state.status == "failed"
    assert state.error == "实际错误"


def test_stale_explicit_save_cannot_overwrite_terminal_state(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    stale = start(store)
    other = ConversationTaskStateStore(db_path=db_path)
    other.fail_task(stale.task_id, "实际错误")
    stale.reply_context["visible_delivery"] = True
    with pytest.raises(ValueError, match="stale"):
        store.save_task(stale)
    assert other.get(stale.task_id).status == "failed"


def test_explicit_save_cannot_reactivate_a_failed_task(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)
    store.fail_task(state.task_id, "失败原因")
    state.status = "running"
    with pytest.raises(ValueError, match="terminal"):
        store.save_task(state)
    assert store.get(state.task_id).status == "failed"


def test_database_write_failure_rolls_back_lifecycle_and_cached_identity(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)
    before = asdict(state)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON conversation_tasks "
            "BEGIN SELECT RAISE(ABORT, 'write rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="write rejected"):
        store.fail_task(state.task_id, "不能落盘的失败")
    assert store.get(state.task_id) is state
    assert asdict(state) == before


def test_serialization_failure_rolls_back_database_and_cache(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)
    state.reply_context["invalid"] = object()
    with pytest.raises(TypeError):
        store.save_task(state)
    restored = store.get(state.task_id)
    assert restored is state
    assert "invalid" not in restored.reply_context
    assert restored.status == "running"


def test_concurrent_stores_do_not_lose_tool_updates(db_path):
    store = ConversationTaskStateStore(db_path=db_path)
    state = start(store)

    def record(number):
        other = ConversationTaskStateStore(db_path=db_path)
        other.record_tool_result(state.task_id, f"tool-{number}", "完成")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(4)))
    restored = store.get(state.task_id)
    assert len(restored.tool_events) == 4
    assert {item["tool"] for item in restored.tool_events} == {
        f"tool-{number}" for number in range(4)
    }


def test_database_initialization_failure_is_explicit(db_path):
    db_path.parent.mkdir(parents=True)
    db_path.mkdir()
    with pytest.raises(sqlite3.OperationalError):
        ConversationTaskStateStore(db_path=db_path)


def test_no_database_mode_keeps_bounded_memory_contract():
    store = ConversationTaskStateStore(max_tasks=1)
    state = start(store)
    start(store)
    assert store.get(state.task_id) is None
