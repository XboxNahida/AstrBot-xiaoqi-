# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.star import Context
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin
from astrbot_plugin_private_companion.token_budget import (
    TokenBudgetMixin,
    _looks_like_upstream_llm_error_response,
)


_GOOGLE_POLICY_ERROR = (
    "The。 prompt。 could。not。be。submitted.。The。prompt。contains。sensitive。words。"
    "that。violate。Google's。[Generative。AI。Prohibited。Use。policy]"
    "(https://policies.google.com/terms/generative-ai/use-policy).，"
    "Tryrephrasingtheprompt."
)


class _FakeProvider:
    def __init__(self, provider_id: str, responses: list[LLMResponse], calls: list[str]) -> None:
        self.provider_config = {"id": provider_id}
        self._responses = list(responses)
        self._calls = calls

    async def text_chat(self, **_kwargs) -> LLMResponse:
        self._calls.append(self.provider_config["id"])
        return self._responses[-1]

    def text_chat_stream(self, **_kwargs):
        async def _stream():
            self._calls.append(self.provider_config["id"])
            for response in self._responses:
                yield response

        return _stream()


class _DirectFallbackContext:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def llm_generate(self, **kwargs):
        provider_id = str(kwargs.get("chat_provider_id") or "")
        self.calls.append(provider_id)
        if provider_id == "primary":
            return SimpleNamespace(
                role="assistant",
                completion_text=_GOOGLE_POLICY_ERROR,
            )
        return SimpleNamespace(
            role="assistant",
            completion_text="天晚了，记得喝点水再休息。",
        )


class _RealDirectFallbackHarness(ProactiveMessageMixin, TokenBudgetMixin):
    def __init__(self) -> None:
        self.context = _DirectFallbackContext()
        self.provider_config_mode = "precision"
        self.llm_provider_id = "primary"
        self.response_review_provider_id = "primary"
        self.mai_style_provider_id = ""
        self.model_timeout_overrides = {}
        self.model_fallback_overrides = {"RESPONSE_REVIEW_PROVIDER_ID": "backup"}
        self.config = {}
        self.usage: list[dict] = []
        self.bot_name = "测试角色"

    def _classify_llm_prompt(self, _prompt: str) -> str:
        return "other"

    def _is_llm_budget_exempt_task(self, _task: str) -> bool:
        return False

    def _daily_token_soft_limit_should_defer(self, _task: str) -> bool:
        return False

    def _llm_daily_budget_remaining(self) -> int:
        return 100000

    def _record_llm_usage(self, **kwargs) -> None:
        self.usage.append(kwargs)

    @staticmethod
    def _task_provider(*provider_ids: str) -> str:
        return next((provider_id for provider_id in provider_ids if provider_id), "")

    async def _resolve_proactive_persona_prompt(self, *_args, **_kwargs) -> str:
        return "说话自然、简洁，像熟悉的朋友。"

    def _format_proactive_voice_prompt(self) -> str:
        return ""

    def _format_expression_voice_for_prompt(self, **_kwargs) -> str:
        return ""

    async def _recent_private_conversation_for_proactive_review(
        self,
        *_args,
        **_kwargs,
    ) -> str:
        return ""

    def _format_proactive_recipient_identity_guard(self, *_args, **_kwargs) -> str:
        return "当前收件人：测试用户。"

    def _format_action_prompt_context(self, *_args, **_kwargs) -> str:
        return ""

    @staticmethod
    def _strip_internal_identity_anchors(text: str) -> str:
        return text


def _native_fallback_runner(
    providers: list[_FakeProvider],
    *,
    streaming: bool = False,
) -> ToolLoopAgentRunner:
    runner = object.__new__(ToolLoopAgentRunner)
    runner.provider = providers[0]
    runner.fallback_providers = providers[1:]
    runner.streaming = streaming
    runner.run_context = SimpleNamespace(messages=[])
    runner.req = SimpleNamespace(
        session_id="default:FriendMessage:10001",
        extra_user_content_parts=[],
        model=None,
        func_tool=None,
    )
    runner._abort_signal = asyncio.Event()
    runner.request_max_retries = 1
    runner.final_llm_resp = None
    runner._is_task_request = False
    runner._pending_required_tools = set()
    return runner


async def _collect_native_fallback(runner: ToolLoopAgentRunner) -> list[LLMResponse]:
    return [response async for response in runner._iter_llm_responses_with_fallback()]


class _FrameworkExecutionHarness(ProactiveMessageMixin):
    def __init__(self, context: Context) -> None:
        self.context = context
        self.enable_llm_proactive_message = True
        self._framework_captured_send_cache = {}
        self.direct_fallback_calls = 0

    async def _get_current_conversation_safely(self, *_args, **_kwargs):
        return None

    async def _capture_framework_send_message_calls(self, *, runner_factory, **_kwargs):
        result = await runner_factory()
        runner = result.agent_runner
        responses = await _collect_native_fallback(runner)
        runner.final_llm_resp = responses[-1] if responses else None
        return result, []

    async def _generate_proactive_message_via_framework(
        self,
        user,
        name,
        reason,
        action_context="",
        action="message",
        motive="",
    ) -> str:
        return await self._run_framework_agent_text(
            umo=user["umo"],
            prompt="主动消息测试",
            name=name,
            label="semantic_fallback_e2e",
        )

    async def _generate_proactive_message_direct_fallback(self, *_args, **_kwargs) -> str:
        self.direct_fallback_calls += 1
        return "这是人格化直接兜底。"

    async def _finalize_proactive_generated_text(self, _user, raw_text, **_kwargs):
        return str(raw_text or "").strip(), ""


class SemanticProviderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_direct_persona_fallback_uses_card_backup_provider(self) -> None:
        harness = _RealDirectFallbackHarness()

        text = await harness._generate_proactive_message_direct_fallback(
            {
                "user_id": "10001",
                "umo": "default:FriendMessage:10001",
                "nickname": "测试用户",
            },
            name="测试用户",
            reason="quiet_care",
            action="message",
        )

        self.assertEqual(text, "天晚了，记得喝点水再休息。")
        self.assertEqual(harness.context.calls, ["primary", "backup"])
        self.assertEqual(harness.usage[0]["error"], "semantic_provider_error")
        self.assertFalse(harness.usage[0]["success"])
        self.assertTrue(harness.usage[1]["success"])

    async def test_primary_semantic_error_uses_astrbot_native_fallback(self) -> None:
        calls: list[str] = []
        primary = _FakeProvider(
            "primary",
            [LLMResponse(role="assistant", completion_text=_GOOGLE_POLICY_ERROR)],
            calls,
        )
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="晚点也要记得吃饭。")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback])
        harness = ProactiveMessageMixin()

        self.assertTrue(
            harness._install_proactive_semantic_provider_fallback(
                SimpleNamespace(agent_runner=runner),
                label="primary_fallback_test",
            )
        )
        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].role, "assistant")
        self.assertEqual(responses[0].completion_text, "晚点也要记得吃饭。")

    async def test_consecutive_semantic_errors_reach_next_fallback(self) -> None:
        calls: list[str] = []
        providers = [
            _FakeProvider(
                "primary",
                [LLMResponse(role="assistant", completion_text=_GOOGLE_POLICY_ERROR)],
                calls,
            ),
            _FakeProvider(
                "fallback-1",
                [
                    LLMResponse(
                        role="assistant",
                        completion_text="The prompt could not be submitted.",
                    )
                ],
                calls,
            ),
            _FakeProvider(
                "fallback-2",
                [LLMResponse(role="assistant", completion_text="我换了一条线路回来啦。")],
                calls,
            ),
        ]
        runner = _native_fallback_runner(providers)
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="multi_fallback_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary", "fallback-1", "fallback-2"])
        self.assertEqual([item.completion_text for item in responses], ["我换了一条线路回来啦。"])

    async def test_normal_assistant_text_is_unchanged_without_fallback(self) -> None:
        calls: list[str] = []
        normal_text = "刚才那段技术讨论先放一放，我只是想提醒你喝口水。"
        primary = _FakeProvider(
            "primary",
            [LLMResponse(role="assistant", completion_text=normal_text)],
            calls,
        )
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="不应调用")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback])
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="normal_text_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary"])
        self.assertEqual(responses[0].role, "assistant")
        self.assertEqual(responses[0].completion_text, normal_text)

    async def test_technical_boundary_terms_do_not_trigger_native_fallback(self) -> None:
        normal_messages = (
            "你刚才说的 tool schema 我看懂了，先歇一会儿吧。",
            "那个页面显示 status disabled，晚点我陪你再看。",
            "别再盯着 traceback 了，先喝口水。",
            "工具调用失败这种提示确实很烦，但先别折腾了。",
        )
        for normal_text in normal_messages:
            with self.subTest(normal_text=normal_text):
                calls: list[str] = []
                primary = _FakeProvider(
                    "primary",
                    [LLMResponse(role="assistant", completion_text=normal_text)],
                    calls,
                )
                fallback = _FakeProvider(
                    "fallback",
                    [LLMResponse(role="assistant", completion_text="不应调用")],
                    calls,
                )
                runner = _native_fallback_runner([primary, fallback])
                harness = ProactiveMessageMixin()
                harness._install_proactive_semantic_provider_fallback(
                    SimpleNamespace(agent_runner=runner),
                    label="technical_boundary_test",
                )

                responses = await _collect_native_fallback(runner)

                self.assertFalse(_looks_like_upstream_llm_error_response(normal_text))
                self.assertEqual(calls, ["primary"])
                self.assertEqual(responses[0].completion_text, normal_text)

    async def test_all_semantic_errors_are_sanitized_and_reach_direct_fallback(self) -> None:
        calls: list[str] = []
        primary = _FakeProvider(
            "primary",
            [LLMResponse(role="assistant", completion_text=_GOOGLE_POLICY_ERROR)],
            calls,
        )
        usage = SimpleNamespace(total=27)
        fallback = _FakeProvider(
            "fallback",
            [
                LLMResponse(
                    role="assistant",
                    completion_text="The prompt could not be submitted.",
                    id="fallback-error-id",
                    usage=usage,
                )
            ],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback])
        context = object.__new__(Context)
        context.get_config = lambda **_kwargs: {"provider_settings": {}}
        harness = _FrameworkExecutionHarness(context)
        build_result = SimpleNamespace(agent_runner=runner)

        with patch(
            "astrbot_plugin_private_companion.proactive_message.build_main_agent",
            new=AsyncMock(return_value=build_result),
        ):
            text = await harness._generate_proactive_message_with_llm(
                {"user_id": "10001", "umo": "default:FriendMessage:10001"},
                "测试角色",
                "quiet_care",
            )

        final_response = runner.get_final_llm_resp()
        self.assertEqual(calls, ["primary", "fallback"])
        self.assertIsNotNone(final_response)
        self.assertEqual(final_response.role, "err")
        self.assertEqual(final_response.id, "fallback-error-id")
        self.assertIs(final_response.usage, usage)
        self.assertNotIn("prompt could not be submitted", final_response.completion_text.lower())
        self.assertEqual(harness.direct_fallback_calls, 1)
        self.assertEqual(text, "这是人格化直接兜底。")

    async def test_provider_error_candidate_stops_before_proactive_cleanup(self) -> None:
        harness = ProactiveMessageMixin()

        text, stage = await harness._finalize_proactive_generated_text(
            {"user_id": "10001"},
            _GOOGLE_POLICY_ERROR,
            name="测试角色",
            reason="quiet_care",
            action="message",
        )

        self.assertEqual(text, "")
        self.assertEqual(stage, "Provider/API 错误正文")

    async def test_tool_call_response_is_not_reclassified(self) -> None:
        calls: list[str] = []
        tool_response = LLMResponse(
            role="assistant",
            completion_text="The prompt could not be submitted.",
            tools_call_name=["safe_tool"],
            tools_call_args=[{}],
            tools_call_ids=["call-1"],
        )
        primary = _FakeProvider("primary", [tool_response], calls)
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="不应调用")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback])
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="tool_call_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary"])
        self.assertIs(responses[0], tool_response)

    async def test_non_text_response_is_not_reclassified(self) -> None:
        calls: list[str] = []
        media_response = LLMResponse(
            role="assistant",
            completion_text="The prompt could not be submitted.",
        )
        media_response.result_chain = SimpleNamespace(
            chain=[object()],
            get_plain_text=lambda: "The prompt could not be submitted.",
        )
        primary = _FakeProvider("primary", [media_response], calls)
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="不应调用")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback])
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="media_response_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary"])
        self.assertIs(responses[0], media_response)

    async def test_streamed_error_chunks_do_not_escape_before_fallback(self) -> None:
        calls: list[str] = []
        primary = _FakeProvider(
            "primary",
            [
                LLMResponse(
                    role="assistant",
                    completion_text="The prompt could not ",
                    is_chunk=True,
                ),
                LLMResponse(
                    role="assistant",
                    completion_text="The prompt could not be submitted.",
                ),
            ],
            calls,
        )
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="流式回退成功。")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback], streaming=True)
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="streaming_fallback_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual(len(responses), 1)
        self.assertFalse(responses[0].is_chunk)
        self.assertEqual(responses[0].completion_text, "流式回退成功。")

    async def test_streamed_chunks_are_suppressed_when_final_role_is_error(self) -> None:
        calls: list[str] = []
        primary = _FakeProvider(
            "primary",
            [
                LLMResponse(
                    role="assistant",
                    completion_text="partial upstream output",
                    is_chunk=True,
                ),
                LLMResponse(
                    role="err",
                    completion_text="opaque upstream error",
                ),
            ],
            calls,
        )
        fallback = _FakeProvider(
            "fallback",
            [LLMResponse(role="assistant", completion_text="原生错误回退成功。")],
            calls,
        )
        runner = _native_fallback_runner([primary, fallback], streaming=True)
        harness = ProactiveMessageMixin()
        harness._install_proactive_semantic_provider_fallback(
            SimpleNamespace(agent_runner=runner),
            label="streaming_native_error_test",
        )

        responses = await _collect_native_fallback(runner)

        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual(len(responses), 1)
        self.assertFalse(responses[0].is_chunk)
        self.assertEqual(responses[0].completion_text, "原生错误回退成功。")


if __name__ == "__main__":
    unittest.main()
