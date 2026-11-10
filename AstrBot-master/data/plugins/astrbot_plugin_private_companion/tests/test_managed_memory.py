"""Gemini短记忆的持久化、隔离与工具身份边界。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.managed_memory import (
    ManagedMemoryStore,
    ManagedMemoryTool,
    memory_key,
)


def key(bot="bot", persona="小七", user="u", group="", kind="private"):
    return memory_key(bot, persona, kind, user, group)


def test_persistence_clear_and_seed_do_not_resurrect_old_memory(tmp_path):
    path = tmp_path / "notes.sqlite3"
    store = ManagedMemoryStore(path)
    assert store.seed(key(), "喜欢红茶", "旧资料")["text"] == "喜欢红茶"
    first = store.read(key())
    assert first["revision"] == 1
    assert store.write(key(), "", first["revision"])["ok"]
    restarted = ManagedMemoryStore(path)
    assert restarted.seed(key(), "喜欢红茶", "旧资料")["text"] == ""
    assert restarted.read(key())["revision"] == 2
    assert restarted.legacy(key(), 0, 100)["text"] == "旧资料"


def test_conflict_keeps_new_fact_and_returns_latest(tmp_path):
    store = ManagedMemoryStore(tmp_path / "notes.db")
    store.seed(key(), "称呼小明")
    assert store.write(key(), "称呼阿明", 1)["ok"]
    conflict = store.write(key(), "喜欢猫", 1)
    assert not conflict["ok"] and conflict["code"] == "revision_conflict"
    assert conflict["note"]["text"] == "称呼阿明"
    assert conflict["note"]["revision"] == 2


def test_scope_and_soft_length_are_preserved(tmp_path):
    store = ManagedMemoryStore(tmp_path / "notes.db")
    long_note = "用户明确要求保留的信息。" * 200
    assert store.write(key(), long_note, 0)["ok"]
    assert store.read(key())["text"] == long_note
    for foreign in (
        key(bot="other"),
        key(persona="晚柠"),
        key(user="other"),
        key(group="g", kind="group_member"),
    ):
        assert store.read(foreign)["text"] == ""
    with pytest.raises(ValueError):
        key(user="")
    with pytest.raises(ValueError):
        key(group="g")


def setup_tool(tmp_path):
    class Event:
        def get_group_id(self):
            return ""

    event = Event()
    scope = SimpleNamespace(bot_id="bot", sender_id="u", session_key="private:u")
    store = ManagedMemoryStore(tmp_path / "notes.db")
    store.seed(key(), "喜欢红茶", "旧" * 40)
    runtime = SimpleNamespace(
        _events={event: {"persona_id": "小七"}},
        scope=lambda _: scope,
        _trusted_event=lambda _: True,
        _safe_text=lambda text, limit=16000: text.replace(
            "active-secret", "[redacted]"
        )[:limit],
    )
    return (
        ManagedMemoryTool(runtime, event, store, {"self": key()}),
        runtime,
        event,
        scope,
    )


@pytest.mark.asyncio
async def test_tool_can_edit_own_memory_without_task_permission_and_preserves_business(
    tmp_path,
):
    tool, _, _, _ = setup_tool(tmp_path)
    first = json.loads(await tool.call(None, action="read"))
    assert first["ok"] and first["note"]["text"] == "喜欢红茶"
    text = "偏好代码示例 api_key=ordinary-example；active-secret"
    saved = json.loads(
        await tool.call(None, action="write", text=text, expected_revision=1)
    )
    assert saved["ok"] and "ordinary-example" in saved["note"]["text"]
    assert "active-secret" not in saved["note"]["text"]
    empty = json.loads(
        await tool.call(None, action="write", text="", expected_revision=2)
    )
    assert empty["ok"] and empty["note"]["text"] == ""


@pytest.mark.asyncio
async def test_tool_rejects_scope_injection_and_stale_identity(tmp_path):
    tool, runtime, event, scope = setup_tool(tmp_path)
    for args in (
        {"user_id": "other"},
        {"target": "group"},
        {"action": "write", "text": "new"},
        {"expected_revision": -1},
    ):
        assert not json.loads(await tool.call(None, **args))["ok"]
    scope.sender_id = "other"
    assert not json.loads(await tool.call(None, action="read"))["ok"]
    scope.sender_id = "u"
    runtime._events[event]["persona_id"] = "晚柠"
    assert not json.loads(await tool.call(None, action="read"))["ok"]


@pytest.mark.asyncio
async def test_legacy_pagination_and_concurrent_writers(tmp_path):
    tool, _, _, _ = setup_tool(tmp_path)
    page = json.loads(await tool.call(None, action="legacy", limit=10))
    assert page["text"] == "旧" * 10 and page["next_offset"] == 10
    results = await asyncio.gather(
        *(
            tool.call(None, action="write", text=f"新记忆{i}", expected_revision=1)
            for i in range(2)
        )
    )
    assert sum(json.loads(result)["ok"] for result in results) == 1
