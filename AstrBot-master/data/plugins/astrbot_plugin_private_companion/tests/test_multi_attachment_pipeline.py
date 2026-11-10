"""回归真实私聊入口的多附件接管问题。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from astrbot.core.message.components import File, Image, Plain
from astrbot_plugin_private_companion.message_pipeline import handle_private_message


class ReachedMainChain(Exception):
    pass


class PipelineHost(SimpleNamespace):
    def __init__(self):
        super().__init__()
        self._data_lock = asyncio.Lock()
        self._event_is_inbound_chat_message = lambda _: True
        self.user = {"user_id": "u", "enabled": True}
        self.data = {"users": {"u": self.user}}
        self._semantic_message_buffers = {}
        self._ensure_auto_private_user_profile = lambda *a, **k: (self.user, False)
        self._sender_display_name = lambda _: "测试用户"
        self._event_self_id = lambda _: "bot"
        self._event_message_id = lambda e: e.message_obj.message_id
        self._canonical_private_user_id = lambda x: x
        self._private_passive_profile_available = lambda *a: True
        self._get_user = lambda _: self.user
        self._event_existing_reply_result_preview = lambda _: ""
        self._feature_enabled_or_temp_unlocked = lambda _: True
        self._message_debounce_seconds = lambda _: 1
        self._private_event_has_image_safe = lambda e, **k: any(isinstance(p, Image) for p in e.message_obj.message)
        self._private_event_has_nontext_content = lambda e: any(isinstance(p, (Image, File)) for p in e.message_obj.message)
        self._event_has_media_component = self._private_event_has_nontext_content
        self._is_private_image_only_message = lambda e, t: not t and self._private_event_has_image_safe(e)
        self._semantic_buffer_key = lambda *a: ":".join(a)
        self._persist_private_inbound_images = AsyncMock(return_value=["one.png"])
        self._private_image_source_to_model_url = lambda p: p
        self._has_private_image_visual_provider = lambda _: False
        self._private_image_delivery_mode = lambda **k: "direct"
        self._note_semantic_message_buffer = Mock(return_value=True)
        self._note_private_display_name_observation = Mock(side_effect=ReachedMainChain)
        for name in (
            "_cancel_activity_followup_on_user_return", "_maybe_answer_companion_manual_natural_question",
            "_maybe_handle_natural_language_photo_request", "_maybe_handle_atrelay_private_receipt_reply",
            "_find_forward_descriptor_for_event", "_private_reply_only_relation_lookup_text",
            "_event_references_media_or_forward_with_text", "_maybe_resume_pending_atrelay_request",
            "_maybe_handle_direct_atrelay_request", "_smart_message_debounce_wait_seconds_for_event",
        ):
            setattr(self, name, AsyncMock(return_value=False))
        self._find_forward_descriptor_for_event = AsyncMock(return_value=("", {}))
        self._private_reply_only_relation_lookup_text = AsyncMock(return_value="")
        self._create_lifecycle_background_task = lambda coro, **k: coro.close()

    def __getattr__(self, name):
        return Mock(return_value=False)

    @asynccontextmanager
    async def _temporarily_release_data_lock(self):
        yield


def event(*parts, text="", message_id="m1", native=True):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id=message_id, message=list(parts)),
        message_str=text, unified_msg_origin="bot:FriendMessage:u",
        get_sender_id=lambda: "u", stop_event=Mock(),
        _private_companion_native_multimodal=native,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parts,text", [
    ([Image(file="one.png"), Image(file="two.png")], ""),
    ([Plain("比较两张图"), Image(file="one.png"), Image(file="two.png")], "比较两张图"),
    ([Plain("两份资料"), File(name="资料.zip", file="a.zip"), File(name="资料.zip", file="b.zip")], "两份资料"),
])
async def test_native_attachments_reach_main_chain_without_text_or_single_image_buffer(parts, text):
    host = PipelineHost()
    incoming = event(*parts, text=text)
    original = list(incoming.message_obj.message)
    with pytest.raises(ReachedMainChain):
        await handle_private_message(host, incoming)
    incoming.stop_event.assert_not_called()
    host._persist_private_inbound_images.assert_not_awaited()
    host._note_semantic_message_buffer.assert_not_called()
    assert incoming.message_obj.message == original


@pytest.mark.asyncio
async def test_consecutive_native_images_do_not_replace_each_other():
    host = PipelineHost()
    first = event(Image(file="one.png"), message_id="m1")
    second = event(Image(file="two.png"), message_id="m2")
    for incoming in (first, second):
        with pytest.raises(ReachedMainChain):
            await handle_private_message(host, incoming)
        incoming.stop_event.assert_not_called()
    assert first.message_obj.message[0].file == "one.png"
    assert second.message_obj.message[0].file == "two.png"
    assert not host._semantic_message_buffers


@pytest.mark.asyncio
async def test_multiple_files_with_same_name_keep_distinct_bytes_and_order(tmp_path):
    from pathlib import Path

    from astrbot_plugin_private_companion.task_attachments import stage_event_files

    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    first.write_bytes(b"first archive")
    second.write_bytes(b"second archive")
    incoming = event(File(name="资料.zip", file=str(first)), Image(file="photo.png"), File(name="资料.zip", file=str(second)))
    rows = await stage_event_files(incoming, tmp_path / "work", (tmp_path,), lambda: True)
    assert [Path(row["path"]).read_bytes() for row in rows] == [b"first archive", b"second archive"]
    assert [row["position"] for row in rows] == ["1", "3"]
    assert rows[0]["path"] != rows[1]["path"]
