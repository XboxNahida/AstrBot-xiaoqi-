from __future__ import annotations

import asyncio
import functools
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.pipeline.context_utils import call_event_hook
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class Event:
    def __init__(
        self, text: str, message_id: str, sender: str = "u1", group: str = "g1"
    ) -> None:
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id=message_id)
        self._sender = sender
        self._group = group
        self.stopped = False

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def is_private_chat(self) -> bool:
        return False

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return self._sender

    def get_group_id(self) -> str:
        return self._group


class HubPendingBatchTests(unittest.IsolatedAsyncioTestCase):
    def test_media_message_cannot_be_swallowed_by_text_only_batch(self):
        from astrbot.core.message.components import File

        first = Event("请接收文件", "m1")
        second = Event("请接收文件", "m2")
        first.message_obj.message = [File(name="一.zip", file="one.zip")]
        second.message_obj.message = [File(name="二.zip", file="two.zip")]
        first._private_companion_capability_decision = SimpleNamespace(merge_messages=True)
        second._private_companion_capability_decision = SimpleNamespace(merge_messages=True)
        self.plugin._message_debounce_mark_llm_pending(first)
        self.assertFalse(self.plugin._message_debounce_absorb_pending_message(second, second.message_str))
        third = Event("请接收文件", "m3")
        third._private_companion_capability_decision = SimpleNamespace(merge_messages=True)
        self.assertFalse(self.plugin._message_debounce_absorb_pending_message(third, third.message_str))

    def setUp(self) -> None:
        self.plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        self.plugin._central_hub_scheduler_enabled = True
        self.plugin.enable_message_debounce = True
        self.plugin._message_debounce_pending_llm = {}
        self.plugin._semantic_message_buffers = {}
        self.plugin._event_is_inbound_chat_message = lambda _event: True
        self.plugin._extract_group_id_from_event = lambda event: event.get_group_id()
        self.plugin._group_topic_signature = lambda text: (
            "algorithm" if "复杂度" in text else "other"
        )
        self.plugin._topic_signature_similar = lambda left, right: left == right

    def test_queue_hooks_are_registered_to_the_loaded_plugin_module(self):
        expected = {
            "guard_pending_message_debounce": EventType.OnWaitingLLMRequestEvent,
            "guard_superseded_hub_request": EventType.OnLLMRequestEvent,
            "settle_pending_message_debounce": EventType.OnLLMResponseEvent,
            "confirm_pending_message_debounce_after_send": EventType.OnAfterMessageSentEvent,
        }
        handlers = star_handlers_registry.get_handlers_by_module_name(
            PrivateCompanionPlugin.__module__
        )
        actual = {
            handler.handler_name: handler.event_type
            for handler in handlers
            if handler.handler_name in expected
        }
        self.assertEqual(expected, actual)

    async def test_framework_response_and_send_hooks_release_completed_queue(self):
        event = Event("小七", "m1")
        event.send = AsyncMock(return_value=None)
        event.plugins_name = None
        self.plugin._install_reaction_expression_delivery_tracker = lambda *_: None
        self.plugin._reaction_expression_primary_reply_confirmed = lambda _: True
        self.plugin._message_debounce_mark_llm_pending(event)
        selected = [
            handler
            for handler in star_handlers_registry.get_handlers_by_module_name(
                PrivateCompanionPlugin.__module__
            )
            if handler.handler_name
            in {
                "settle_pending_message_debounce",
                "confirm_pending_message_debounce_after_send",
            }
        ]
        patches = [
            patch.object(
                handler, "handler", functools.partial(handler.handler, self.plugin)
            )
            for handler in selected
        ]
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(star_handlers_registry, "_handlers", selected)
            )
            stack.enter_context(
                patch.object(
                    star_map[PrivateCompanionPlugin.__module__],
                    "name",
                    "companion-test",
                )
            )
            for binding in patches:
                stack.enter_context(binding)
            await call_event_hook(
                event,
                EventType.OnLLMResponseEvent,
                SimpleNamespace(completion_text="在", result_chain=None),
            )
            await event.send(SimpleNamespace(chain=[]))
            await call_event_hook(event, EventType.OnAfterMessageSentEvent)

        self.assertEqual({}, self.plugin._message_debounce_pending_llm)
        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                Event("唯一支持小七的一次", "m2"), "唯一支持小七的一次"
            )
        )

    async def test_third_related_cross_sender_message_merges_pending_batch(
        self,
    ) -> None:
        first = Event("小七，算法时间复杂度？", "m1", "u1")
        second = Event("那空间复杂度呢？", "m2", "u2")
        third = Event("算法复杂度怎么算？", "m3", "u3")
        self.plugin._message_debounce_mark_llm_pending(first)

        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                second, second.message_str
            )
        )
        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                third, third.message_str
            )
        )
        key = self.plugin._semantic_buffer_key("group:g1", "u3")
        self.assertEqual(
            "hub_batch", self.plugin._semantic_message_buffers[key]["kind"]
        )
        texts = [
            item["text"]
            for item in self.plugin._semantic_message_buffers[key]["messages"]
        ]
        self.assertEqual(
            [first.message_str, second.message_str, third.message_str], texts
        )
        self.assertEqual(
            ["u1", "u2", "u3"],
            [
                item["sender_name"]
                for item in self.plugin._semantic_message_buffers[key]["messages"]
            ],
        )
        combined = await self.plugin._consume_semantic_message_buffer_for_event(
            third, private_chat=False
        )
        self.assertIn("u1:", combined)
        self.assertIn("u2:", combined)
        self.assertIn("u3:", combined)

        first_response = SimpleNamespace(
            completion_text="旧回复", result_chain=["旧链路"]
        )
        await self.plugin.settle_pending_message_debounce(first, first_response)
        self.assertEqual("", first_response.completion_text)
        self.plugin._message_debounce_mark_llm_pending(third)
        third_response = SimpleNamespace(completion_text="合并后的回复")
        await self.plugin.settle_pending_message_debounce(third, third_response)
        self.assertEqual("合并后的回复", third_response.completion_text)

    def test_model_merge_decision_allows_two_message_cross_sender_batch(self) -> None:
        first = Event("小七，算法时间复杂度？", "m1", "u1")
        second = Event("空间复杂度呢？", "m2", "u2")
        first._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        second._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )

        self.plugin._message_debounce_mark_llm_pending(first)

        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                second, second.message_str
            )
        )

    def test_hub_merge_survives_slow_generation_and_local_signature_mismatch(self):
        first = Event("算法时间复杂度？", "m1")
        second = Event("补充一下刚才的条件", "m2")
        second._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        with patch(
            "astrbot_plugin_private_companion.event_dispatch._now_ts", return_value=1000
        ):
            self.plugin._message_debounce_mark_llm_pending(first)
        with patch(
            "astrbot_plugin_private_companion.event_dispatch._now_ts", return_value=1120
        ):
            self.assertTrue(
                self.plugin._message_debounce_absorb_pending_message(
                    second, second.message_str
                )
            )

    async def test_waiting_requests_are_visible_and_superseded_before_provider_call(
        self,
    ):
        first = Event("算法时间复杂度？", "m1")
        second = Event("补充一下约束", "m2")
        for event in (first, second):
            event._private_companion_capability_decision = SimpleNamespace(
                reply=True,
                merge_messages=True,
                route="chat",
                actions=(),
            )
        await self.plugin.guard_pending_message_debounce(first)
        await self.plugin.guard_pending_message_debounce(second)

        key = self.plugin._message_debounce_pending_key(first)
        self.assertIn(
            "m1", self.plugin._message_debounce_pending_llm[key]["stale_event_ids"]
        )
        await self.plugin.guard_superseded_hub_request(first, SimpleNamespace())
        await self.plugin.guard_superseded_hub_request(second, SimpleNamespace())
        self.assertTrue(first.stopped)
        self.assertFalse(second.stopped)

    async def test_continuous_merges_preserve_all_earlier_supplements(self):
        events = [
            Event(text, f"m{i}")
            for i, text in enumerate(
                (
                    "算法复杂度？",
                    "约束是大数据",
                    "还要考虑内存",
                    "只需简述",
                )
            )
        ]
        for event in events:
            event._private_companion_capability_decision = SimpleNamespace(
                reply=True,
                merge_messages=True,
                route="chat",
                actions=(),
            )
            await self.plugin.guard_pending_message_debounce(event)
        combined = await self.plugin._consume_semantic_message_buffer_for_event(
            events[-1], private_chat=False
        )
        for event in events:
            self.assertIn(event.message_str, combined)
        pending = self.plugin._message_debounce_pending_llm[
            self.plugin._message_debounce_pending_key(events[-1])
        ]
        self.assertEqual({"m0", "m1", "m2"}, set(pending["stale_event_ids"]))
        self.assertIn(events[0].message_str, pending["active_events"]["m3"]["text"])

    def test_task_and_tool_requests_are_not_superseded_by_chat_merge(self):
        for decision in (
            SimpleNamespace(route="task", actions=()),
            SimpleNamespace(route="mixed", actions=()),
            SimpleNamespace(route="chat", actions=("image_generate",)),
        ):
            with self.subTest(decision=decision):
                self.plugin._message_debounce_pending_llm.clear()
                first = Event("算法复杂度？", "m1")
                first._private_companion_capability_decision = decision
                self.plugin._message_debounce_mark_llm_pending(first)
                second = Event("算法复杂度补充", "m2")
                second._private_companion_capability_decision = SimpleNamespace(
                    merge_messages=True
                )
                self.assertFalse(
                    self.plugin._message_debounce_absorb_pending_message(
                        second, second.message_str
                    )
                )

    def test_chat_expression_actions_still_allow_pending_merge(self):
        first = Event("算法复杂度？", "m1")
        first._private_companion_capability_decision = SimpleNamespace(
            route="chat", actions=("emoji", "voice")
        )
        self.plugin._message_debounce_mark_llm_pending(first)
        second = Event("补充条件", "m2")
        second._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                second, second.message_str
            )
        )

    async def test_same_group_two_bot_instances_have_separate_queues_and_batches(self):
        first = Event("算法复杂度？", "m1")
        other = Event("算法复杂度？", "m1")
        followup = Event("算法复杂度补充", "m2")
        first.unified_msg_origin = followup.unified_msg_origin = "likh:GroupMessage:g1"
        other.unified_msg_origin = "Nahida:GroupMessage:g1"
        for event in (first, other):
            self.plugin._message_debounce_mark_llm_pending(event)
        followup._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                followup, followup.message_str
            )
        )
        response = SimpleNamespace(completion_text="另一个机器人的回复")
        await self.plugin.settle_pending_message_debounce(other, response)
        self.assertEqual("另一个机器人的回复", response.completion_text)
        self.assertEqual(
            "",
            await self.plugin._consume_semantic_message_buffer_for_event(
                other, private_chat=False
            ),
        )

    def test_marking_again_does_not_revive_superseded_request(self):
        first = Event("算法复杂度？", "m1")
        second = Event("算法复杂度补充", "m2")
        second._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._message_debounce_absorb_pending_message(second, second.message_str)
        self.plugin._message_debounce_mark_llm_pending(first)
        pending = self.plugin._message_debounce_pending_llm[
            self.plugin._message_debounce_pending_key(first)
        ]
        self.assertIn("m1", pending["stale_event_ids"])
        self.assertEqual("m2", pending["event_id"])

    async def test_private_queue_gets_hub_decision_before_waiting_for_lock(self):
        self.plugin.enabled = True
        self.plugin.data = {"users": {"u1": {"enabled": True}}}
        self.plugin._private_passive_profile_available = lambda user_id, user: bool(
            user
        )
        self.plugin._private_user_id_for_event = lambda event, user_id: user_id
        first = Event("在吗", "m1")
        first.is_private_chat = lambda: True
        with patch(
            "astrbot_plugin_private_companion.main.append_capability_guidance",
            new_callable=AsyncMock,
        ) as judge:
            await self.plugin.prepare_hub_message_queue.__wrapped__(self.plugin, first)
        judge.assert_awaited_once_with(self.plugin, first, None)

    async def test_private_queue_does_not_capture_non_companion_user(self):
        self.plugin.enabled = True
        self.plugin.data = {"users": {}}
        self.plugin._private_passive_profile_available = lambda user_id, user: bool(
            user
        )
        self.plugin._private_user_id_for_event = lambda event, user_id: user_id
        event = Event("在吗", "m1")
        event.is_private_chat = lambda: True
        with patch(
            "astrbot_plugin_private_companion.main.append_capability_guidance",
            new_callable=AsyncMock,
        ) as judge:
            await self.plugin.prepare_hub_message_queue.__wrapped__(self.plugin, event)
        judge.assert_not_awaited()

    async def test_premerged_group_waits_for_full_batch_before_rejudging(self):
        self.plugin.enabled = True
        event = Event("最后一句补充", "m2")
        event.private_companion_debounce_pending_merged = True
        event._private_companion_capability_decision = SimpleNamespace(reply=True)
        with patch(
            "astrbot_plugin_private_companion.main.append_capability_guidance",
            new_callable=AsyncMock,
        ) as judge:
            await self.plugin.prepare_hub_message_queue.__wrapped__(self.plugin, event)
        judge.assert_not_awaited()

    async def test_locked_burst_only_calls_provider_for_latest_combined_request(self):
        lock = asyncio.Lock()
        await lock.acquire()
        provider = AsyncMock(return_value="统一回答")
        events = [
            Event(text, f"m{i}")
            for i, text in enumerate(("算法复杂度？", "约束是大数据", "还要考虑内存"))
        ]

        async def process(event):
            async with lock:
                await self.plugin.guard_superseded_hub_request(event, SimpleNamespace())
                if event.stopped:
                    return
                prompt = await self.plugin._consume_semantic_message_buffer_for_event(
                    event, private_chat=False
                )
                await provider(prompt)

        tasks = []
        for event in events:
            event._private_companion_capability_decision = SimpleNamespace(
                reply=True, merge_messages=True
            )
            await self.plugin.guard_pending_message_debounce(event)
            tasks.append(asyncio.create_task(process(event)))
        lock.release()
        await asyncio.gather(*tasks)
        provider.assert_awaited_once()
        for event in events:
            self.assertIn(event.message_str, provider.await_args.args[0])

    def test_explicit_merge_ids_leave_independent_questions_pending(self):
        first = Event("算法复杂度？", "m1")
        independent = Event("天气怎么样", "m2")
        for event in (first, independent):
            self.plugin._message_debounce_mark_llm_pending(event)
        followup = Event("请补充空间占用", "m3")
        followup._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True, merge_event_ids=("m1",)
        )
        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                followup, followup.message_str
            )
        )
        pending = self.plugin._message_debounce_pending_llm[
            self.plugin._message_debounce_pending_key(first)
        ]
        self.assertEqual(["m1"], pending["stale_event_ids"])
        self.assertIn("m2", pending["active_events"])

    async def test_superseded_during_enrichment_cannot_start_task_route(self):
        self.plugin.enabled = True
        self.plugin.enable_conversation_model_routing = True
        first = Event("算法复杂度？", "m1")
        first._private_companion_capability_decision = SimpleNamespace(
            reply=True, route="chat"
        )
        self.plugin._message_debounce_mark_llm_pending(first)
        second = Event("补充条件", "m2")
        second._private_companion_capability_decision = SimpleNamespace(
            merge_messages=True
        )
        self.plugin._message_debounce_absorb_pending_message(second, second.message_str)
        with patch(
            "astrbot_plugin_private_companion.main.configure_conversation_request"
        ) as configure:
            await self.plugin.route_conversation_models.__wrapped__(
                self.plugin, first, SimpleNamespace()
            )
        configure.assert_not_called()
        self.assertTrue(first.stopped)

    def test_other_group_and_unrelated_topic_are_not_merged(self) -> None:
        first = Event("小七，算法时间复杂度？", "m1", "u1")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                Event("补充", "m2", "u2", "g2"), "补充"
            )
        )
        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                Event("明天吃什么", "m3", "u3"), "明天吃什么"
            )
        )
        self.assertEqual({}, self.plugin._semantic_message_buffers)

    def test_unrelated_active_request_does_not_count_toward_batch(self) -> None:
        first = Event("小七，算法时间复杂度？", "m1", "u1")
        unrelated = Event("明天吃什么", "m2", "u2")
        third = Event("算法复杂度怎么算？", "m3", "u3")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._message_debounce_mark_llm_pending(unrelated)

        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                third, third.message_str
            )
        )
        self.assertEqual({}, self.plugin._semantic_message_buffers)

    async def test_empty_response_clears_pending_state(self) -> None:
        first = Event("小七在吗", "m1")
        self.plugin._message_debounce_mark_llm_pending(first)
        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="")
        )
        self.assertEqual({}, self.plugin._message_debounce_pending_llm)

    async def test_nonempty_response_stays_pending_until_send_confirmation(
        self,
    ) -> None:
        first = Event("小七在吗", "m1")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._install_reaction_expression_delivery_tracker = (
            lambda event, pending: None
        )
        self.plugin._reaction_expression_primary_reply_confirmed = lambda event: True

        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="在")
        )
        self.assertIn(
            self.plugin._message_debounce_pending_key(first),
            self.plugin._message_debounce_pending_llm,
        )

        await self.plugin.confirm_pending_message_debounce_after_send(first)
        self.assertEqual({}, self.plugin._message_debounce_pending_llm)

    async def test_related_messages_can_merge_after_llm_before_send(self) -> None:
        first = Event("算法复杂度怎么算？", "m1", "u1")
        second = Event("时间复杂度呢？", "m2", "u2")
        third = Event("空间复杂度也说说", "m3", "u3")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._install_reaction_expression_delivery_tracker = (
            lambda event, pending: None
        )
        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="时间复杂度是……")
        )

        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                second, second.message_str
            )
        )
        self.assertTrue(
            self.plugin._message_debounce_absorb_pending_message(
                third, third.message_str
            )
        )

    async def test_stale_generated_reply_is_blocked_at_send(self) -> None:
        first = Event("算法复杂度怎么算？", "m1", "u1")
        second = Event("时间复杂度呢？", "m2", "u2")
        third = Event("空间复杂度也说说", "m3", "u3")
        original_send = AsyncMock(return_value=None)
        first.send = original_send
        self.plugin._install_reaction_expression_delivery_tracker = (
            lambda event, pending: None
        )
        self.plugin._message_debounce_mark_llm_pending(first)
        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="旧回复")
        )
        self.plugin._message_debounce_absorb_pending_message(second, second.message_str)
        self.plugin._message_debounce_absorb_pending_message(third, third.message_str)

        self.assertFalse(await first.send(SimpleNamespace(chain=[])))
        original_send.assert_not_awaited()

    async def test_send_already_started_is_not_merged_into_new_batch(self) -> None:
        first = Event("算法复杂度怎么算？", "m1", "u1")
        second = Event("时间复杂度呢？", "m2", "u2")
        third = Event("空间复杂度也说说", "m3", "u3")
        first.send = AsyncMock(return_value=None)
        self.plugin._install_reaction_expression_delivery_tracker = (
            lambda event, pending: None
        )
        self.plugin._message_debounce_mark_llm_pending(first)
        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="回复")
        )
        await first.send(SimpleNamespace(chain=[]))

        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                second, second.message_str
            )
        )
        self.assertFalse(
            self.plugin._message_debounce_absorb_pending_message(
                third, third.message_str
            )
        )

    async def test_expired_old_response_cannot_escape_pending_guard(self) -> None:
        first = Event("小七在吗", "m1")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._message_debounce_pending_llm.clear()
        response = SimpleNamespace(completion_text="迟到回复", result_chain=["旧链路"])

        await self.plugin.settle_pending_message_debounce(first, response)

        self.assertEqual("", response.completion_text)
        self.assertIsNone(response.result_chain)

    async def test_expired_request_accepts_none_response_from_agent_hook(self) -> None:
        first = Event("小七在吗", "m1")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._message_debounce_pending_llm.clear()

        await self.plugin.settle_pending_message_debounce(first, None)

    async def test_intermediate_send_keeps_pending_until_final_model_reply(self) -> None:
        # 工具调用轮可能没有正文，也可能先发送一句说明；两种都不是任务收尾。
        for prefix in ("", "我查一下后台任务。"):
            with self.subTest(prefix=prefix):
                first = Event("确认任务开始了吗", "m-query")
                first.send = AsyncMock(return_value=None)
                self.plugin._install_reaction_expression_delivery_tracker = lambda *_: None
                self.plugin._reaction_expression_primary_reply_confirmed = lambda _: bool(prefix)
                self.plugin._message_debounce_mark_llm_pending(first)
                key = self.plugin._message_debounce_pending_key(first)

                # 对应run_agent工具轮的after_message_sent，最终on_llm_response尚未触发。
                await self.plugin.confirm_pending_message_debounce_after_send(first)
                self.assertIn(key, self.plugin._message_debounce_pending_llm)
                self.assertIn("m-query", self.plugin._message_debounce_pending_llm[key]["active_events"])

                response = SimpleNamespace(completion_text="任务已启动，正在处理文档。")
                await self.plugin.settle_pending_message_debounce(first, response)
                self.assertEqual("任务已启动，正在处理文档。", response.completion_text)
                await first.send(SimpleNamespace(chain=[response.completion_text]))
                self.plugin._reaction_expression_primary_reply_confirmed = lambda _: True
                await self.plugin.confirm_pending_message_debounce_after_send(first)
                self.assertNotIn(key, self.plugin._message_debounce_pending_llm)

    async def test_repeated_tool_send_hooks_do_not_invalidate_later_answer(self) -> None:
        first = Event("查询任务和最近结果", "m-many-tools")
        self.plugin._message_debounce_mark_llm_pending(first)
        key = self.plugin._message_debounce_pending_key(first)
        self.plugin._reaction_expression_primary_reply_confirmed = lambda _: False
        for _ in range(3):
            await self.plugin.confirm_pending_message_debounce_after_send(first)
        response = SimpleNamespace(completion_text="已查询，后台仍在运行。")
        await self.plugin.settle_pending_message_debounce(first, response)
        self.assertEqual("已查询，后台仍在运行。", response.completion_text)
        self.assertIn(key, self.plugin._message_debounce_pending_llm)

    async def test_failed_send_releases_pending_without_marking_delivered(self) -> None:
        first = Event("小七在吗", "m1")
        self.plugin._message_debounce_mark_llm_pending(first)
        self.plugin._install_reaction_expression_delivery_tracker = (
            lambda event, pending: None
        )
        self.plugin._reaction_expression_primary_reply_confirmed = lambda event: False

        await self.plugin.settle_pending_message_debounce(
            first, SimpleNamespace(completion_text="在")
        )
        await self.plugin.confirm_pending_message_debounce_after_send(first)

        self.assertEqual({}, self.plugin._message_debounce_pending_llm)


if __name__ == "__main__":
    unittest.main()
