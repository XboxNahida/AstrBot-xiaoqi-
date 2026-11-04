import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.message.components import Json
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.error_response import looks_like_upstream_llm_error_response


def _runner(primary_responses, backup_responses=None):
    runner = ToolLoopAgentRunner()
    runner.provider = SimpleNamespace(
        provider_config={"id": "primary"},
        text_chat=AsyncMock(side_effect=primary_responses),
    )
    runner.fallback_providers = []
    if backup_responses is not None:
        runner.fallback_providers.append(SimpleNamespace(
            provider_config={"id": "backup"},
            text_chat=AsyncMock(side_effect=backup_responses),
        ))
    runner.streaming = False
    runner.run_context = SimpleNamespace(messages=[])
    runner.req = ProviderRequest()
    runner._abort_signal = asyncio.Event()
    runner.request_max_retries = 1
    runner._is_task_request = False
    runner._pending_required_tools = set()
    runner.EMPTY_OUTPUT_RETRY_WAIT_MIN_S = 0
    runner.EMPTY_OUTPUT_RETRY_WAIT_MAX_S = 0
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    "All chat models failed: private-upstream-detail",
    "The prompt could not be submitted.",
    "RateLimitError: status code 429, private-upstream-detail",
])
async def test_semantic_error_uses_configured_backup_without_publishing_error(error):
    runner = _runner(
        [LLMResponse(role="assistant", completion_text=error)],
        [LLMResponse(role="assistant", completion_text="recovered")],
    )
    primary = runner.provider
    backup = runner.fallback_providers[0]

    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert [item.completion_text for item in responses] == ["recovered"]
    assert primary.text_chat.await_count == backup.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_semantic_error_without_backup_retries_only_model_request():
    runner = _runner([
        LLMResponse(role="assistant", completion_text="All chat models failed"),
        LLMResponse(role="assistant", completion_text="recovered"),
    ])

    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert [item.completion_text for item in responses] == ["recovered"]
    assert runner.provider.text_chat.await_count == 2


@pytest.mark.asyncio
async def test_semantic_error_exhaustion_is_bounded_and_sanitized():
    usage = SimpleNamespace(total=27)
    runner = _runner([
        LLMResponse(role="assistant", completion_text="All chat models failed: private-upstream-detail",
                    id="failed-response", usage=usage)
        for _ in range(3)
    ])

    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert runner.provider.text_chat.await_count == 3
    assert len(responses) == 1
    assert responses[0].role == "err"
    assert "E_LLM_ALL_PROVIDERS_FAILED" in responses[0].completion_text
    assert "private-upstream-detail" not in responses[0].completion_text
    assert responses[0].id == "failed-response"
    assert responses[0].usage is usage


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "“All chat models failed” 表示所有聊天模型请求失败。",
    "All chat models failed 表示所有聊天模型请求失败。",
    "All chat models failed means every configured provider failed.",
    "截图里的终端提示 All chat models failed。",
    "`RateLimitError: status code 429` 通常表示请求过于频繁。",
    "你可以检查日志里的 All chat models failed 和 request id。",
    "The prompt could not be submitted 表示请求没有提交成功。",
    '{"explanation": "All chat models failed 表示所有模型请求失败"}',
])
async def test_normal_error_explanation_does_not_use_backup(text):
    runner = _runner([LLMResponse(role="assistant", completion_text=text)], [])
    primary = runner.provider

    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert [item.completion_text for item in responses] == [text]
    assert primary.text_chat.await_count == 1
    runner.fallback_providers[0].text_chat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool_call", "tool_result", "non_plain"])
async def test_error_text_with_tool_or_non_plain_content_is_preserved(kind):
    response = LLMResponse(role="assistant", completion_text="All chat models failed")
    if kind == "tool_call":
        response.tools_call_name = ["test_tool"]
        response.tools_call_args = [{}]
        response.tools_call_ids = ["one-call"]
    elif kind == "tool_result":
        response.role = "tool"
    else:
        response.result_chain = MessageChain(chain=[Json(data={"error": "tool result"})])
    runner = _runner([response], [])

    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert responses == [response]
    runner.fallback_providers[0].text_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_and_native_error_keep_existing_behaviors():
    runner = _runner([asyncio.CancelledError()], [])
    with pytest.raises(asyncio.CancelledError):
        _ = [item async for item in runner._iter_llm_responses_with_fallback()]
    runner.fallback_providers[0].text_chat.assert_not_awaited()

    runner = _runner(
        [LLMResponse(role="err", completion_text="native error")],
        [LLMResponse(role="assistant", completion_text="recovered")],
    )
    primary = runner.provider
    responses = [item async for item in runner._iter_llm_responses_with_fallback()]
    assert [item.completion_text for item in responses] == ["recovered"]
    assert primary.text_chat.await_count == 1


@pytest.mark.asyncio
async def test_started_stream_is_not_replayed_after_error_text():
    runner = _runner([], [])
    runner.streaming = True

    async def stream(**_kwargs):
        yield LLMResponse(role="assistant", completion_text="already visible", is_chunk=True)
        yield LLMResponse(role="err", completion_text="stream failed")

    runner.provider.text_chat_stream = stream
    responses = [item async for item in runner._iter_llm_responses_with_fallback()]

    assert responses[0].completion_text == "already visible"
    runner.fallback_providers[0].text_chat.assert_not_awaited()


@pytest.mark.parametrize("text, expected", [
    ("All chat models failed", True),
    ("The。 prompt。 could。not。be。submitted.。The。prompt。contains。sensitive。words。", True),
    ("RateLimitError: status code 429", True),
    ("All chat models failed means every provider failed.", False),
    ("“All chat models failed” 表示所有聊天模型失败。", False),
    ("截图里的终端提示 All chat models failed。", False),
    ('{"explanation": "All chat models failed 表示所有模型失败"}', False),
])
def test_strict_shared_detection_distinguishes_error_envelopes(text, expected):
    assert looks_like_upstream_llm_error_response(text, require_error_envelope=True) is expected
