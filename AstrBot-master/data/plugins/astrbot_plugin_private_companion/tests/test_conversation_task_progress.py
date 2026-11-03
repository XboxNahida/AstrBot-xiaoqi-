import asyncio
import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from dataclasses import replace

from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_private_companion import conversation_route as route
from astrbot_plugin_private_companion.conversation_task_state import ConversationTaskStateStore
from .test_conversation_route import fake_provider


class CancelledTaskStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_persistent_request_survives_cache_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationTaskStateStore(max_tasks=1, db_path=Path(directory) / "tasks.db")
            plugin = SimpleNamespace(_conversation_task_states=store)
            event = SimpleNamespace(message_str="补写测试", unified_msg_origin="bot:GroupMessage:g", get_sender_id=lambda: "u")
            request = SimpleNamespace(provider_id="chosen-model", fallback_provider_ids=[])
            decision = SimpleNamespace(route="task", segments=(), estimated_seconds=120)
            ready = asyncio.Event()

            async def worker():
                route.register_conversation_task(plugin, event, request, decision)
                ready.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(worker())
            await asyncio.wait_for(ready.wait(), 1)
            store.start_task(session_key="other", task_text="另一个任务", provider_id="chosen")
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            saved = ConversationTaskStateStore(db_path=Path(directory) / "tasks.db").get(event._private_companion_task_id)
            self.assertEqual("failed", saved.status)

    async def test_cancelled_request_keeps_model_and_unfinished_work(self):
        store = ConversationTaskStateStore()
        plugin = SimpleNamespace(_conversation_task_states=store)
        event = SimpleNamespace(message_str="补写测试", unified_msg_origin="bot:GroupMessage:g", get_sender_id=lambda: "u")
        request = SimpleNamespace(provider_id="chosen-model", fallback_provider_ids=["backup"])
        decision = SimpleNamespace(route="task", segments=(), estimated_seconds=120)
        started = asyncio.Event()

        async def worker():
            route.register_conversation_task(plugin, event, request, decision)
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        state = store.get(event._private_companion_task_id)
        self.assertEqual("failed", state.status)
        self.assertEqual("chosen-model", state.provider_id)
        self.assertEqual("backup", state.fallback_provider_id)
        self.assertEqual("补写测试", state.unfinished_task)
        self.assertIn("取消", state.error)


class TaskProgressStateTests(unittest.TestCase):
    def test_finished_task_cannot_be_revived_by_delayed_progress(self):
        store = ConversationTaskStateStore()
        task = store.start_task(session_key="s", task_text="测试", provider_id="chosen")
        store.complete_task(task.task_id, "结果")
        store.mark_progress(task.task_id, "迟到的输出")
        self.assertEqual("complete", task.status)
        self.assertEqual("", task.progress_note)

    def test_tool_phase_requires_actual_tool_evidence(self):
        store = ConversationTaskStateStore()
        task = store.start_task(session_key="s", task_text="测试", provider_id="chosen")
        self.assertEqual("waiting_model", task.phase)
        store.record_tool_start(task.task_id, "astrbot_execute_shell")
        self.assertEqual("tool_running", task.phase)
        store.record_tool_result(task.task_id, "astrbot_execute_shell", "exit code 1", failed=True)
        self.assertEqual("tool_error", task.phase)
        self.assertEqual("progress", task.status)
        self.assertIn("exit code 1", task.tool_result)
        store.record_tool_start(task.task_id, "astrbot_execute_python")
        store.record_tool_result(task.task_id, "astrbot_execute_python", "4 checks passed")
        self.assertEqual("waiting_model", task.phase)
        self.assertTrue(any(item["failed"] for item in task.tool_events))

    def test_same_group_other_sender_has_no_task_access(self):
        store = ConversationTaskStateStore()
        store.start_task(session_key="qq:Group:g", task_text="用户甲的任务", provider_id="chosen", sender_id="a")
        self.assertEqual({}, store.summary_for_session("qq:Group:g", sender_id="b"))
        self.assertIn("甲", store.summary_for_session("qq:Group:g", sender_id="a")["task_text"])


class TaskProgressHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.chat = fake_provider("gemini")
        self.chat.text_chat.return_value = LLMResponse(role="assistant", completion_text="还在等任务模型的结果。")
        self.store = ConversationTaskStateStore()
        self.task = self.store.start_task(session_key="qq:Friend:u1", task_text="补写测试，我观察后端", provider_id="chosen", sender_id="u1")
        self.plugin = SimpleNamespace(enabled=True, enable_conversation_model_routing=True,
            _conversation_task_states=self.store,
            context=SimpleNamespace(get_provider_by_id={"gemini": self.chat}.get))
        self.event = SimpleNamespace(unified_msg_origin="qq:Friend:u1", message_str="现在查询进度",
            _private_companion_task_id=self.task.task_id,
            _private_companion_chat_provider_ids=["gemini"],
            _private_companion_chat_system_prompt="小七：嘴硬但关心对方",
            send=AsyncMock(), plain_result=lambda text: text, stop_event=MagicMock(), get_sender_id=lambda: "u1")

    async def test_agent_error_ends_task_before_late_progress(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        response = LLMResponse(role="err", completion_text="Required tool was not called: future_task")
        await PrivateCompanionPlugin.sync_conversation_task_history.__wrapped__(
            self.plugin, self.event, SimpleNamespace(messages=[]), response
        )
        self.assertEqual("failed", self.task.status)
        self.assertIn("future_task", self.task.error)
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_not_awaited()

    async def test_agent_timeout_records_timeout_and_keeps_unfinished_work(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        response = LLMResponse(role="err", completion_text="APITimeoutError: request timed out")
        await PrivateCompanionPlugin.sync_conversation_task_history.__wrapped__(
            self.plugin, self.event, SimpleNamespace(messages=[]), response
        )
        self.assertEqual("timeout", self.task.status)
        self.assertIn("补写测试", self.task.unfinished_task)

    async def test_recovered_task_query_does_not_claim_still_running(self):
        self.store.recover_interrupted()
        self.event._private_companion_chat_provider_ids = []
        await route._send_task_notice(self.plugin, self.event, "query")
        self.assertIn("核实", self.event.send.await_args.args[0])
        self.assertNotIn("还在等待", self.event.send.await_args.args[0])

    async def test_outbound_failure_preserves_recorded_timeout(self):
        self.store.timeout_task(self.task.task_id, "APITimeoutError: request timed out")
        self.event._private_companion_chat_provider_ids = []
        await route.send_task_failure(self.plugin, self.event, "后台模型不可用")
        self.assertEqual("timeout", self.store.get(self.task.task_id).status)
        self.assertIn("timed out", self.store.get(self.task.task_id).error)

    async def test_persistent_registration_and_notice_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "tasks.db"
            self.plugin._conversation_task_states = ConversationTaskStateStore(db_path=db_path)
            request = SimpleNamespace(provider_id="chosen", fallback_provider_ids=["backup"])
            decision = SimpleNamespace(route="task", segments=(), estimated_seconds=90)
            state = route.register_conversation_task(self.plugin, self.event, request, decision)
            await route.send_task_ack(self.plugin, self.event)
            saved = ConversationTaskStateStore(db_path=db_path).get(state.task_id)
            self.assertEqual(self.event.message_str, saved.reply_context["user_message"])
            self.assertEqual(["gemini"], saved.reply_context["chat_provider_ids"])
            self.assertEqual(1, len(saved.notices))

    async def test_persistent_image_delivery_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "tasks.db"
            store = ConversationTaskStateStore(db_path=db_path)
            self.plugin._conversation_task_states = store
            self.task = store.start_task(session_key="qq:Friend:u1", task_text="图片", provider_id="chosen")
            self.event._private_companion_task_id = self.task.task_id
            self.record_image_delivery()
            saved = ConversationTaskStateStore(db_path=db_path).get(self.task.task_id)
            self.assertTrue(saved.reply_context.get("visible_delivery"))
            self.assertEqual("media_delivered", saved.phase)

    async def test_real_tool_hooks_update_current_task(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

        tool = SimpleNamespace(name="astrbot_execute_shell")
        await PrivateCompanionPlugin.track_conversation_tool_start.__wrapped__(self.plugin, self.event, tool, {})
        self.assertEqual("tool_running", self.task.phase)
        result = SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text='{"status":"running","stdout":"started","session_id":"sh_test"}')])
        await PrivateCompanionPlugin.track_conversation_tool_result.__wrapped__(self.plugin, self.event, tool, {}, result)
        self.assertEqual("background_running", self.task.phase)
        await route.send_task_progress(self.plugin, self.event)
        data = json.loads(self.chat.text_chat.await_args.kwargs["prompt"])
        self.assertIn("started", data["task"]["tool_result"])

    async def test_progress_generation_race_uses_latest_facts(self):
        async def model(**kwargs):
            self.store.record_tool_start(self.task.task_id, "read_file")
            return LLMResponse(role="assistant", completion_text="正在等任务模型，还没开始调用工具。")

        self.chat.text_chat.side_effect = model
        await route.send_task_progress(self.plugin, self.event)
        self.assertNotIn("还没开始调用工具", self.event.send.await_args.args[0])

    def record_image_delivery(self, *, sent=True, deferred=False):
        result = json.dumps({'status': 'success', 'success': True, 'generation_completed': True,
                             'sent': sent, 'delivery': 'current', 'delivery_deferred': deferred})
        route.record_task_tool_event(self.plugin, self.event, SimpleNamespace(name='companion_send_photo'), result)

    async def test_delivered_image_suppresses_automatic_progress(self):
        self.record_image_delivery()
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_not_awaited()
        self.chat.text_chat.assert_not_awaited()
        self.assertNotEqual('complete', self.task.status)

    async def test_delivery_during_notice_generation_drops_stale_notice(self):
        async def model(**kwargs):
            self.record_image_delivery()
            return LLMResponse(role='assistant', completion_text='还在等图片，请稍等。')
        self.chat.text_chat.side_effect = model
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_not_awaited()

    async def test_undelivered_image_keeps_progress(self):
        self.record_image_delivery(sent=False)
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_awaited_once()

    async def test_deferred_delivery_does_not_hide_progress(self):
        self.record_image_delivery(deferred=True)
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_awaited_once()

    async def test_next_real_tool_allows_progress_after_partial_delivery(self):
        self.record_image_delivery()
        route.record_task_tool_event(self.plugin, self.event, SimpleNamespace(name='read_file'), started=True)
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_awaited_once()

    async def test_empty_final_after_delivery_closes_task_without_new_reply(self):
        self.event._private_companion_conversation_route = 'task'
        self.record_image_delivery()
        await route.present_task_response(self.plugin, self.event, LLMResponse(role='assistant', completion_text=''))
        self.assertEqual('complete', self.task.status)
        self.chat.text_chat.assert_not_awaited()

    async def test_error_word_in_successful_result_is_not_a_failed_task(self):
        self.event._private_companion_conversation_route = "task"
        self.chat.text_chat.return_value = LLMResponse(role="assistant", completion_text="结果在这儿。")
        await route.present_task_response(self.plugin, self.event,
            LLMResponse(role="assistant", completion_text="修复了错误，4项测试通过，未发现错误。"))
        self.assertEqual("complete", self.task.status)

    async def test_real_tool_failure_retains_unfinished_work_at_final_reply(self):
        self.event._private_companion_conversation_route = "task"
        self.store.record_tool_result(self.task.task_id, "read_file", "Permission denied", failed=True)
        await route.present_task_response(self.plugin, self.event,
            LLMResponse(role="assistant", completion_text="权限不足，这次未完成。"))
        self.assertEqual("failed", self.task.status)
        self.assertIn("补写测试", self.task.unfinished_task)

    async def test_recovered_tool_failure_can_finish_normally(self):
        self.event._private_companion_conversation_route = "task"
        self.store.record_tool_result(self.task.task_id, "shell", "exit 1", failed=True)
        self.store.record_tool_start(self.task.task_id, "shell")
        self.store.record_tool_result(self.task.task_id, "shell", "4 checks passed")
        await route.present_task_response(self.plugin, self.event,
            LLMResponse(role="assistant", completion_text="4项检查通过。"))
        self.assertEqual("complete", self.task.status)
        self.assertEqual("", self.task.unfinished_task)
        self.assertTrue(self.task.tool_events[0]["failed"])

    async def test_user_interruption_is_not_recorded_as_completion(self):
        self.event._private_companion_conversation_route = "task"
        self.event.get_extra = lambda key, default=None: True if key == "agent_stop_requested" else default
        response = LLMResponse(role="assistant", completion_text="任务已中断。")
        await route.present_task_response(self.plugin, self.event, response)
        self.assertEqual("failed", self.task.status)
        self.assertIn("用户", self.task.error)
        self.assertIn("补写测试", self.task.unfinished_task)
        self.chat.text_chat.assert_not_awaited()
        await route.send_task_progress(self.plugin, self.event)
        self.event.send.assert_not_awaited()

    async def test_failure_notification_cannot_overwrite_a_completed_task(self):
        self.store.complete_task(self.task.task_id, "已验证的结果")
        await route.send_task_failure(self.plugin, self.event, "迟到的上游异常")
        self.assertEqual("complete", self.task.status)
        self.event.send.assert_not_awaited()

    async def test_status_query_is_answered_before_core_followup_capture(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
        from astrbot_plugin_private_companion.capability_hub import CapabilityDecision

        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "query", route="chat", task_query=True)
        self.task.notices = ["我来补测试，你观察后端。"]
        await PrivateCompanionPlugin.reply_conversation_task_query.__wrapped__(self.plugin, self.event)
        self.event.stop_event.assert_called_once()
        self.event.send.assert_awaited_once()
        self.assertEqual(1, len(self.store._tasks))
        data = json.loads(self.chat.text_chat.await_args.kwargs["prompt"])
        self.assertEqual("现在查询进度", data["user_message"])
        self.assertIn("我来补测试", str(data["previous_replies"]))

    async def test_query_cannot_access_other_platform_or_sender(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
        from astrbot_plugin_private_companion.capability_hub import CapabilityDecision

        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "query", route="chat", task_query=True)
        self.event.get_sender_id = lambda: "other"
        await PrivateCompanionPlugin.reply_conversation_task_query.__wrapped__(self.plugin, self.event)
        self.event.send.assert_not_awaited()
        self.event.stop_event.assert_not_called()

    async def test_query_checks_current_conversation_without_emotion_context(self):
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
        from astrbot_plugin_private_companion.capability_hub import CapabilityDecision

        self.task.conversation_id = "old-conversation"
        self.plugin.context.conversation_manager = SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value="new-conversation"))
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "query", route="chat", task_query=True)
        await PrivateCompanionPlugin.reply_conversation_task_query.__wrapped__(self.plugin, self.event)
        self.event.send.assert_not_awaited()
        self.event.stop_event.assert_not_called()

    async def test_query_resolution_failure_does_not_reuse_old_task(self):
        from astrbot_plugin_private_companion.capability_hub import CapabilityDecision

        self.plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(side_effect=RuntimeError("unavailable")))
        self.event._private_companion_capability_decision = CapabilityDecision(
            True, "text", "neutral", (), False, "query", route="chat", task_query=True)
        self.assertFalse(await route.maybe_send_task_query(self.plugin, self.event))
        self.event.send.assert_not_awaited()

    async def test_real_process_stage_stops_query_before_agent_followup(self):
        from astrbot.core.pipeline.process_stage.stage import ProcessStage
        from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
        from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import StarHandlerRegistry, star_handlers_registry
        from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
        from astrbot_plugin_private_companion.capability_hub import CapabilityDecision
        from functools import partial

        async def preflight(event):
            event._private_companion_capability_decision = CapabilityDecision(
                True, "text", "neutral", (), False, "query", route="chat", task_query=True)
            return True

        self.plugin._handle_private_message_preflight = preflight
        registry = StarHandlerRegistry()
        for metadata in star_handlers_registry._handlers:
            if metadata.handler_module_path == PrivateCompanionPlugin.__module__ and metadata.handler_name in {
                "on_private_message", "reply_conversation_task_query"
            }:
                registry.append(replace(metadata, handler=partial(
                    getattr(PrivateCompanionPlugin, metadata.handler_name), self.plugin)))
        self.assertEqual(["on_private_message", "reply_conversation_task_query"],
            [item.handler_name for item in registry._handlers])
        extras = {"activated_handlers": registry._handlers, "handlers_parsed_params": {}}
        self.event.get_extra = lambda key, default=None: extras.get(key, default)
        self.event.is_at_or_wake_command = True
        self.event.call_llm = False
        self.event._has_send_oper = False
        self.event.get_result = lambda: None
        self.event.clear_result = MagicMock()
        self.event.is_stopped = lambda: self.event.stop_event.called
        self.event.message_obj = SimpleNamespace(message=[])

        async def send(text):
            self.event._has_send_oper = True

        self.event.send.side_effect = send
        agent = internal.InternalAgentSubStage()
        agent.streaming_response = False
        stage = ProcessStage()
        stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
        stage.star_request_sub_stage = StarRequestSubStage()
        stage.agent_sub_stage = SimpleNamespace(process=lambda event: agent.process(event, ""))
        with patch.dict(star_map, {PrivateCompanionPlugin.__module__: SimpleNamespace(name="companion")}), \
             patch.object(internal, "try_capture_follow_up") as capture:
            async for _ in stage.process(self.event):
                pass
        capture.assert_not_called()
        self.event.send.assert_awaited_once()
        self.event.stop_event.assert_called_once()

    async def test_hub_and_request_isolate_task_state_with_emotion_disabled(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import append_capability_guidance

        self.task.conversation_id = "old"
        self.plugin.enable_short_term_emotion = False
        self.plugin._task_provider = lambda *args: "hub"
        self.plugin._llm_call = AsyncMock(return_value='{"reply":true,"actions":[],"route":"chat"}')
        self.plugin.context.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="new"))
        self.event.is_private_chat = lambda: True
        self.event.get_sender_name = lambda: "甲"
        req = SimpleNamespace(system_prompt="当前人格", contexts=[])
        await append_capability_guidance(self.plugin, self.event, req)
        self.assertEqual("new", self.event._private_companion_task_conversation_id)
        self.assertNotIn(self.task.task_id, self.plugin._llm_call.await_args.args[0])
        self.assertNotIn(self.task.task_id, req.system_prompt)
        self.plugin.context.conversation_manager.get_curr_conversation_id.return_value = "old"
        await append_capability_guidance(self.plugin, self.event, req, force_rejudge=True)
        self.assertIn(self.task.task_id, self.plugin._llm_call.await_args.args[0])
        self.assertIn(self.task.task_id, req.system_prompt)


class TaskQueryHubTests(unittest.IsolatedAsyncioTestCase):
    def context(self, text="现在查询进度"):
        from astrbot_plugin_private_companion.capability_hub import build_capability_context

        base = build_capability_context(scope="private:u1", current_text=text,
            sender_id="u1", sender_name="甲", now=datetime.now(timezone.utc))
        return replace(base, task_session_key="qq:Friend:u1")

    async def test_hub_reads_the_real_platform_task_key_without_expiring_live_task(self):
        from astrbot_plugin_private_companion.capability_hub import judge_capabilities

        store = ConversationTaskStateStore(clock=lambda: 1)
        task = store.start_task(session_key="qq:Friend:u1", sender_id="u1", task_text="补写测试", provider_id="chosen")
        llm_call = AsyncMock(return_value='{"reply":true,"actions":[],"route":"chat","task_query":true}')
        plugin = SimpleNamespace(_conversation_task_states=store,
            enable_conversation_model_routing=True, _llm_call=llm_call,
            _task_provider=lambda *ids: "hub", llm_provider_id="hub")
        decision = await judge_capabilities(plugin, self.context(), available_capabilities=())
        self.assertTrue(decision.task_query)
        self.assertIn(task.task_id, llm_call.await_args.args[0])
        self.assertEqual("running", task.status)

    def test_query_flag_cannot_swallow_new_work(self):
        from astrbot_plugin_private_companion.capability_hub import parse_model_capability_decision

        decision = parse_model_capability_decision(json.dumps({"reply": True, "actions": [], "route": "mixed", "task_query": True,
            "segments": [{"kind": "task", "text": "再修改一个功能"}]}),
            available_capabilities=(), context=self.context("查询进度，再修改一个功能"))
        self.assertFalse(decision.task_query)

    def test_query_flag_requires_a_real_boolean(self):
        from astrbot_plugin_private_companion.capability_hub import parse_model_capability_decision

        decision = parse_model_capability_decision(json.dumps({"reply": True, "actions": [], "route": "chat", "task_query": "false"}),
            available_capabilities=(), context=self.context())
        self.assertFalse(decision.task_query)


if __name__ == "__main__":
    unittest.main()
