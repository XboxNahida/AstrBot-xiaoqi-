import asyncio
import copy
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import ImageURLPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import LLMResponse, ProviderRequest


@pytest.mark.asyncio
async def test_real_internal_chain_absorbs_two_inputs_and_sends_only_revised_answer(monkeypatch):
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal

    @dataclass
    class Config:
        provider_wake_prefix: str = ""
        streaming_response: bool = False
        provider_settings: dict = field(default_factory=dict)

    stage = internal.InternalAgentSubStage()
    stage.main_agent_cfg = Config()
    stage.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=object()))
    stage.streaming_response = False
    stage.unsupported_streaming_strategy = "turn_off"
    stage.max_step = 5
    stage.show_tool_use = stage.show_tool_call_result = stage.show_reasoning = False
    stage.buffer_intermediate_messages = False
    saved = []

    async def save(event, req, response, messages, *args, **kwargs):
        saved.append(copy.deepcopy(messages))

    stage._save_to_history = save
    monkeypatch.setattr(internal, "call_event_hook", AsyncMock(return_value=False))
    monkeypatch.setattr(internal, "_record_internal_agent_stats", AsyncMock())
    monkeypatch.setattr(internal.Metric, "upload", AsyncMock())
    provider = SimpleNamespace(provider_config={"id": "mock", "max_context_tokens": 128000},
        get_model=lambda: "mock", meta=lambda: SimpleNamespace(type="mock"))
    began = asyncio.Event()
    release = asyncio.Event()
    calls = 0
    request_views = []
    runners = []

    async def build(**kwargs):
        event = kwargs["event"]
        req = ProviderRequest(prompt=event.message_str, image_urls=event.test_images,
            model_managed_loop=True, history_policy="recent_only")
        runner = ToolLoopAgentRunner()

        async def responses():
            nonlocal calls
            calls += 1
            request_views.append(copy.deepcopy(runner.run_context.messages))
            if calls == 1:
                began.set()
                await release.wait()
            yield LLMResponse(role="assistant", completion_text="过时草稿" if calls == 1 else "结合全部输入的回复")

        runner._iter_llm_responses_with_fallback = responses
        runners.append(runner)
        reset_kwargs = dict(provider=provider, request=req,
            run_context=ContextWrapper(context=SimpleNamespace(event=event)),
            tool_executor=SimpleNamespace(), agent_hooks=BaseAgentRunHooks())
        return SimpleNamespace(agent_runner=runner, provider_request=req, provider=provider,
            reset_kwargs=reset_kwargs, reset_coro=runner.reset(**reset_kwargs))

    monkeypatch.setattr(internal, "build_main_agent", build)

    def event(mid, text, images=None):
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        message.sender = MessageMember(user_id="owner", nickname="owner")
        message.message_id, message.message_str, message.message = mid, text, []
        result = AstrMessageEvent(text, message, PlatformMetadata("mock", "mock", "test-bot"), "owner")
        result.test_images = images or []
        result._private_companion_unified_queue = True
        result.send = AsyncMock(return_value=True)
        result.send_typing = result.stop_typing = AsyncMock()
        return result

    async def process(event):
        async for _ in stage.process(event, ""):
            result = event.get_result()
            if result is not None and result.chain:
                await event.send(result)

    first = event("first", "比较图片")
    second = event("second", "第一张" * 200, ["data:image/png;base64,AQ=="])
    third = event("third", "第二张", ["data:image/png;base64,Ag=="])
    running = asyncio.create_task(process(first))
    await asyncio.wait_for(began.wait(), 2)
    followups = [asyncio.create_task(process(second)), asyncio.create_task(process(third))]
    for _ in range(20):
        await asyncio.sleep(0)
        if len(runners[0].conversation_turn.queue) == 3:
            break
    assert len(runners[0].conversation_turn.queue) == 3
    release.set()
    await asyncio.wait_for(asyncio.gather(running, *followups), 3)
    assert calls == 2
    assert first.send.await_count == 1
    assert first.send.await_args.args[0].get_plain_text() == "结合全部输入的回复"
    second.send.assert_not_awaited()
    third.send.assert_not_awaited()
    assert len(saved) == 1
    images = [p.image_url.url for m in saved[0] if isinstance(m.content, list)
              for p in m.content if isinstance(p, ImageURLPart)]
    assert images == ["data:image/png;base64,AQ==", "data:image/png;base64,Ag=="]
    assert any("第一张" * 200 in str(m.content) for m in request_views[-1])
    assert not any(m.role == "assistant" and m.content == "过时草稿" for m in saved[0])
