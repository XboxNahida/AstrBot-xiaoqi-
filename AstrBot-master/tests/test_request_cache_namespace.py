import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,support,expected", [
    ("https://api.openai.com/v1", None, True),
    ("https://api.openai.com/v1", False, False),
    ("https://relay.invalid/v1", None, False),
    ("https://relay.invalid/v1", True, True),
    ("https://generativelanguage.googleapis.com/v1beta/openai", None, False),
])
async def test_cache_key_is_sent_only_to_supported_endpoints(endpoint, support, expected):
    config = {"id": "mock", "type": "openai_chat_completion", "model": "gpt-test",
              "key": ["synthetic-key"], "api_base": endpoint}
    if support is not None:
        config["supports_prompt_cache_key"] = support
    provider = ProviderOpenAIOfficial(provider_config=config, provider_settings={})
    original = dict(config)
    try:
        payload, _ = await provider._prepare_chat_payload(prompt="test", prompt_cache_key="astrbot-chat-opaque")
        assert (payload.get("prompt_cache_key") == "astrbot-chat-opaque") is expected
        assert provider.provider_config == original
    finally:
        await provider.terminate()


@pytest.mark.asyncio
async def test_runner_keeps_namespace_through_primary_and_backup():
    runner = ToolLoopAgentRunner()
    primary = SimpleNamespace(provider_config={"id": "primary"}, text_chat=AsyncMock(return_value=LLMResponse(role="assistant", completion_text="ok")))
    backup = SimpleNamespace(provider_config={"id": "backup"}, text_chat=AsyncMock(return_value=LLMResponse(role="assistant", completion_text="ok")))
    runner.req = ProviderRequest(prompt_cache_key="astrbot-chat-opaque")
    runner.run_context = ContextWrapper(context=None)
    runner.streaming = False
    runner._pending_required_tools = set()
    runner.request_max_retries = 3
    runner._abort_signal = asyncio.Event()
    runner._fallback_from_provider_id = ""
    for provider in (primary, backup):
        runner.provider = provider
        _ = [response async for response in runner._iter_llm_responses()]
        assert provider.text_chat.await_args.kwargs["prompt_cache_key"] == "astrbot-chat-opaque"
        assert "prompt_cache_key" not in provider.provider_config


@pytest.mark.parametrize("reported,expected", [(None, None), (0, 0), (125, 125)])
def test_absent_cache_metrics_are_not_reported_as_a_miss(reported, expected):
    from astrbot.core.provider.entities import TokenUsage, reported_cached_tokens
    raw = SimpleNamespace(usage=SimpleNamespace(prompt_tokens_details=None if reported is None else SimpleNamespace(cached_tokens=reported)))
    response = LLMResponse(role="assistant", completion_text="ok", raw_completion=raw,
        usage=TokenUsage(input_other=200 - (reported or 0), input_cached=reported or 0, output=5))
    assert reported_cached_tokens(response) == expected
