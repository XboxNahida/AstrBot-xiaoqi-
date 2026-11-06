import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import botpy.message
import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Record
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform import Platform
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter import WeixinOCAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response, accepted",
    [
        ({}, False),
        ({"ret": 0}, True),
        ({"ret": 1}, False),
        ({"message_id": "synthetic-receipt"}, True),
        ({"message_id": "synthetic-receipt", "ret": 1}, False),
    ],
)
async def test_weixin_voice_ack_requires_explicit_status_and_logs_no_secrets(
    response, accepted, monkeypatch
):
    adapter = object.__new__(WeixinOCAdapter)
    adapter.token = "PRIVATE_LOGIN_TOKEN"
    adapter.metadata = SimpleNamespace(id="wx")
    adapter.account_id = "bot"
    adapter._context_tokens = {"user": "PRIVATE_CONTEXT_TOKEN"}
    adapter.client = SimpleNamespace(request_json=AsyncMock(return_value=response))
    adapter._cache_recent_message = Mock()
    logs = Mock()
    monkeypatch.setattr(
        "astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter.logger", logs
    )
    item = {
        "type": 3,
        "voice_item": {
            "encode_type": 6,
            "sample_rate": 24000,
            "playtime": 1000,
            "media": {
                "aes_key": "PRIVATE_AES_KEY",
                "encrypt_query_param": "PRIVATE_CDN_QUERY",
            },
        },
    }
    assert await adapter._send_items_to_session("user", [item]) is accepted
    assert adapter._cache_recent_message.call_count == int(accepted)
    output = repr(logs.info.call_args_list)
    assert "media send API acknowledgement" in output
    assert all(
        secret not in output
        for secret in (
            adapter.token,
            "PRIVATE_CONTEXT_TOKEN",
            "PRIVATE_AES_KEY",
            "PRIVATE_CDN_QUERY",
        )
    )


@pytest.mark.asyncio
async def test_weixin_record_reaches_media_sender_and_failure_is_not_success(
    monkeypatch,
):
    adapter = object.__new__(WeixinOCAdapter)
    adapter.metadata = SimpleNamespace(id="wx")
    adapter._send_media_segment = AsyncMock(return_value=False)
    monkeypatch.setattr(Platform, "send_by_session", AsyncMock())
    with pytest.raises(RuntimeError, match="failed to send"):
        await adapter.send_by_session(
            SimpleNamespace(session_id="user"), MessageChain([Record(file="voice.wav")])
        )
    assert adapter._send_media_segment.await_count == 1
    Platform.send_by_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_weixin_voice_uses_playable_mp3_attachment_without_transcript(
    tmp_path, monkeypatch
):
    adapter = object.__new__(WeixinOCAdapter)
    adapter.token = "synthetic"
    adapter.metadata = SimpleNamespace(id="wx")
    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"synthetic mp3")
    adapter._resolve_media_file_path = AsyncMock(return_value=mp3)
    adapter._prepare_media_item = AsyncMock(return_value={"type": 4, "file_item": {}})
    adapter._send_items_to_session = AsyncMock(return_value=True)
    assert await adapter._send_media_segment(
        "user", Record(file=str(mp3), text="private transcript")
    )
    args = adapter._prepare_media_item.call_args.args
    assert args[2:4] == (3, 4)
    assert args[4].endswith(".mp3")
    item = adapter._send_items_to_session.call_args.args[1][0]
    assert item["type"] == 4 and "voice_item" not in item
    assert "private transcript" not in repr(
        adapter._send_items_to_session.call_args.args
    )
    assert adapter._send_items_to_session.await_count == 1


@pytest.mark.asyncio
async def test_weixin_record_is_resolved_to_mp3(tmp_path, monkeypatch):
    adapter = object.__new__(WeixinOCAdapter)
    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"synthetic mp3")
    resolver = SimpleNamespace(to_path=AsyncMock(return_value=str(mp3)))
    monkeypatch.setattr(
        "astrbot.core.platform.sources.weixin_oc.weixin_oc_adapter.MediaResolver",
        lambda *args, **kwargs: resolver,
    )
    assert await adapter._resolve_media_file_path(Record(file="voice.wav")) == mp3
    resolver.to_path.assert_awaited_once_with(target_format="mp3")


@pytest.mark.asyncio
async def test_qq_same_event_concurrent_voice_does_not_overwrite_buffer():
    event = object.__new__(QQOfficialMessageEvent)
    event.bot = SimpleNamespace()
    event.send_buffer = None
    event._send_lock = asyncio.Lock()
    seen = []

    async def deliver():
        await asyncio.sleep(0)
        seen.append(event.send_buffer.chain[0].file)

    event._post_send = deliver
    await asyncio.gather(
        event.send(MessageChain([Record(file="one.wav")])),
        event.send(MessageChain([Record(file="two.wav")])),
    )
    assert seen == ["one.wav", "two.wav"]


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [False, True])
async def test_qq_failed_voice_upload_does_not_send_empty_or_text_only_success(
    group, monkeypatch
):
    event = object.__new__(QQOfficialMessageEvent)
    event.bot = SimpleNamespace()
    source = object.__new__(
        botpy.message.GroupMessage if group else botpy.message.C2CMessage
    )
    if group:
        source.group_openid = "group"
    else:
        source.author = SimpleNamespace(user_openid="user")
    event.message_obj = SimpleNamespace(raw_message=source, message_id="incoming")
    event.send_buffer = MessageChain([Record(file="voice.wav")])
    event.upload_group_and_c2c_media = AsyncMock(return_value=None)
    event._send_with_markdown_fallback = AsyncMock(return_value={"id": "false-success"})
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    monkeypatch.setattr(
        QQOfficialMessageEvent,
        "_parse_to_qqofficial",
        AsyncMock(
            return_value=("", None, None, "voice.wav", None, None, None),
        ),
    )
    with pytest.raises(RuntimeError, match="voice_upload_failed"):
        await event._post_send_one(event.send_buffer)
    event._send_with_markdown_fallback.assert_not_awaited()
