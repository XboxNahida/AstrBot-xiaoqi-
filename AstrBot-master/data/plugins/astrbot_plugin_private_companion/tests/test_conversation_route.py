import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from astrbot.core.provider import Provider
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot_plugin_private_companion import conversation_route as route
from astrbot_plugin_private_companion.capability_hub import CapabilityDecision


def fake_provider(identity):
    item = MagicMock(spec=Provider)
    item.provider_config = {"id": identity, "model": identity}
    item.get_model.return_value = identity
    item.text_chat = AsyncMock()
    return item


class ConversationRouteTests(unittest.TestCase):
    def setUp(self):
        self.providers = {
            name: fake_provider(name)
            for name in (
                "gemini",
                "grok",
                "kimi",
                "gpt",
                "gpt-fallback",
                "deepseek",
                "claude",
            )
        }
        self.plugin = SimpleNamespace(
            enable_conversation_model_routing=True,
            conversation_chat_provider_id="gemini",
            conversation_chat_fallback_provider_id="grok",
            conversation_task_provider_id="gpt",
            conversation_task_fallback_provider_id="gpt-fallback",
            context=SimpleNamespace(get_provider_by_id=self.providers.get),
        )
        self.event = SimpleNamespace()

    def request(self, kind):
        decision = CapabilityDecision(
            True, "text", "neutral", (), False, "test", route=kind
        )
        self.event._private_companion_capability_decision = decision
        req = ProviderRequest(
            prompt="用户消息", system_prompt="当前人格：小七", model="旧模型"
        )
        route.configure_conversation_request(self.plugin, self.event, req)
        return req

    def test_chat_uses_chat_whitelist_and_removes_work_tools(self):
        req = self.request("chat")
        self.assertEqual("gemini", req.provider_id)
        self.assertEqual(["grok"], req.fallback_provider_ids)
        self.assertIsNone(req.model)
        self.assertIsNone(req.func_tool)

    def test_chat_keeps_enabled_knowledge_search_without_granting_work_tools(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        from astrbot.core.tools.knowledge_base_tools import KnowledgeBaseQueryTool

        knowledge = KnowledgeBaseQueryTool()
        tools = ToolSet()
        tools.add_tool(knowledge)
        tools.add_tool(FunctionTool(
            name="pc_task_run_command", description="Run a task command",
            parameters={"type": "object", "properties": {}},
        ))
        req = ProviderRequest(
            prompt="这话是在玩谐音梗吗", system_prompt="当前人格：小七", func_tool=tools
        )
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "test", route="chat"
        )

        route.configure_conversation_request(self.plugin, self.event, req)

        self.assertIs(knowledge, req.func_tool.get_tool("astr_kb_search"))
        self.assertIsNone(req.func_tool.get_tool("pc_task_run_command"))
        self.assertTrue(req.system_prompt.startswith("当前人格：小七"))
        self.assertIn("astr_kb_search", req.system_prompt)
        self.assertIn("谐音", req.system_prompt)
        self.assertIn("网络用语", req.system_prompt)
        self.assertEqual((), req.required_tool_names)

    def test_chat_does_not_restore_disabled_or_missing_knowledge_search(self):
        from astrbot.core.agent.tool import ToolSet
        from astrbot.core.tools.knowledge_base_tools import KnowledgeBaseQueryTool

        for present in (False, True):
            with self.subTest(present=present):
                tools = ToolSet()
                if present:
                    knowledge = KnowledgeBaseQueryTool()
                    knowledge.active = False
                    tools.add_tool(knowledge)
                req = ProviderRequest(prompt="你好", func_tool=tools)
                self.event._private_companion_capability_decision = CapabilityDecision(
                    True, "text", "neutral", (), False, "test", route="chat"
                )

                route.configure_conversation_request(self.plugin, self.event, req)

                self.assertIsNone(req.func_tool)
                self.assertNotIn("astr_kb_search", req.system_prompt)

    def test_route_diagnostics_use_astrbot_logger(self):
        with self.assertLogs("astrbot", level="INFO") as captured:
            self.request("task")
        self.assertTrue(any("Companion request route selected" in line for line in captured.output))

    def test_task_uses_gpt_and_keeps_chat_presentation_context(self):
        req = self.request("task")
        self.assertEqual("gpt", req.provider_id)
        self.assertEqual(["gpt-fallback"], req.fallback_provider_ids)
        self.assertTrue(req.disable_streaming)
        self.assertTrue(req.final_response_only)
        self.assertEqual(
            "当前人格：小七", self.event._private_companion_chat_system_prompt
        )

    def test_schedule_task_keeps_future_task_and_user_selected_task_provider(self):
        decision = CapabilityDecision(
            True, "text", "neutral", ("schedule",), False, "test", route="task"
        )
        self.event._private_companion_capability_decision = decision
        req = ProviderRequest(
            prompt="明天早上7:30提醒我起床", system_prompt="当前人格：小七", model="旧模型"
        )
        from astrbot.core.agent.tool import ToolSet

        req.func_tool = ToolSet()
        schedule_tool = MagicMock()
        schedule_tool.name = "future_task"
        req.func_tool.add_tool(schedule_tool)
        route.configure_conversation_request(self.plugin, self.event, req)
        self.assertEqual("gpt", req.provider_id)
        self.assertIn(schedule_tool, req.func_tool.tools)
        self.assertEqual("task", self.event._private_companion_conversation_route)
        self.assertEqual((), req.required_tool_names)

    def test_task_accepts_user_selected_non_gpt_provider(self):
        self.plugin.conversation_task_provider_id = "kimi"
        self.plugin.conversation_task_fallback_provider_id = "gemini"
        req = self.request("task")
        self.assertEqual("kimi", req.provider_id)
        self.assertEqual(["gemini"], req.fallback_provider_ids)

    def test_forbidden_chat_family_rejected_in_primary_and_backup(self):
        for field in (
            "conversation_chat_provider_id",
            "conversation_chat_fallback_provider_id",
        ):
            for identity in ("gpt", "claude"):
                with self.subTest(field=field, identity=identity):
                    setattr(self.plugin, field, identity)
                    with self.assertRaises(ValueError):
                        self.request("chat")
            setattr(
                self.plugin,
                field,
                "gemini" if field.endswith("chat_provider_id") else "grok",
            )

    def test_deepseek_chat_backup_does_not_block_primary_or_task_route(self):
        self.plugin.conversation_chat_fallback_provider_id = "deepseek"
        for kind in ("chat", "task", "mixed"):
            with self.subTest(route=kind):
                req = self.request(kind)
                self.assertEqual(["gemini", "deepseek"], self.event._private_companion_chat_provider_ids)
                self.assertEqual("gemini" if kind == "chat" else "gpt", req.provider_id)
                self.assertEqual(["deepseek"] if kind == "chat" else ["gpt-fallback"], req.fallback_provider_ids)

    def test_missing_api_configuration_does_not_fall_through_to_gpt(self):
        self.plugin.conversation_chat_provider_id = ""
        with self.assertRaises(ValueError):
            self.request("chat")

    def test_unavailable_hub_does_not_silently_route_work_to_chat(self):
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "hub_unavailable"
        )
        with self.assertRaises(ValueError):
            route.configure_conversation_request(
                self.plugin, self.event, ProviderRequest()
            )

    def test_disabled_feature_preserves_original_request(self):
        self.plugin.enable_conversation_model_routing = False
        req = self.request("chat")
        self.assertIsNone(req.provider_id)
        self.assertIsNone(req.fallback_provider_ids)

    def test_unavailable_hub_uses_chat_only_after_host_identity_check(self):
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "hub_unavailable"
        )
        self.event._private_companion_persistent_hub_fallback = True
        req = ProviderRequest()
        route.configure_conversation_request(self.plugin, self.event, req)
        self.assertEqual("gemini", req.provider_id)
        self.assertIsNone(req.func_tool)
        self.assertEqual((), req.required_tool_names)

    def test_rejudged_task_is_applied_to_current_request(self):
        self.request("chat")
        req = self.request("mixed")
        self.assertEqual("gpt", req.provider_id)
        self.assertEqual("mixed", self.event._private_companion_conversation_route)

    def test_task_fallback_can_use_any_enabled_provider(self):
        self.plugin.conversation_task_fallback_provider_id = "gemini"
        req = self.request("task")
        self.assertEqual(["gemini"], req.fallback_provider_ids)


class TaskPresentationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gemini = fake_provider("gemini")
        self.grok = fake_provider("grok")
        self.gemini.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="给你看，别再说我没认真。"
        )
        self.grok.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="结果在这儿。"
        )
        self.plugin = SimpleNamespace(
            enable_conversation_model_routing=True,
            context=SimpleNamespace(
                get_provider_by_id={"gemini": self.gemini, "grok": self.grok}.get
            ),
        )
        self.event = SimpleNamespace(
            _private_companion_conversation_route="task",
            _private_companion_chat_provider_ids=["gemini", "grok"],
            _private_companion_chat_system_prompt="小七人格和短期状态",
            message_str="帮我算数并写代码",
            unified_msg_origin="test:private:u1",
        )

    async def test_task_code_numbers_and_failure_are_preserved_exactly(self):
        from astrbot.core.message.message_event_result import MessageChain

        original = (
            "结果：12.3400\n```python\nprint(1 + 2)\n```\n工具返回：Permission denied"
        )
        resp = LLMResponse(
            role="assistant",
            completion_text=original,
            result_chain=MessageChain().message(original),
        )
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertEqual(
            "给你看，别再说我没认真。\n\n" + original, resp.completion_text
        )
        self.assertEqual(resp.completion_text, resp.result_chain.get_plain_text())
        self.assertIsNone(self.gemini.text_chat.await_args.kwargs.get("func_tool"))
        self.assertIn(
            "小七人格和短期状态",
            self.gemini.text_chat.await_args.kwargs["system_prompt"],
        )
        await route.present_task_response(self.plugin, self.event, resp)
        self.gemini.text_chat.assert_awaited_once()

    async def test_only_chat_backup_is_used_after_primary_failure(self):
        self.gemini.text_chat.side_effect = RuntimeError("offline")
        resp = LLMResponse(role="assistant", completion_text="任务未完成：网络失败")
        await route.present_task_response(self.plugin, self.event, resp)
        self.grok.text_chat.assert_awaited_once()
        self.assertTrue(resp.completion_text.endswith("任务未完成：网络失败"))

    async def test_deepseek_backup_presents_result_after_primary_failure(self):
        deepseek = fake_provider("deepseek/deepseek-flash")
        deepseek.text_chat.return_value = LLMResponse(role="assistant", completion_text="结果在这儿。")
        self.plugin.context.get_provider_by_id = {"gemini": self.gemini, "deepseek/deepseek-flash": deepseek}.get
        self.event._private_companion_chat_provider_ids = ["gemini", "deepseek/deepseek-flash"]
        self.gemini.text_chat.side_effect = TimeoutError("primary timeout")
        resp = LLMResponse(role="assistant", completion_text="原始任务结果")
        await route.present_task_response(self.plugin, self.event, resp)
        deepseek.text_chat.assert_awaited_once()
        self.assertEqual("结果在这儿。\n\n原始任务结果", resp.completion_text)

    async def test_both_chat_failures_keep_result_with_honest_fixed_message(self):
        self.gemini.text_chat.side_effect = RuntimeError("offline")
        self.grok.text_chat.side_effect = RuntimeError("offline")
        resp = LLMResponse(role="assistant", completion_text="任务失败：没有权限")
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertIn("回复整理暂时失败", resp.completion_text)
        self.assertTrue(resp.completion_text.endswith("任务失败：没有权限"))
        self.assertNotIn("已完成", resp.completion_text)

    async def test_registry_failure_preserves_result_and_clears_stored_reasoning(self):
        self.plugin.context.get_provider_by_id = MagicMock(
            side_effect=RuntimeError("registry unavailable")
        )
        self.event.set_extra = MagicMock()
        resp = LLMResponse(
            role="assistant",
            completion_text="任务失败：没有权限",
            reasoning_content="内部推理",
        )
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertTrue(resp.completion_text.endswith("任务失败：没有权限"))
        self.assertIn("回复整理暂时失败", resp.completion_text)
        self.assertIsNone(resp.reasoning_content)
        self.event.set_extra.assert_called_with("_llm_reasoning_content", None)

    async def test_task_presentation_rechecks_core_blocked_endpoint(self):
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
            decoded_blocked,
        )

        self.gemini.provider_config["api_base"] = "https://" + decoded_blocked[0]
        resp = LLMResponse(role="assistant", completion_text="原结果")
        await route.present_task_response(self.plugin, self.event, resp)
        self.gemini.text_chat.assert_not_awaited()
        self.grok.text_chat.assert_awaited_once()

    async def test_chat_and_tool_intermediates_are_not_rephrased(self):
        for kind, tools in (("chat", []), ("task", ["search"])):
            self.event._private_companion_conversation_route = kind
            resp = LLMResponse(
                role="assistant", completion_text="原话", tools_call_name=tools
            )
            await route.present_task_response(self.plugin, self.event, resp)
            self.assertEqual("原话", resp.completion_text)
        self.gemini.text_chat.assert_not_awaited()

    async def test_generated_numeric_claim_is_rejected(self):
        self.gemini.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="已经成功发送了 10 条消息。"
        )
        resp = LLMResponse(role="assistant", completion_text="发送失败")
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertNotIn("10", resp.completion_text)
        self.assertTrue(resp.completion_text.endswith("发送失败"))

    async def test_task_ack_uses_chat_model_and_only_sends_once(self):
        from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore

        self.plugin._conversation_task_states = ConversationTaskStateStore()
        state = self.plugin._conversation_task_states.start_task(
            session_key="test:private:u1", task_text="分析项目", provider_id="gpt", estimated_seconds=90
        )
        self.event._private_companion_task_id = state.task_id
        self.event.send = AsyncMock()
        self.event.plain_result = lambda text: text
        self.gemini.text_chat.return_value = LLMResponse(role="assistant", completion_text="我先去处理一下，别催。")
        await route.send_task_ack(self.plugin, self.event)
        await route.send_task_ack(self.plugin, self.event)
        self.assertEqual(1, self.event.send.await_count)
        self.assertIn("处理", self.event.send.await_args.args[0])

    async def test_task_failure_keeps_unfinished_state_and_uses_chat_model(self):
        from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore

        self.plugin._conversation_task_states = ConversationTaskStateStore()
        state = self.plugin._conversation_task_states.start_task(
            session_key="test:private:u1", task_text="继续分析项目", provider_id="gpt"
        )
        self.event._private_companion_task_id = state.task_id
        self.event.send = AsyncMock()
        self.event.plain_result = lambda text: text
        await route.send_task_failure(self.plugin, self.event, "E_LLM_ALL_PROVIDERS_FAILED")
        saved = self.plugin._conversation_task_states.get(state.task_id)
        self.assertEqual("failed", saved.status)
        self.assertIn("分析项目", saved.unfinished_task)
        self.assertEqual(1, self.event.send.await_count)


class TaskNoticeContextTests(unittest.IsolatedAsyncioTestCase):
    setUp = TaskPresentationTests.setUp

    async def test_deepseek_backup_reports_progress_after_primary_failure(self):
        self.prepare_task("帮我查询资料")
        deepseek = fake_provider("deepseek/deepseek-flash")
        deepseek.text_chat.return_value = LLMResponse(role="assistant", completion_text="还在等任务模型返回。")
        self.plugin.context.get_provider_by_id = {"gemini": self.gemini, "deepseek/deepseek-flash": deepseek}.get
        self.event._private_companion_chat_provider_ids = ["gemini", "deepseek/deepseek-flash"]
        self.gemini.text_chat.side_effect = TimeoutError("primary timeout")
        await route._send_task_notice(self.plugin, self.event, "progress")
        deepseek.text_chat.assert_awaited_once()
        self.event.send.assert_awaited_once_with("还在等任务模型返回。")

    def prepare_task(self, text="后端还是有问题，再写一个测试，我观察后端"):
        from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore

        self.plugin._conversation_task_states = ConversationTaskStateStore()
        state = self.plugin._conversation_task_states.start_task(
            session_key=self.event.unified_msg_origin,
            task_text=text, provider_id="user-selected-task-model",
        )
        self.event._private_companion_task_id = state.task_id
        self.event.message_str = text
        self.event.send = AsyncMock()
        self.event.plain_result = lambda value: value
        self.event._private_companion_conversation_request = ProviderRequest(
            prompt=text, contexts=[
                {"role": "user", "content": "我在检查机器人后端"},
                {"role": "assistant", "content": "那我补测试，你观察后端。"},
            ],
        )
        return state

    async def test_schedule_without_tool_receipt_stays_failed_and_unfinished(self):
        state = self.prepare_task("明天早上7:30提醒我起床")
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", ("schedule",), False, "test", route="task"
        )
        resp = LLMResponse(role="assistant", completion_text="Processing completed successfully.")
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertEqual("failed", state.status)
        self.assertIn("7:30", state.unfinished_task)
        self.assertIn("没有取得定时工具的成功回执", resp.completion_text)
        self.assertNotIn("Processing completed successfully", resp.completion_text)
        self.gemini.text_chat.assert_not_awaited()

    async def test_task_timeout_notice_rejects_chat_model_misattribution(self):
        self.prepare_task("帮我查询资料")
        self.gemini.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="聊天模型暂时不可用，任务没能完成。"
        )
        self.grok.text_chat.return_value = self.gemini.text_chat.return_value
        await route.send_task_failure(self.plugin, self.event, "APITimeoutError: Request timed out")
        sent = self.event.send.await_args.args[0]
        self.assertIn("后台任务模型请求超时", sent)
        self.assertNotIn("聊天模型", sent)

    async def test_task_timeout_notice_preserves_normalized_core_timeout(self):
        self.prepare_task("帮我查询资料")
        self.gemini.text_chat.return_value = LLMResponse(
            role="assistant", completion_text="后台执行出了点问题，任务还没完成。"
        )
        self.grok.text_chat.return_value = self.gemini.text_chat.return_value
        await route.send_task_failure(
            self.plugin, self.event,
            "[E_LLM_ALL_PROVIDERS_FAILED] 后台任务模型请求超时，本次任务未完成。",
        )
        sent = self.event.send.await_args.args[0]
        self.assertIn("后台任务模型请求超时", sent)

    async def test_schedule_with_real_receipt_keeps_success(self):
        state = self.prepare_task("明天提醒我起床")
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", ("schedule",), False, "test", route="task"
        )
        self.event.private_companion_future_task_succeeded = True
        resp = LLMResponse(role="assistant", completion_text="提醒已登记，job_id=real-id")
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertEqual("complete", state.status)
        self.assertIn("real-id", resp.completion_text)

    async def test_schedule_list_receipt_does_not_require_mutation(self):
        state = self.prepare_task("查查我的定时任务")
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", ("schedule",), False, "test", route="task"
        )
        self.event.private_companion_future_task_result_observed = True
        self.event.private_companion_future_task_action = "list"
        resp = LLMResponse(role="assistant", completion_text="No cron jobs found.")
        await route.present_task_response(self.plugin, self.event, resp)
        self.assertEqual("complete", state.status)

    async def test_all_notices_strip_internal_markers(self):
        for kind in ("ack", "progress", "failure"):
            with self.subTest(kind=kind):
                self.prepare_task()
                self.gemini.text_chat.return_value = LLMResponse(
                    role="assistant",
                    completion_text="<think>不能发出的内部内容</think>我还在处理。 &&work&& [[PCTTS:secret]]",
                )
                if kind == "failure":
                    await route.send_task_failure(self.plugin, self.event, "网络异常")
                else:
                    await getattr(route, "send_task_" + kind)(self.plugin, self.event)
                sent = self.event.send.await_args.args[0]
                self.assertNotIn("&&", sent)
                self.assertNotIn("PCTTS", sent)
                self.assertNotIn("内部内容", sent)

    async def test_progress_receives_recent_context_and_sent_ack(self):
        import json

        self.prepare_task()
        self.gemini.text_chat.return_value = LLMResponse(role="assistant", completion_text="我来补测试，你先看后端。")
        await route.send_task_ack(self.plugin, self.event)
        await route.send_task_progress(self.plugin, self.event)
        args = self.gemini.text_chat.await_args.kwargs
        payload = json.loads(args["prompt"])
        self.assertEqual(self.event.message_str, payload["user_message"])
        self.assertIn("我来补测试，你先看后端。", str(payload["previous_replies"]))
        self.assertIn("我在检查机器人后端", str(args["contexts"]))

    async def test_progress_reads_the_requested_task_not_newest_task(self):
        import json

        state = self.prepare_task("补写后端测试")
        self.plugin._conversation_task_states.start_task(
            session_key=self.event.unified_msg_origin,
            task_text="另一个无关任务", provider_id="other-model",
        )
        await route.send_task_progress(self.plugin, self.event)
        payload = json.loads(self.gemini.text_chat.await_args.kwargs["prompt"])
        self.assertEqual(state.task_id, payload["task"]["task_id"])
        self.assertNotIn("另一个无关任务", str(payload))

    async def test_wait_notice_does_not_invent_execution_progress(self):
        state = self.prepare_task()
        await route.send_task_progress(self.plugin, self.event)
        self.assertEqual("", state.progress_note)
        self.assertEqual("running", state.status)

    async def test_progress_is_discarded_if_task_finishes_during_generation(self):
        state = self.prepare_task()

        async def complete(**kwargs):
            self.plugin._conversation_task_states.complete_task(state.task_id, "测试结果")
            return LLMResponse(role="assistant", completion_text="还在等待结果。")

        self.gemini.text_chat.side_effect = complete
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_not_awaited()

    async def test_notice_never_reads_another_session_task(self):
        self.prepare_task()
        self.event.unified_msg_origin = "test:private:other-user"
        await route.send_task_ack(self.plugin, self.event)
        self.event.send.assert_not_awaited()
        self.gemini.text_chat.assert_not_awaited()

    async def test_final_opening_cleans_markers_and_preserves_literal_code(self):
        self.prepare_task()
        self.gemini.text_chat.return_value = LLMResponse(role="assistant", completion_text="给你看。 &&work&&")
        original = "结果：12.3400\n```python\nprint('&&work&&')\n```"
        response = LLMResponse(role="assistant", completion_text=original)
        await route.present_task_response(self.plugin, self.event, response)
        self.assertEqual("给你看。\n\n" + original, response.completion_text)

    async def test_concurrent_acknowledgements_only_deliver_once(self):
        import asyncio

        self.prepare_task()
        await asyncio.gather(route.send_task_ack(self.plugin, self.event), route.send_task_ack(self.plugin, self.event))
        self.event.send.assert_awaited_once()
        self.gemini.text_chat.assert_awaited_once()

    async def test_task_finishing_before_90_seconds_skips_progress_query(self):
        for status in ("complete", "failed", "timeout"):
            with self.subTest(status=status):
                state = self.prepare_task()
                self.gemini.text_chat.reset_mock()
                self.grok.text_chat.reset_mock()

                async def finish_during_wait(seconds):
                    self.assertEqual(90.0, seconds)
                    store = self.plugin._conversation_task_states
                    if status == "complete":
                        store.complete_task(state.task_id, "真实结果")
                    elif status == "failed":
                        store.fail_task(state.task_id, "任务已取消或失败")
                    else:
                        store.timeout_task(state.task_id, "请求超时")

                with patch.object(route.asyncio, "sleep", side_effect=finish_during_wait):
                    await route.delayed_task_progress(self.plugin, self.event, 90.0)
                self.event.send.assert_not_awaited()
                self.gemini.text_chat.assert_not_awaited()
                self.grok.text_chat.assert_not_awaited()

    async def test_task_finishing_during_progress_query_drops_late_notice(self):
        state = self.prepare_task()

        async def finish_while_generating(**kwargs):
            self.plugin._conversation_task_states.complete_task(state.task_id, "真实结果")
            return LLMResponse(role="assistant", completion_text="还在等后台结果。")

        self.gemini.text_chat.side_effect = finish_while_generating
        with patch.object(route.asyncio, "sleep", new_callable=AsyncMock):
            await route.delayed_task_progress(self.plugin, self.event, 90.0)
        self.event.send.assert_not_awaited()

    async def test_failed_delivery_is_retryable_and_not_added_to_history(self):
        self.prepare_task()
        self.event.send.side_effect = RuntimeError("platform offline")
        with self.assertRaises(RuntimeError):
            await route.send_task_ack(self.plugin, self.event)
        self.assertFalse(getattr(self.event, "_private_companion_task_ack_sent", False))
        self.assertFalse(getattr(self.event, "_private_companion_task_notice_history", []))
        self.event.send.side_effect = None
        await route.send_task_ack(self.plugin, self.event)
        self.assertTrue(self.event._private_companion_task_ack_sent)

    async def test_unstarted_task_cannot_claim_test_is_running(self):
        self.prepare_task()
        self.gemini.text_chat.return_value = LLMResponse(role="assistant", completion_text="测试还在跑着呢，等后端稳了我再看。")
        self.grok.text_chat.return_value = LLMResponse(role="assistant", completion_text="测试已经运行了百分之八十。")
        await route.send_task_progress(self.plugin, self.event)
        self.assertIn("结果", self.event.send.await_args.args[0])
        self.assertNotIn("测试还在跑", self.event.send.await_args.args[0])

    async def test_failure_notice_never_accepts_a_success_claim(self):
        self.prepare_task()
        for provider in (self.gemini, self.grok):
            provider.text_chat.return_value = LLMResponse(role="assistant", completion_text="任务已经全部完成了。")
        await route.send_task_failure(self.plugin, self.event, "真实失败")
        self.assertIn("没能完成", self.event.send.await_args.args[0])

    async def test_final_result_control_cleanup_preserves_code_and_numbers(self):
        self.prepare_task()
        original = "结果12.3400。 &&work&&\n```python\nprint('&&work&&')\n```"
        response = LLMResponse(role="assistant", completion_text=original)
        await route.present_task_response(self.plugin, self.event, response)
        self.assertNotIn("结果12.3400。 &&work&&", response.completion_text)
        self.assertIn("print('&&work&&')", response.completion_text)


class ConversationHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_failure_notices_are_bounded_per_bot_session_and_recover(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = SimpleNamespace(enabled=True, enable_conversation_model_routing=True)
        def event(session):
            return SimpleNamespace(
                unified_msg_origin=session,
                _private_companion_capability_decision=SimpleNamespace(reply=True),
                stop_event=MagicMock(), send=AsyncMock(), plain_result=lambda text: text,
            )

        first, repeated, other = event("a:GroupMessage:g1"), event("a:GroupMessage:g1"), event("b:GroupMessage:g1")
        hook = PrivateCompanionPlugin.route_conversation_models.__wrapped__
        with patch("astrbot_plugin_private_companion.main.configure_conversation_request", side_effect=ValueError("语境中枢暂时不可用")):
            await hook(plugin, first, ProviderRequest())
            await hook(plugin, repeated, ProviderRequest())
            await hook(plugin, other, ProviderRequest())
        first.send.assert_awaited_once()
        repeated.send.assert_not_awaited()
        repeated.stop_event.assert_called_once()
        other.send.assert_awaited_once()
        with patch("astrbot_plugin_private_companion.main.configure_conversation_request"):
            await hook(plugin, event("a:GroupMessage:g1"), ProviderRequest())
        recovered = event("a:GroupMessage:g1")
        with patch("astrbot_plugin_private_companion.main.configure_conversation_request", side_effect=ValueError("语境中枢暂时不可用")):
            await hook(plugin, recovered, ProviderRequest())
        recovered.send.assert_awaited_once()

    async def test_route_failure_notice_retries_after_cooldown_or_failed_delivery(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = SimpleNamespace(enabled=True, enable_conversation_model_routing=True)
        event = SimpleNamespace(
            unified_msg_origin="a:FriendMessage:u1",
            _private_companion_capability_decision=SimpleNamespace(reply=True),
            stop_event=MagicMock(), send=AsyncMock(side_effect=[False, None, None]),
            plain_result=lambda text: text,
        )
        hook = PrivateCompanionPlugin.route_conversation_models.__wrapped__
        with (
            patch("astrbot_plugin_private_companion.main.configure_conversation_request", side_effect=ValueError("中枢不可用")),
            patch("astrbot_plugin_private_companion.main.time.monotonic", side_effect=[1000, 1001, 1002, 1301]),
        ):
            for _ in range(4):
                await hook(plugin, event, ProviderRequest())
        self.assertEqual(3, event.send.await_count)

    async def test_progress_query_waits_90_seconds_regardless_of_hub_estimate(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = SimpleNamespace(
            enabled=True, enable_conversation_model_routing=True,
            _create_lifecycle_background_task=lambda coro, **kwargs: coro.close(),
        )
        for estimate in (0, 5, 10, 30, 60, 90, 120, 300):
            with self.subTest(estimate=estimate):
                event = SimpleNamespace(
                    _private_companion_capability_decision=SimpleNamespace(reply=True),
                    _private_companion_task_id="test-task",
                    _private_companion_task_estimated_seconds=estimate,
                )
                with (
                    patch("astrbot_plugin_private_companion.main.configure_conversation_request"),
                    patch("astrbot_plugin_private_companion.main.send_task_ack", new_callable=AsyncMock),
                    patch("astrbot_plugin_private_companion.main.delayed_task_progress", new_callable=AsyncMock) as progress,
                ):
                    await PrivateCompanionPlugin.route_conversation_models.__wrapped__(plugin, event, ProviderRequest())
                    progress.assert_called_once_with(plugin, event, 90.0)

    async def test_progress_timer_queries_only_after_90_seconds(self):
        waiting = asyncio.Event()
        elapsed = asyncio.Event()

        async def wait_for_threshold(seconds):
            self.assertEqual(90.0, seconds)
            waiting.set()
            await elapsed.wait()

        with (
            patch.object(route.asyncio, "sleep", side_effect=wait_for_threshold),
            patch.object(route, "send_task_progress", new_callable=AsyncMock) as progress,
        ):
            timer = asyncio.create_task(route.delayed_task_progress("plugin", "event", 90.0))
            await asyncio.wait_for(waiting.wait(), 1)
            progress.assert_not_awaited()
            elapsed.set()
            await asyncio.wait_for(timer, 1)
            progress.assert_awaited_once_with("plugin", "event")

    async def test_request_hook_keeps_task_classification_in_gemini_without_legacy_ack(self):
        from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        chat = fake_provider("gemini")
        chat.text_chat.return_value = LLMResponse(role="assistant", completion_text="我先处理，等下回来。")
        task = fake_provider("task-model")
        pending = []
        plugin = SimpleNamespace(
            enabled=True,
            enable_conversation_model_routing=True,
            conversation_chat_provider_id="gemini",
            conversation_chat_fallback_provider_id="",
            conversation_task_provider_id="task-model",
            context=SimpleNamespace(get_provider_by_id={"gemini": chat, "task-model": task}.get),
            _conversation_task_states=ConversationTaskStateStore(),
            _create_lifecycle_background_task=lambda coro, **kwargs: pending.append(coro),
        )
        event = SimpleNamespace(
            unified_msg_origin="test:session",
            message_str="写一个网页版贪吃蛇",
            _private_companion_capability_decision=CapabilityDecision(
                True, "text", "neutral", (), False, "test", route="task", estimated_seconds=90,
            ),
            send=AsyncMock(), plain_result=lambda text: text, stop_event=MagicMock(),
        )
        try:
            req = ProviderRequest(prompt=event.message_str)
            await PrivateCompanionPlugin.route_conversation_models.__wrapped__(plugin, event, req)
            self.assertEqual("gemini", req.provider_id)
            event.send.assert_not_awaited()
            self.assertEqual(0, len(pending))
            task.text_chat.assert_not_awaited()
            event.stop_event.assert_not_called()
        finally:
            for coro in pending:
                coro.close()

    async def test_framework_error_hook_notifies_and_retains_failed_task(self):
        from astrbot.api.message_components import Plain
        from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.enabled = True
        plugin.enable_framework_error_leak_guard = True
        plugin._record_passive_no_reply = MagicMock()
        plugin._restore_response_review_meta_leak_before_send = MagicMock(return_value=False)
        plugin._conversation_task_states = ConversationTaskStateStore()
        state = plugin._conversation_task_states.start_task(
            session_key="test:session", task_text="写网页版贪吃蛇", provider_id="task-model",
        )
        result = SimpleNamespace(chain=[Plain("Provider API Error: upstream timeout")])
        event = SimpleNamespace(
            unified_msg_origin="test:session", _private_companion_task_id=state.task_id,
            get_result=lambda: result, set_result=MagicMock(), stop_event=MagicMock(),
            send=AsyncMock(), plain_result=lambda text: text,
        )
        await PrivateCompanionPlugin.suppress_framework_error_leak_before_send.__wrapped__(plugin, event)
        self.assertEqual("failed", plugin._conversation_task_states.get(state.task_id).status)
        self.assertIn("贪吃蛇", plugin._conversation_task_states.get(state.task_id).unfinished_task)
        event.send.assert_awaited_once()
        event.stop_event.assert_called_once()

    async def test_merged_work_request_is_judged_before_search_tools_are_removed(self):
        from unittest.mock import patch

        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
            gate_request_search_tools,
        )

        for private in (True, False):
            with self.subTest(private=private):
                tools = SimpleNamespace(
                    names=lambda: ["web_search_test"],
                    tools=[SimpleNamespace(name="web_search_test")],
                    remove_tool=MagicMock(),
                )
                first = CapabilityDecision(
                    True, "text", "neutral", (), False, "first", route="chat"
                )
                merged = CapabilityDecision(
                    True, "text", "neutral", ("search",), False, "merged", route="task"
                )
                event = SimpleNamespace(
                    unified_msg_origin="test:session",
                    message_str="在吗",
                    is_private_chat=lambda: private,
                    get_sender_id=lambda: "u1",
                    get_sender_name=lambda: "甲",
                    _private_companion_capability_decision=first,
                )
                consumer = AsyncMock(return_value="在吗\n再帮我搜索资料并写代码")
                plugin = SimpleNamespace(
                    enable_conversation_model_routing=True,
                    _consume_semantic_message_buffer_for_event=consumer,
                )
                req = ProviderRequest(prompt="在吗", func_tool=tools)
                judge = AsyncMock(return_value=merged)
                with patch(
                    "astrbot_plugin_private_companion.passive_state_pipeline.judge_capabilities",
                    judge,
                ):
                    await append_capability_guidance(plugin, event, req)
                    gate_request_search_tools(req)
                    await append_capability_guidance(plugin, event, req)
                self.assertEqual(
                    "task", event._private_companion_capability_decision.route
                )
                self.assertIn("搜索资料", judge.await_args.args[1].current_text)
                self.assertIn("搜索资料", req.prompt)
                tools.remove_tool.assert_not_called()
                consumer.assert_awaited_once()

    async def test_request_hook_stops_missing_chat_configuration(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        plugin = SimpleNamespace(
            enabled=True,
            enable_conversation_model_routing=True,
            conversation_chat_provider_id="",
        )
        decision = CapabilityDecision(True, "text", "neutral", (), False, "test")
        event = SimpleNamespace(
            unified_msg_origin="default:FriendMessage:u1",
            _private_companion_capability_decision=decision,
            send=AsyncMock(),
            plain_result=lambda text: text,
            stop_event=MagicMock(),
        )
        req = ProviderRequest(prompt="hello")
        await PrivateCompanionPlugin.route_conversation_models.__wrapped__(
            plugin, event, req
        )
        event.stop_event.assert_called_once()
        event.send.assert_awaited_once()
        self.assertIsNone(req.provider_id)

    async def test_task_history_records_presented_reply(self):
        from astrbot.core.agent.message import Message
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        event = SimpleNamespace(
            _private_companion_presented_task_text="小七承接\n\n结果"
        )
        ctx = SimpleNamespace(messages=[Message(role="assistant", content="结果")])
        await PrivateCompanionPlugin.sync_conversation_task_history.__wrapped__(
            SimpleNamespace(),
            event,
            ctx,
            LLMResponse(role="assistant", completion_text="小七承接\n\n结果"),
        )
        self.assertEqual("小七承接\n\n结果", ctx.messages[-1].content[0].text)
