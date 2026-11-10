import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.message import Message
from astrbot.core.pipeline.process_stage.conversation_queue import enqueue_conversation


@pytest.mark.asyncio
async def test_stage_prepares_queued_multimodal_message_through_normal_hooks(monkeypatch):
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
    from astrbot.core.provider.entities import ProviderRequest

    @dataclass
    class Config:
        provider_wake_prefix: str = ""

    stage = internal.InternalAgentSubStage()
    stage.main_agent_cfg = Config()
    stage.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=object()))
    event = SimpleNamespace(message_obj=SimpleNamespace(message_id="second", timestamp=123))
    request = ProviderRequest(prompt="完整原文" * 400,
        image_urls=["data:image/png;base64,AA==", "data:image/png;base64,AQ=="])
    reset = AsyncMock()()
    build = AsyncMock(return_value=SimpleNamespace(provider_request=request, reset_coro=reset))
    hook = AsyncMock(return_value=False)
    monkeypatch.setattr(internal, "build_main_agent", build)
    monkeypatch.setattr(internal, "call_event_hook", hook)
    owner = object()
    message = await stage._prepare_queued_message(event, "", owner)
    assert len([p for p in message.content if p.type == "image_url"]) == 2
    assert "完整原文" * 400 in message.content[0].text
    assert event._conversation_owner_event is owner
    assert build.await_args.kwargs["apply_reset"] is False
    assert reset.cr_frame is None
    assert hook.await_count == 2


@pytest.mark.asyncio
async def test_queue_keeps_full_inputs_and_callback_order():
    async def prepare(text):
        return Message(role="user", content=text)

    first = enqueue_conversation("bot:private:1", "user", key="a")
    second = enqueue_conversation(
        first.umo, "user", key="b", prepare=lambda: prepare("x" * 900)
    )
    callback = enqueue_conversation(
        first.umo, "user", key="task:1", kind="callback",
        prepare=lambda: prepare("task result"),
    )
    assert await first.wait_turn()
    messages = await first.drain()
    assert [m.content for m in messages] == ["x" * 900, "task result"]
    assert not await second.wait_turn()
    assert not callback.finished.done()
    first.finish("sent")
    assert await callback.finished == "handled"


@pytest.mark.asyncio
async def test_queue_does_not_inherit_another_senders_permission():
    first = enqueue_conversation("bot:group:1", "owner", key="a")
    other = enqueue_conversation("bot:group:1", "guest", key="b")
    assert await first.drain() == []
    first.commit()
    late = enqueue_conversation("bot:group:1", "owner", key="c")
    assert await first.drain() == []
    first.finish("sent")
    assert await other.wait_turn()
    assert not late.ready.is_set()
    other.finish("sent")
    assert await late.wait_turn()
    late.finish("sent")


@pytest.mark.asyncio
async def test_prepare_failure_returns_original_event_to_fifo():
    async def broken():
        raise ValueError("input not prepared")

    first = enqueue_conversation("bot:private:2", "user", key="a")
    later = enqueue_conversation(first.umo, "user", key="b", prepare=broken)
    assert await first.drain() == []
    assert not later.consumed
    first.finish("unknown")
    assert await later.wait_turn()
    later.finish("not_sent")


@pytest.mark.asyncio
async def test_cancel_waiter_and_idempotent_callback_do_not_block_queue():
    first = enqueue_conversation("bot:private:3", "user", key="a")
    second = enqueue_conversation(first.umo, "user", key="b")
    assert enqueue_conversation(first.umo, "user", key="b") is second
    second.cancel()
    third = enqueue_conversation(first.umo, "user", key="c")
    first.finish("silent")
    assert await asyncio.wait_for(third.wait_turn(), 0.2)
    third.finish("silent")


@pytest.mark.asyncio
async def test_separate_sessions_do_not_wait_for_each_other():
    first = enqueue_conversation("bot-a:group:1", "user", key="a")
    second = enqueue_conversation("bot-b:group:1", "user", key="a")
    assert await first.wait_turn()
    assert await second.wait_turn()
    first.finish("unknown")
    second.finish("sent")


@pytest.mark.asyncio
async def test_idempotent_turn_cannot_acquire_a_second_owner():
    first = enqueue_conversation("bot:private:duplicate", "user", key="same")
    assert await first.wait_turn()
    duplicate = asyncio.create_task(first.wait_turn())
    await asyncio.sleep(0)
    assert not duplicate.done()
    first.finish("sent")
    assert not await duplicate


@pytest.mark.asyncio
async def test_pipeline_releases_an_input_stopped_before_agent_stage():
    from astrbot.core.pipeline.scheduler import PipelineScheduler
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata

    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.sender = MessageMember(user_id="u", nickname="u")
    message.message_id, message.message_str, message.message = "skip", "静默", []
    event = AstrMessageEvent("静默", message, PlatformMetadata("mock", "mock", "bot"), "early")
    turn = enqueue_conversation(event.unified_msg_origin, "u", key="first")
    turn.source_event = event
    event._conversation_turn = turn
    following = enqueue_conversation(event.unified_msg_origin, "u", key="second")
    scheduler = PipelineScheduler.__new__(PipelineScheduler)
    scheduler._process_stages = AsyncMock()
    await scheduler.execute(event)
    assert await turn.finished == "not_sent"
    assert await following.wait_turn()
    following.finish("sent")
