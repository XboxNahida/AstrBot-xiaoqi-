import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.chat_history_reader import ChatHistoryReadTool
from astrbot_plugin_private_companion.image_inspection import ImageInspectionTool


def setup_reader():
    scope = SimpleNamespace(bot_id="bot-a", session_key="bot-a:FriendMessage:u", sender_id="u")
    event = object()
    history = [
        {"role": "system", "content": "private system"},
        {"role": "user", "content": [{"type": "text", "text": "昨天的题图"}, {"type": "image_url", "image_url": {"url": "https://example.test/problem.png"}}]},
        {"role": "assistant", "content": "答案是42", "reasoning_content": "private reasoning"},
        {"role": "tool", "content": "private tool receipt"},
    ]
    conversation = SimpleNamespace(cid="c1", user_id=scope.session_key, platform_id=scope.bot_id, title="数学", updated_at=123, history=json.dumps(history))
    other = SimpleNamespace(cid="foreign", user_id="bot-b:FriendMessage:u", platform_id="bot-b", history=json.dumps([{"role": "user", "content": "other bot private"}]))
    manager = SimpleNamespace(
        get_conversations=AsyncMock(return_value=[conversation, other]),
        get_curr_conversation_id=AsyncMock(return_value="c1"),
        get_conversation=AsyncMock(side_effect=lambda _, cid: {"c1": conversation, "foreign": other}.get(cid)),
    )
    runtime = SimpleNamespace(
        _events={event: {}}, scope=lambda _: scope, _trusted_event=lambda _: True,
        _safe_text=lambda text, limit=16000: str(text).replace("secret-credential", "[redacted]")[:limit],
        plugin=SimpleNamespace(context=SimpleNamespace(conversation_manager=manager)),
        service=SimpleNamespace(clock=lambda: 1234),
    )
    return ChatHistoryReadTool(runtime, event), manager, runtime, event


@pytest.mark.asyncio
async def test_history_pagination_search_and_image_reference_without_hidden_content():
    tool, manager, runtime, event = setup_reader()
    data = json.loads(await tool.call(None, query="题图", limit=1))
    assert data["ok"] and data["messages"][0]["content"] == "昨天的题图"
    ref = data["messages"][0]["images"][0]["image_ref"]
    assert runtime._events[event]["history_images"][ref] == "https://example.test/problem.png"
    assert "https://" not in json.dumps(data)
    all_data = await tool.call(None)
    assert "private system" not in all_data and "private reasoning" not in all_data
    assert "private tool receipt" not in all_data
    page = json.loads(await tool.call(None, limit=1))
    assert page["messages"][0]["role"] == "assistant"
    assert page["next_offset"] == 1
    manager.get_conversations.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_rejects_cross_identity_ids_and_rechecks_event():
    tool, manager, runtime, event = setup_reader()
    assert not json.loads(await tool.call(None, conversation_id="foreign"))["ok"]
    assert not json.loads(await tool.call(None, session_key="bot-b:FriendMessage:u"))["ok"]
    assert not json.loads(await tool.call(None, limit=100000))["ok"]
    del runtime._events[event]
    assert not json.loads(await tool.call(None))["ok"]


@pytest.mark.asyncio
async def test_history_lists_only_owned_conversations_and_marks_missing_timestamps():
    tool, _, _, _ = setup_reader()
    data = json.loads(await tool.call(None, resource="conversations"))
    assert [row["conversation_id"] for row in data["conversations"]] == ["c1"]
    messages = json.loads(await tool.call(None))
    assert all(row["timestamp"] is None for row in messages["messages"])


@pytest.mark.asyncio
async def test_history_image_reference_is_resolved_only_in_bound_chat():
    tool, _, runtime, event = setup_reader()
    # 模拟真实事件的会话字段，但不允许工具参数覆盖身份。
    bound_event = SimpleNamespace(unified_msg_origin=runtime.scope(event).session_key)
    class Event:
        unified_msg_origin = bound_event.unified_msg_origin
    replacement = Event()
    runtime._events = {replacement: {"request": {}}}
    tool = ChatHistoryReadTool(runtime, replacement)
    vision = ImageInspectionTool(runtime, replacement)
    runtime.plugin._transcribe_private_inbound_images = AsyncMock(return_value="图中是昨天的数学题")
    data = json.loads(await tool.call(None, images_only=True))
    ref = data["messages"][0]["images"][0]["image_ref"]
    receipt = json.loads(await vision.call(None, source="history", image_refs=[ref], question="核对题号"))
    assert receipt["success"] and receipt["text"] == "图中是昨天的数学题"
    runtime.plugin._transcribe_private_inbound_images.assert_awaited_once_with(
        ["https://example.test/problem.png"], umo=replacement.unified_msg_origin,
        user_text="核对题号", force_contextual=True,
    )
    rejected = json.loads(await vision.call(None, source="history", image_refs=["other-session-ref"], question="读图"))
    assert rejected["code"] == "history_image_not_in_scope"
    assert runtime.plugin._transcribe_private_inbound_images.await_count == 1
