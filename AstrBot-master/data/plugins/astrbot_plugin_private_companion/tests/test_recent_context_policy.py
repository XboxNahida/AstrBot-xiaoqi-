import copy
from types import SimpleNamespace

import pytest

from astrbot.core.agent.message import Message

try:
    from astrbot.core.agent.context import recent
except ImportError:
    recent = None


def test_recent_window_keeps_original_turns_and_excludes_old_summary():
    history = [
        {"role": "user", "content": "Our previous history conversation summary: stale"},
        {"role": "assistant", "content": "Acknowledged the summary of our previous conversation history."},
    ]
    for index in range(18):
        history.extend([{"role": "user", "content": f"question {index}"}, {"role": "assistant", "content": f"answer {index}"}])
    original = copy.deepcopy(history)
    selected = recent.select_recent(history, 12)
    assert selected[0]["content"] == "question 6"
    assert len(selected) == 24
    assert history == original
    assert "stale" not in str(selected)


def test_recent_window_preserves_complete_tool_round_and_images():
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image.png"}}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call", "content": "facts"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert recent.select_recent(history, 1) == history[2:]


def test_request_cache_namespace_is_stable_and_does_not_expose_identity():
    first = recent.cache_namespace("chat", "bot", "group-user-id", "conversation-a")
    assert first == recent.cache_namespace("chat", "bot", "group-user-id", "conversation-a")
    assert first != recent.cache_namespace("chat", "bot", "group-user-id", "conversation-b")
    assert first != recent.cache_namespace("task", "bot", "group-user-id", "conversation-a")
    assert "group-user-id" not in first


@pytest.mark.asyncio
async def test_history_pressure_replaces_old_lookup_only_and_keeps_user_material():
    compressor = recent.RecentOnlyCompressor()
    messages = [
        Message(role="system", content="persona"),
        Message(role="user", content="current material"),
        Message(role="assistant", content="", tool_calls=[{"id": "a", "type": "function", "function": {"name": "pc_read_chat_history", "arguments": '{"query":"old"}'}}]),
        Message(role="tool", tool_call_id="a", content="x" * 20000),
        Message(role="assistant", content="", tool_calls=[{"id": "b", "type": "function", "function": {"name": "pc_read_chat_history", "arguments": '{"query":"new"}'}}]),
        Message(role="tool", tool_call_id="b", content="latest relevant facts"),
    ]
    result = await compressor(messages)
    assert result[1].content == "current material"
    assert result[-1].content == "latest relevant facts"
    assert len(result[3].content) < 1000
    assert "pc_read_chat_history" in result[3].content
    assert messages[3].content == "x" * 20000


def test_archive_merges_only_current_messages_without_losing_old_history():
    archive = [{"role": "user", "content": str(i)} for i in range(60)]
    current = [{"role": "user", "content": "new question"}, {"role": "assistant", "content": "answer"}]
    request = SimpleNamespace(history_policy="recent_only", history_archive=archive)
    merged = recent.merge_archive(request, current)
    assert [item["content"] for item in merged] == [item["content"] for item in archive + current]
    assert merged[-1]["timestamp"] > 0
    assert archive[-1]["content"] == "59"
    assert len(recent.merge_archive(request, current)) == len(merged)
