import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.agent.context.config import ContextConfig
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.context.recent import RecentOnlyCompressor
from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.message import Message
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.db.po import Conversation
from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
    InternalAgentSubStage,
)
from astrbot.core.provider.entities import LLMResponse, ProviderRequest


def history():
    result = []
    for i in range(30):
        result.extend([{"role": "user", "content": f"question {i}"},
                       {"role": "assistant", "content": f"answer {i}"}])
    return result


@pytest.mark.asyncio
async def test_two_consecutive_image_turns_remain_in_next_text_request_and_archive():
    """真实runner及核心归档链，验证两次独立图片事件都留给下一轮文字。"""
    import base64
    import json
    archive = []
    urls = ["data:image/png;base64," + base64.b64encode(value).decode() for value in (b"first", b"second")]
    provider = SimpleNamespace(provider_config={"id": "mock", "max_context_tokens": 128000},
        supports_image=lambda: True)
    stage = InternalAgentSubStage()
    stage.conv_manager = AsyncMock()
    for prompt, images in [("第一张", [urls[0]]), ("第二张", [urls[1]]), ("比较这两张", [])]:
        req = ProviderRequest(prompt=prompt, image_urls=images, contexts=archive,
            history_policy="recent_only", system_prompt="persona",
            conversation=Conversation(platform_id="qq", user_id="media-session", cid="media-cid"))
        runner = ToolLoopAgentRunner()
        await runner.reset(provider=provider, request=req,
            run_context=ContextWrapper(context=None), tool_executor=SimpleNamespace(),
            agent_hooks=BaseAgentRunHooks(), enforce_max_turns=12)
        if not images:
            request_payload = json.dumps([m.model_dump() for m in runner.run_context.messages])
            assert all(url in request_payload for url in urls)
        runner.run_context.messages.append(Message(role="assistant", content="已收到"))
        await stage._save_to_history(
            SimpleNamespace(unified_msg_origin="media-session", get_extra=lambda _: None),
            req, LLMResponse(role="assistant", completion_text="已收到"),
            runner.run_context.messages, runner_stats=None,
        )
        archive = stage.conv_manager.update_conversation.await_args.kwargs["history"]
    assert [m["content"][1]["image_url"]["url"] for m in archive if m["role"] == "user" and isinstance(m["content"], list)] == urls


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "error", "abort"])
@pytest.mark.parametrize("checkpoint", [False, True])
async def test_runner_preserves_full_archive_and_new_checkpoint(ending, checkpoint):
    archive = history()
    archive.extend([
        {"role": "user", "content": "old tool question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "old-call", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "old-call", "content": "old evidence"},
        {"role": "assistant", "content": "old tool answer"},
        {"role": "_checkpoint", "content": {"id": "old-cp"}},
    ])
    original = copy.deepcopy(archive)
    req = ProviderRequest(prompt="current question", contexts=archive,
        history_policy="recent_only", system_prompt="persona",
        conversation=Conversation(platform_id="qq", user_id="session", cid="cid"))
    runner = ToolLoopAgentRunner()
    provider = SimpleNamespace(provider_config={"id": "mock", "max_context_tokens": 128000})
    await runner.reset(provider=provider, request=req,
        run_context=ContextWrapper(context=None), tool_executor=SimpleNamespace(),
        agent_hooks=BaseAgentRunHooks(), enforce_max_turns=12)
    historical = [m for m in runner.run_context.messages if m._no_save]
    assert len([m for m in historical if m.role == "user"]) == 12
    assert all(m._no_save for m in runner.run_context.messages[1:-1])
    assert not runner.run_context.messages[-1]._no_save
    assert runner.run_context.messages[1].content == "question 19"
    runner.run_context.messages.append(Message(role="assistant", content="current answer"))
    stage = InternalAgentSubStage()
    stage.conv_manager = AsyncMock()
    event = SimpleNamespace(unified_msg_origin="session", get_extra=lambda key: "new-cp" if checkpoint and key == "llm_checkpoint_id" else None)
    response = LLMResponse(role="err" if ending == "error" else "assistant", completion_text="current answer")
    await stage._save_to_history(event, req, response, runner.run_context.messages,
                                 runner_stats=None, user_aborted=ending == "abort")
    saved = stage.conv_manager.update_conversation.await_args.kwargs["history"]
    assert saved[:len(original)] == original
    assert len(saved) == len(original) + 2 + int(checkpoint)
    added = saved[len(original):]
    assert added[0]["content"] == "current question"
    assert added[1]["content"] == "current answer"
    assert added[1]["conversation_id"] == "cid"
    assert added[1]["message_id"] != added[0]["message_id"]
    if checkpoint:
        assert saved[-1] == {"role": "_checkpoint", "content": {"id": "new-cp"}}
    assert req.contexts == archive[38:]
    assert archive == original
    from astrbot.core.agent.context.recent import merge_archive
    assert merge_archive(req, added) == saved


@pytest.mark.asyncio
async def test_recent_budget_never_halves_current_material():
    manager = ContextManager(ContextConfig(max_context_tokens=20,
                                          custom_compressor=RecentOnlyCompressor()))
    messages = [Message(role="system", content="persona"),
                Message(role="user", content="CURRENT SOURCE " * 200),
                Message(role="assistant", content="checking")]
    processed = await manager.process(messages)
    assert [m.content for m in processed] == [m.content for m in messages]


@pytest.mark.asyncio
async def test_compacted_lookup_keeps_original_for_archive():
    req = ProviderRequest(history_policy="recent_only", history_archive=[])
    compressor = RecentOnlyCompressor(request=req)
    messages = [Message(role="user", content="current"),
                Message(role="assistant", tool_calls=[{"id": "lookup", "type": "function", "function": {"name": "pc_read_chat_history", "arguments": "{}"}}]),
                Message(role="tool", tool_call_id="lookup", content="SOURCE" * 5000),
                Message(role="assistant", tool_calls=[{"id": "latest", "type": "function", "function": {"name": "pc_read_chat_history", "arguments": "{}"}}]),
                Message(role="tool", tool_call_id="latest", content="latest evidence")]
    compacted = await compressor(messages)
    assert len(compacted[2].content) < 800
    from astrbot.core.agent.context.recent import merge_archive
    from astrbot.core.agent.message import dump_messages_with_checkpoints
    stored = merge_archive(req, dump_messages_with_checkpoints(compacted))
    assert stored[2]["content"] == messages[2].content


def test_persona_examples_are_outside_recent_history_budget():
    from astrbot.core.agent.context.recent import select_recent
    examples = [{"role": "user", "content": "example question", "_no_save": True},
                {"role": "assistant", "content": "example reply", "_no_save": True}]
    selected = select_recent(examples + history(), 12)
    assert selected[:2] == examples
    assert selected[2]["content"] == "question 18"
    assert len(selected) == 26


@pytest.mark.asyncio
async def test_budget_drops_history_but_keeps_persona_and_current_user():
    req = ProviderRequest(contexts=[
        {"role": "user", "content": "example", "_no_save": True},
        {"role": "assistant", "content": "persona", "_no_save": True},
        {"role": "user", "content": "old"},
    ])
    messages = [Message(role="user", content="example"),
                Message(role="assistant", content="persona"),
                Message(role="user", content="old"),
                Message(role="assistant", content="old answer"),
                Message(role="user", content="CURRENT SOURCE")]
    for message in messages[:-1]:
        message._no_save = True
    processed = await RecentOnlyCompressor(request=req)(messages)
    assert [m.content for m in processed] == ["example", "persona", "CURRENT SOURCE"]


def test_tool_image_is_part_of_one_real_turn_when_selecting_recent_history():
    from astrbot.core.agent.context.recent import select_recent
    old_turn = [
        {"role": "user", "content": "inspect", "turn_id": "old"},
        {"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}], "turn_id": "old"},
        {"role": "tool", "content": "image result", "tool_call_id": "a", "turn_id": "old"},
        {"role": "user", "content": [{"type": "text", "text": "[Image from tool 'read']"}, {"type": "image_url", "image_url": {"url": "img.png"}}], "turn_id": "old"},
        {"role": "assistant", "content": "image answer", "turn_id": "old"},
    ]
    newest = [{"role": "user", "content": "new question", "turn_id": "new"},
              {"role": "assistant", "content": "new answer", "turn_id": "new"}]
    assert select_recent(old_turn + newest, 2) == old_turn + newest
    assert select_recent(old_turn + newest, 1) == newest


@pytest.mark.asyncio
async def test_unhandled_agent_error_still_archives_inside_session_lock(monkeypatch):
    from astrbot.core.astr_main_agent import MainAgentBuildConfig
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
    stage = InternalAgentSubStage()
    stage.conv_manager = AsyncMock()
    stage.ctx = SimpleNamespace(plugin_manager=SimpleNamespace(context=None))
    stage.main_agent_cfg = MainAgentBuildConfig(tool_call_timeout=60)
    stage.streaming_response = False
    stage.unsupported_streaming_strategy = "turn_off"
    stage.max_step = 5
    stage.show_tool_use = stage.show_tool_call_result = stage.show_reasoning = False
    stage.buffer_intermediate_messages = False
    request = ProviderRequest(history_policy="recent_only", history_archive=history(),
        conversation=Conversation(platform_id="qq", user_id="session-exception", cid="cid"))
    runner = SimpleNamespace(run_context=SimpleNamespace(messages=[Message(role="user", content="current raw")]),
                             stats=None, was_aborted=lambda: False)
    provider = SimpleNamespace(provider_config={}, get_model=lambda: "synthetic")
    event = SimpleNamespace(get_extra=lambda _: None, message_str="question",
        message_obj=SimpleNamespace(message=[]), unified_msg_origin="session-exception",
        send_typing=AsyncMock(), stop_typing=AsyncMock(), send=AsyncMock(),
        platform_meta=SimpleNamespace(support_streaming_message=False), trace=SimpleNamespace(record=MagicMock()),
        is_stopped=lambda: False)
    async def failing_run(*args, **kwargs):
        raise RuntimeError("synthetic interrupted stream")
        yield
    monkeypatch.setattr(internal, "build_main_agent", AsyncMock(return_value=SimpleNamespace(
        agent_runner=runner, provider_request=request, provider=provider, reset_coro=None)))
    monkeypatch.setattr(internal, "try_capture_follow_up", lambda _: None)
    monkeypatch.setattr(internal, "call_event_hook", AsyncMock(return_value=False))
    monkeypatch.setattr(internal, "register_active_runner", lambda *args: None)
    monkeypatch.setattr(internal, "unregister_active_runner", lambda *args: None)
    monkeypatch.setattr(internal, "extract_persona_custom_error_message_from_event", lambda _: None)
    monkeypatch.setattr(internal, "run_agent", failing_run)
    _ = [item async for item in stage.process(event, "")]
    saved = stage.conv_manager.update_conversation.await_args.kwargs["history"]
    assert saved[:60] == history()
    assert saved[-1]["content"] == "current raw"
