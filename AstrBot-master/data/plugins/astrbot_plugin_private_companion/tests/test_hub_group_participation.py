from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Plain, Reply
from astrbot_plugin_private_companion.capability_hub import CapabilityDecision
from astrbot_plugin_private_companion.final_response_persistence import (
    FinalResponsePersistenceMixin,
)
from astrbot_plugin_private_companion.passive_state_pipeline import (
    append_capability_guidance,
)


class HubGroupParticipationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc).timestamp()
        self.group = {
            "recent_messages": [
                {"ts": self.now - 20, "sender_id": "u1", "text": "少说一点"},
            ],
            "recent_bot_replies": [{"ts": self.now - 2, "text": "未发送的候选"}],
            "confirmed_bot_replies": [
                {"ts": self.now - 70, "text": "早些时候的回复", "sender_id": "u1"},
                {"ts": self.now - 10, "text": "好，我先看你们聊", "sender_id": "u1"},
                {"ts": self.now - 5, "text": "说明完成", "sender_id": "u2"},
            ],
        }
        self.plugin = SimpleNamespace(
            _event_scope_key=lambda event: (
                "private:u1" if event.is_private_chat() else "group:g1"
            ),
            _extract_group_id_from_event=lambda event: (
                "" if event.is_private_chat() else "g1"
            ),
            _get_group=lambda _: self.group,
            _recall_message_cache={},
            _task_provider=lambda *_: "hub",
            _llm_call=AsyncMock(return_value='{"reply":false,"actions":[]}'),
            _message_debounce_pending_llm={
                "group:g1:__hub_pending__": {
                    "updated_ts": self.now,
                    "active_events": {
                        "m0": {
                            "event_id": "m0",
                            "ts": self.now - 2,
                            "text": "正在回答的问题",
                        },
                        "current": {
                            "event_id": "current",
                            "ts": self.now,
                            "text": "确实",
                        },
                        "stale": {
                            "event_id": "stale",
                            "ts": self.now,
                            "text": "已被合并",
                        },
                    },
                    "stale_event_ids": ["stale"],
                },
                "group:g2:__hub_pending__": {
                    "updated_ts": self.now,
                    "active_events": {"x": {"ts": self.now, "text": "其他群的问题"}},
                },
            },
        )

    def event(self, private=False):
        return SimpleNamespace(
            message_str="确实",
            message_obj=SimpleNamespace(message_id="current"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: private,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "甲",
        )

    async def payload(self, private=False):
        await append_capability_guidance(self.plugin, self.event(private), None)
        prompt = self.plugin._llm_call.await_args.args[0]
        return json.loads(prompt.split("输入数据：\n", 1)[1])

    async def test_inline_quoted_text_reaches_hub_without_message_id(self):
        event = self.event(private=True)
        event.message_str = "这个任务"
        event.message_obj.message = [Reply(id="", chain=[Plain("明天到周五7:30叫我，每分钟一次，回复即停")])]
        await append_capability_guidance(self.plugin, event, None)
        prompt = self.plugin._llm_call.await_args.args[0]
        data = json.loads(prompt.split("输入数据：\n", 1)[1])
        self.assertEqual("这个任务", data["current_message"])
        self.assertIn("每分钟一次", data["platform_scene"]["quoted_messages"][0]["text"])
        self.assertEqual("", data["platform_scene"]["quoted_messages"][0]["message_id"])

    async def test_inline_quote_change_invalidates_same_event_reuse(self):
        event = self.event(private=True)
        event.message_str = "这个任务"
        event.message_obj.message = [Reply(id="", message_str="提醒喝水")]
        self.plugin.enable_hub_decision_reuse = True
        await append_capability_guidance(self.plugin, event, None)
        calls = self.plugin._llm_call.await_count
        event.message_obj.message = [Reply(id="", message_str="改成每分钟一次，回复即停")]
        await append_capability_guidance(self.plugin, event, None)
        self.assertGreater(self.plugin._llm_call.await_count, calls)

    async def test_unavailable_hub_does_not_wake_ordinary_group_messages(self):
        self.plugin._llm_call.return_value = None
        event = self.event()
        event.message_obj.author = {"bot": False}
        await append_capability_guidance(self.plugin, event, None)
        self.assertEqual("hub_unavailable", event._private_companion_capability_decision.reason)
        self.assertFalse(event._private_companion_capability_decision.reply)
        self.assertFalse(getattr(event, "private_companion_central_reply_authorized", False))

    async def test_unavailable_hub_keeps_explicit_and_private_failure_paths(self):
        self.plugin._llm_call.return_value = None
        for private, trigger in ((True, "group_message"), (False, "at_bot"), (False, "reply_bot")):
            with self.subTest(private=private, trigger=trigger):
                event = self.event(private)
                event.private_companion_group_scene["trigger"] = trigger
                await append_capability_guidance(self.plugin, event, None)
                self.assertTrue(event._private_companion_capability_decision.reply)

    async def test_unavailable_hub_does_not_answer_bot_mentions_or_other_targets(self):
        self.plugin._llm_call.return_value = None
        for bot, trigger in ((True, "at_bot"), (False, "at_other")):
            with self.subTest(bot=bot, trigger=trigger):
                event = self.event()
                event.message_obj.author = {"bot": bot}
                event.private_companion_group_scene["trigger"] = trigger
                await append_capability_guidance(self.plugin, event, None)
                self.assertFalse(event._private_companion_capability_decision.reply)

    async def test_hub_receives_confirmed_history_and_density_without_candidates(self):
        original = deepcopy(self.group)
        data = await self.payload()
        participation = data["group_participation"]
        self.assertEqual(2, participation["bot_replies_in_window"])
        self.assertEqual(1, participation["user_messages_in_window"])
        self.assertEqual(60, participation["window_seconds"])
        self.assertAlmostEqual(
            5, participation["seconds_since_last_bot_reply"], delta=2
        )
        self.assertEqual(
            "好，我先看你们聊", participation["recent_bot_replies"][-2]["text"]
        )
        self.assertEqual(
            "u2", participation["recent_bot_replies"][-1]["reply_to_sender_id"]
        )
        self.assertNotIn("未发送的候选", json.dumps(data, ensure_ascii=False))
        self.assertEqual(original, self.group)

    async def test_pending_replies_exclude_self_stale_and_other_groups(self):
        data = await self.payload()
        pending = data["group_participation"]["pending_replies"]
        self.assertEqual(["正在回答的问题"], [item["text"] for item in pending])
        self.assertTrue(data["pending_reply"])

    async def test_hub_receives_queue_ids_and_task_protection(self):
        pending = self.plugin._message_debounce_pending_llm["group:g1:__hub_pending__"]
        pending["active_events"]["m0"]["protected"] = True
        data = await self.payload()
        queue = data["platform_scene"]["pending_requests"]
        self.assertEqual(["m0"], [row["event_id"] for row in queue])
        self.assertTrue(queue[0]["protected"])
        self.assertGreaterEqual(queue[0]["waiting_seconds"], 1)

    async def test_queue_change_invalidates_cached_hub_decision(self):
        event = self.event()
        await append_capability_guidance(self.plugin, event, None)
        await append_capability_guidance(self.plugin, event, None)
        self.assertEqual(1, self.plugin._llm_call.await_count)
        pending = self.plugin._message_debounce_pending_llm["group:g1:__hub_pending__"]
        pending["active_events"]["new"] = {
            "event_id": "new",
            "ts": self.now,
            "text": "新补充",
        }
        await append_capability_guidance(self.plugin, event, None)
        self.assertEqual(2, self.plugin._llm_call.await_count)

    async def test_private_hub_sees_only_own_pending_queue(self):
        self.plugin._message_debounce_pending_key = lambda event: "private:u1:u1"
        self.plugin._message_debounce_pending_llm["private:u1:u1"] = {
            "active_events": {
                "p1": {"event_id": "p1", "ts": self.now, "text": "私聊问题"}
            },
        }
        data = await self.payload(private=True)
        self.assertEqual(
            "私聊问题", data["platform_scene"]["pending_requests"][0]["text"]
        )
        self.assertTrue(data["pending_reply"])
        self.assertNotIn("正在回答的问题", json.dumps(data, ensure_ascii=False))

    async def test_model_merge_targets_are_parsed(self):
        self.plugin._llm_call.return_value = '{"reply":true,"actions":[],"merge_messages":true,"merge_event_ids":["m0","m0",12]}'
        event = self.event()
        await append_capability_guidance(self.plugin, event, None)
        self.assertEqual(
            ("m0",), event._private_companion_capability_decision.merge_event_ids
        )

    async def test_hub_receives_selected_identity_even_without_emotion_feature(self):
        event = self.event()
        event._private_companion_bot_identity = {
            "name": "晚柠",
            "persona_id": "晚柠",
            "other_persona_names": ["小七"],
        }
        await append_capability_guidance(self.plugin, event, None)
        prompt = self.plugin._llm_call.await_args.args[0]
        data = json.loads(prompt.split("输入数据：\n", 1)[1])
        self.assertEqual(event._private_companion_bot_identity, data["bot_identity"])
        self.assertIn("其他人格的名字", prompt)

    async def test_other_robot_confirmed_replies_are_excluded(self):
        event = self.event()
        event.unified_msg_origin = "likh:GroupMessage:g1"
        self.group["confirmed_bot_replies"] = [
            {"ts": self.now - 2, "umo": "Nahida:GroupMessage:g1", "text": "小七已回复"},
            {"ts": self.now - 3, "umo": event.unified_msg_origin, "text": "晚柠已回复"},
        ]
        await append_capability_guidance(self.plugin, event, None)
        prompt = self.plugin._llm_call.await_args.args[0]
        data = json.loads(prompt.split("输入数据：\n", 1)[1])
        self.assertEqual(1, data["group_participation"]["bot_replies_in_window"])
        self.assertNotIn("小七已回复", prompt)
        self.assertIn("晚柠已回复", prompt)

    async def test_private_chat_does_not_receive_group_history(self):
        data = await self.payload(private=True)
        self.assertNotIn("group_participation", data)
        self.assertNotIn("先看你们聊", json.dumps(data, ensure_ascii=False))

    async def test_empty_group_has_zero_density_and_no_invented_last_reply(self):
        self.group.clear()
        self.plugin._message_debounce_pending_llm = {}
        data = await self.payload()
        participation = data["group_participation"]
        self.assertEqual(0, participation["bot_replies_in_window"])
        self.assertIsNone(participation["seconds_since_last_bot_reply"])
        self.assertEqual([], participation["recent_bot_replies"])
        self.assertFalse(data["pending_reply"])

    async def test_bad_or_future_timestamps_do_not_inflate_density(self):
        self.group["confirmed_bot_replies"].extend(
            [
                {"ts": "nan", "text": "无效时间"},
                {"ts": self.now + 1000, "text": "未来时间"},
                "broken",
            ]
        )
        data = await self.payload()
        self.assertEqual(2, data["group_participation"]["bot_replies_in_window"])

    async def test_model_keeps_authority_over_explicit_question_at_high_density(self):
        self.plugin._llm_call.return_value = (
            '{"reply":true,"actions":[],"reason":"明确新问题"}'
        )
        event = self.event()
        event.message_str = "小七，这个公式怎么算？"
        await append_capability_guidance(self.plugin, event, None)
        self.assertTrue(event._private_companion_capability_decision.reply)

    async def test_explicit_at_other_is_silent_even_if_model_would_reply(self):
        self.plugin._llm_call.return_value = (
            '{"reply":true,"actions":[],"reason":"看到当前机器人人格名"}'
        )
        event = self.event()
        event.message_str = "@小七 晚柠你怎么看"
        event.private_companion_group_scene = {
            "trigger": "at_other",
            "talking_to": "bot-x",
            "talking_to_name": "小七",
            "at_targets": [{"user_id": "bot-x", "is_bot": False}],
        }
        event.is_at_or_wake_command = True
        event.is_wake = True

        await append_capability_guidance(self.plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)
        self.assertEqual(
            "explicit_other_target", event._private_companion_capability_decision.reason
        )
        self.plugin._llm_call.assert_not_awaited()
        self.assertFalse(
            getattr(event, "private_companion_central_reply_authorized", False)
        )
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.is_wake)

    async def test_explicit_reply_to_other_overrides_cached_reply_decision(self):
        event = self.event()
        event.private_companion_group_scene = {
            "trigger": "reply_other",
            "talking_to": "bot-x",
            "talking_to_name": "小七",
        }
        event._private_companion_capability_decision = CapabilityDecision(
            reply=True,
            reply_style="text",
            emotion="neutral",
            actions=(),
            merge_messages=False,
            reason="cached_model_decision",
        )

        await append_capability_guidance(self.plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)
        self.assertEqual(
            "explicit_other_target", event._private_companion_capability_decision.reason
        )
        self.plugin._llm_call.assert_not_awaited()

    async def test_explicit_self_at_still_uses_hub_decision(self):
        self.plugin._llm_call.return_value = (
            '{"reply":true,"actions":[],"reason":"明确@当前机器人"}'
        )
        event = self.event()
        event.private_companion_group_scene = {
            "trigger": "at_bot",
            "talking_to": "bot",
            "talking_to_name": "你",
            "at_targets": [
                {"user_id": "bot-x", "is_bot": True},
                {"user_id": "bot-y", "is_bot": False},
            ],
        }

        await append_capability_guidance(self.plugin, event, None)

        self.assertTrue(event._private_companion_capability_decision.reply)
        self.plugin._llm_call.assert_awaited_once()

    async def test_platform_bot_author_reaches_hub_for_object_and_dict_payloads(self):
        for raw in (
            SimpleNamespace(author=SimpleNamespace(bot=True)),
            {"author": {"bot": True}},
            {"sender": {"is_bot": True}},
        ):
            with self.subTest(raw=type(raw).__name__):
                self.plugin._llm_call.reset_mock()
                event = self.event()
                event.message_obj.raw_message = raw
                original = deepcopy(event.private_companion_group_scene)
                await append_capability_guidance(self.plugin, event, None)
                data = json.loads(
                    self.plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
                )
                self.assertEqual(
                    "bot", data["platform_scene"]["sender_identity"]["kind"]
                )
                self.assertEqual(original, event.private_companion_group_scene)
                self.assertEqual(
                    "bot", event._private_companion_sender_identity["kind"]
                )
                self.plugin._llm_call.assert_awaited_once()

    async def test_necessary_bot_collaboration_keeps_hub_authority(self):
        self.group["hub_sender_kinds"] = {"human": "human"}
        self.group["recent_messages"] = [
            {
                "ts": self.now - 5,
                "message_id": "h1",
                "sender_id": "human",
                "text": "小七和晚柠一起检查结果，由小七确认计算是否正确",
            }
        ]
        self.plugin._llm_call.return_value = json.dumps(
            {
                "reply": True,
                "actions": [],
                "reason": "需要确认计算",
                "collaboration": {
                    "human_message_id": "h1",
                    "human_quote": "小七和晚柠一起检查结果",
                    "current_quote": "确实",
                    "need": "回应当前计算确认，推进真人委托的核验",
                },
            },
            ensure_ascii=False,
        )
        event = self.event()
        event.message_obj.raw_message = {"author": {"bot": True}}
        await append_capability_guidance(self.plugin, event, None)
        self.assertTrue(event._private_companion_capability_decision.reply)
        self.plugin._llm_call.assert_awaited_once()

    async def test_bot_smalltalk_or_historical_task_needs_verifiable_collaboration(
        self,
    ):
        for reason, actions in (
            ("同话题轻接一句", []),
            ("已有pending，收尾", []),
            ("真人之前要求生图", ["image_generate"]),
        ):
            with self.subTest(reason=reason):
                self.plugin._llm_call.return_value = json.dumps(
                    {
                        "reply": True,
                        "actions": actions,
                        "route": "task" if actions else "chat",
                        "reason": reason,
                    }
                )
                event = self.event()
                event.message_obj.raw_message = {"author": {"bot": True}}
                await append_capability_guidance(self.plugin, event, None)
                decision = event._private_companion_capability_decision
                self.assertFalse(decision.reply)
                self.assertFalse(decision.actions)
                self.assertEqual("chat", decision.route)
                self.assertFalse(
                    getattr(event, "private_companion_central_reply_authorized", False)
                )

    async def test_bot_cannot_forge_human_evidence_or_reuse_unqualified_cache(self):
        self.group["recent_messages"] = [
            {
                "ts": self.now - 5,
                "message_id": "b1",
                "sender_id": "bot",
                "text": "小七和晚柠一起检查结果",
            }
        ]
        self.group["hub_sender_kinds"] = {"bot": "bot"}
        self.plugin._llm_call.return_value = json.dumps(
            {
                "reply": True,
                "actions": [],
                "collaboration": {
                    "human_message_id": "b1",
                    "human_quote": "小七和晚柠一起检查结果",
                    "current_quote": "确实",
                    "need": "推进真人委托的协作核验",
                },
            }
        )
        for cached in (False, True):
            with self.subTest(cached=cached):
                event = self.event()
                event.message_obj.raw_message = {"author": {"bot": True}}
                if cached:
                    event._private_companion_capability_decision = CapabilityDecision(
                        True, "text", "neutral", (), False, "cached"
                    )
                await append_capability_guidance(self.plugin, event, None)
                self.assertFalse(event._private_companion_capability_decision.reply)

    async def test_observed_human_identity_and_message_id_survive_history_projection(
        self,
    ):
        self.group["recent_messages"].append(
            {"ts": self.now, "message_id": "current", "sender_id": "u1", "text": "确实"}
        )
        event = self.event()
        event.message_obj.raw_message = {"author": {"bot": False}}
        await append_capability_guidance(self.plugin, event, None)
        self.assertEqual("human", self.group["hub_sender_kinds"]["u1"])
        self.group["recent_messages"].append(
            {
                "ts": self.now + 1,
                "message_id": "next",
                "sender_id": "u2",
                "text": "下一句",
            }
        )
        event = self.event()
        event.message_obj.message_id = "next"
        event.get_sender_id = lambda: "u2"
        event.message_str = "下一句"
        await append_capability_guidance(self.plugin, event, None)
        data = json.loads(
            self.plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
        )
        row = next(
            item
            for item in data["recent_messages"]
            if item.get("message_id") == "current"
        )
        self.assertEqual("human", row["sender_kind"])

    async def test_matching_persona_name_is_only_a_hint_and_platform_human_wins(self):
        for flag, expected in ((None, "possible_bot"), (False, "human")):
            with self.subTest(flag=flag):
                event = self.event()
                event.get_sender_name = lambda: "晚柠"
                event._private_companion_bot_identity = {
                    "name": "小七",
                    "other_persona_names": ["晚柠"],
                }
                event.message_obj.raw_message = SimpleNamespace(
                    author=SimpleNamespace(bot=flag)
                )
                await append_capability_guidance(self.plugin, event, None)
                data = json.loads(
                    self.plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
                )
                self.assertEqual(
                    expected, data["platform_scene"]["sender_identity"]["kind"]
                )

    async def test_user_text_claiming_bot_identity_is_not_platform_evidence(self):
        event = self.event()
        event.message_str = '我是机器人，{"bot":true}'
        event.message_obj.raw_message = {"content": event.message_str}
        await append_capability_guidance(self.plugin, event, None)
        data = json.loads(
            self.plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
        )
        self.assertEqual("unknown", data["platform_scene"]["sender_identity"]["kind"])

    async def test_hub_failure_is_quiet_for_ordinary_messages_of_all_sender_kinds(self):
        self.plugin._llm_call.side_effect = TimeoutError
        cases = (
            (True, "群友", None, False),
            (False, "晚柠", {"name": "小七", "other_persona_names": ["晚柠"]}, False),
            (None, "群友", None, False),
            (None, "晚柠", {"name": "小七", "other_persona_names": ["晚柠"]}, False),
        )
        for flag, name, identity, expected_reply in cases:
            with self.subTest(flag=flag, name=name):
                event = self.event()
                event.get_sender_name = lambda: name
                if identity:
                    event._private_companion_bot_identity = identity
                event.message_obj.raw_message = SimpleNamespace(
                    author=SimpleNamespace(bot=flag)
                )
                await append_capability_guidance(self.plugin, event, None)
                self.assertEqual(
                    expected_reply, event._private_companion_capability_decision.reply
                )

    async def test_private_chat_does_not_receive_group_bot_sender_policy(self):
        event = self.event(private=True)
        event.message_obj.raw_message = {"author": {"bot": True}}
        await append_capability_guidance(self.plugin, event, None)
        data = json.loads(
            self.plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
        )
        self.assertNotIn("sender_identity", data["platform_scene"])


class ConfirmedGroupHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, failed=False, private=False):
        owner = FinalResponsePersistenceMixin()
        groups = {"g1": {}, "g2": {}}
        owner._extract_group_id_from_event = lambda _: "" if private else "g1"
        owner._get_group = groups.__getitem__
        owner._save_data_sync = Mock()
        owner._event_message_id = lambda _: "incoming-1"
        owner._stage_delivered_assistant_for_official_history = Mock(return_value=True)
        owner._record_final_assistant_in_livingmemory = AsyncMock(return_value=False)
        event = SimpleNamespace(
            unified_msg_origin="default:GroupMessage:g1",
            get_result=lambda: SimpleNamespace(chain=[Plain("未发送候选")]),
            get_sender_id=lambda: "u1",
            is_private_chat=lambda: private,
            is_stopped=lambda: False,
            send=AsyncMock(return_value=False if failed else None),
        )
        coordinator = owner._final_response_persistence_coordinator()
        ledger = coordinator.begin_passive(event)
        ledger.final_chain_start = 0
        coordinator.install_send_tracking(event)
        self.assertNotIn("confirmed_bot_replies", groups["g1"])
        await event.send(SimpleNamespace(chain=[Plain("第一段")]))
        await event.send(SimpleNamespace(chain=[Plain("第二段")]))
        await coordinator.finalize_passive(event)
        await coordinator.finalize_passive(event)
        return groups

    async def test_confirmed_segments_form_one_group_reply_and_are_idempotent(self):
        groups = await self.exercise()
        replies = groups["g1"]["confirmed_bot_replies"]
        self.assertEqual(1, len(replies))
        self.assertEqual("第一段\n第二段", replies[0]["text"])
        self.assertEqual("u1", replies[0]["sender_id"])
        self.assertEqual({}, groups["g2"])

    async def test_failed_sends_do_not_enter_group_history(self):
        groups = await self.exercise(failed=True)
        self.assertNotIn("confirmed_bot_replies", groups["g1"])

    async def test_private_deliveries_do_not_enter_group_history(self):
        groups = await self.exercise(private=True)
        self.assertNotIn("confirmed_bot_replies", groups["g1"])
