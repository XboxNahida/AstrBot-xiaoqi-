from __future__ import annotations

import asyncio
import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.capability_hub import build_capability_context

try:
    from astrbot_plugin_private_companion import hub_optimizations as optimization
except ImportError:
    optimization = None


class HubOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(optimization, "Hub optimization implementation is missing")

    def context(self, text="今天挺热", scope="group:g1", session="bot1:g1"):
        context = build_capability_context(
            scope=scope,
            current_text=text,
            sender_id="u1",
            sender_name="甲",
            now=datetime.now(timezone.utc),
            recent_messages=[],
            available_capabilities=["search"],
        )
        from dataclasses import replace

        return replace(context, task_session_key=session)

    def plugin(self, enabled=True):
        return SimpleNamespace(
            enable_hub_group_batching=enabled,
            hub_group_batch_seconds=0.03,
            _llm_call=AsyncMock(return_value='{"reply":false,"actions":[]}'),
        )

    async def call(
        self, plugin, identity, *, text="今天挺热", session="bot1:g1", scene=None
    ):
        return await optimization.call_hub_model(
            plugin,
            context=self.context(text, session=session),
            scene={"message_id": identity, "trigger": "group_message", **(scene or {})},
            payload={"current_message": text, "sender_id": "u1", "sender_name": "甲"},
            prompt="规则\n输入数据：\n{}",
            provider_id="hub",
            max_tokens=600,
        )

    async def test_burst_uses_one_call_and_preserves_each_message_decision(self):
        plugin = self.plugin()
        plugin._llm_call.return_value = json.dumps(
            {
                "decisions": [
                    {"message_id": "m1", "reply": False, "actions": []},
                    {
                        "message_id": "m2",
                        "reply": True,
                        "actions": [],
                        "reason": "值得回应",
                    },
                ]
            }
        )
        first, second = await asyncio.gather(
            self.call(plugin, "m1"), self.call(plugin, "m2")
        )
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertFalse(json.loads(first)["reply"])
        self.assertTrue(json.loads(second)["reply"])
        self.assertIn("m1", plugin._llm_call.await_args.args[0])
        self.assertIn("m2", plugin._llm_call.await_args.args[0])

    async def test_disabled_batching_uses_original_calls(self):
        plugin = self.plugin(False)
        await asyncio.gather(self.call(plugin, "m1"), self.call(plugin, "m2"))
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_hub_fallback_uses_its_card_for_direct_single_and_batch(self):
        from astrbot_plugin_private_companion.tests.test_model_fallback import (
            _FallbackHarness,
        )

        for mode in ("direct", "single", "batch"):
            with self.subTest(mode=mode):
                response = '{"reply":false,"actions":[]}'
                if mode == "batch":
                    response = json.dumps(
                        {
                            "decisions": [
                                {"message_id": "m1", "reply": False, "actions": []},
                                {"message_id": "m2", "reply": False, "actions": []},
                            ]
                        }
                    )
                plugin = _FallbackHarness(
                    {"hub": RuntimeError("unavailable"), "gemini": response},
                    mode="quick",
                )
                plugin.config = {
                    "CONVERSATION_HUB_PROVIDER_ID": "hub",
                    "FAST_RESPONSE_PROVIDER_ID": "hub",
                }
                plugin.model_fallback_overrides = {
                    "CONVERSATION_HUB_PROVIDER_ID": "gemini",
                    "FAST_RESPONSE_PROVIDER_ID": "wrong-fallback",
                }
                plugin.enable_hub_group_batching = mode != "direct"
                plugin.hub_group_batch_seconds = 0
                if mode == "batch":
                    results = await asyncio.gather(
                        self.call(plugin, "m1"), self.call(plugin, "m2")
                    )
                else:
                    results = [await self.call(plugin, "m1")]
                self.assertEqual(["hub"] * 3 + ["gemini"], plugin.context.calls)
                self.assertTrue(
                    all(json.loads(result)["reply"] is False for result in results)
                )

    async def test_bot_instances_and_groups_do_not_share_batches(self):
        plugin = self.plugin()
        await asyncio.gather(
            self.call(plugin, "m1", session="bot1:g1"),
            self.call(plugin, "m2", session="bot2:g1"),
            self.call(plugin, "m3", session="bot1:g2"),
        )
        self.assertEqual(3, plugin._llm_call.await_count)

    async def test_structural_correction_shares_each_models_three_attempts(self):
        from astrbot_plugin_private_companion.capability_hub import judge_capabilities
        from astrbot_plugin_private_companion.tests.test_model_fallback import (
            _FallbackHarness,
        )

        for batchable in (False, True):
            with self.subTest(batchable=batchable):
                plugin = _FallbackHarness({"primary": "invalid JSON", "backup": RuntimeError("down")})
                original_generate = plugin.context.llm_generate
                async def generate(**kwargs):
                    result = await original_generate(**kwargs)
                    plugin.context.responses["primary"] = RuntimeError("down")
                    return result
                plugin.context.llm_generate = generate
                plugin.model_fallback_overrides = {"CONVERSATION_HUB_PROVIDER_ID": "backup"}
                plugin.enable_hub_group_batching = batchable
                plugin.hub_group_batch_seconds = 0
                async def judge(identity):
                    return await judge_capabilities(
                        plugin, self.context(), available_capabilities=(),
                        scene={"message_id": identity},
                    )
                results = await asyncio.gather(judge("m1"), judge("m2")) if batchable else [await judge("m1")]
                self.assertTrue(all(result is None for result in results))
                # Batch transport is shared; each message's correction retains
                # the consumed transport attempts, rather than starting at zero.
                self.assertEqual(plugin.context.calls.count("primary"), 5 if batchable else 3)
                self.assertEqual(plugin.context.calls.count("backup"), 6 if batchable else 3)


    async def test_explicit_wakeup_and_task_controls_bypass_batch(self):
        for scene, text in [
            ({"trigger": "at_bot"}, "今天挺热"),
            ({"trigger": "reply_bot"}, "接着说"),
            ({"trigger": "group_wakeup_name"}, "小七"),
            ({}, "取消刚才的任务"),
            ({}, "帮我修改服务器"),
        ]:
            with self.subTest(scene=scene, text=text):
                plugin = self.plugin()
                plugin.hub_group_batch_seconds = 30
                await asyncio.wait_for(
                    self.call(plugin, "m1", text=text, scene=scene), 0.2
                )
                self.assertEqual(1, plugin._llm_call.await_count)
                self.assertFalse(getattr(plugin, "_hub_group_batches", {}))

    async def test_cancelled_batch_waiter_does_not_cancel_other_messages(self):
        plugin = self.plugin()
        plugin._llm_call.return_value = '{"reply":false,"actions":[]}'
        one = asyncio.create_task(self.call(plugin, "m1"))
        two = asyncio.create_task(self.call(plugin, "m2"))
        await asyncio.sleep(0)
        one.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await one
        result = await asyncio.wait_for(two, 0.3)
        self.assertFalse(json.loads(result)["reply"])
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertFalse(plugin._hub_group_batches)

    async def test_missing_or_duplicate_batch_item_fails_closed(self):
        plugin = self.plugin()
        plugin._llm_call.return_value = '{"decisions":[{"message_id":"m1","reply":true},{"message_id":"m1","reply":false}]}'
        first, second = await asyncio.gather(
            self.call(plugin, "m1"), self.call(plugin, "m2")
        )
        self.assertIsNone(first)
        self.assertIsNone(second)

    async def test_reload_cancels_registered_batch_and_releases_waiters(self):
        plugin = self.plugin()
        tasks = []

        def register(operation, *, label):
            self.assertEqual("hub_group_batch", label)
            task = asyncio.create_task(operation)
            tasks.append(task)
            return task

        plugin._create_lifecycle_background_task = register
        waiter = asyncio.create_task(self.call(plugin, "m1"))
        await asyncio.sleep(0)
        self.assertEqual(1, len(tasks))
        tasks[0].cancel()
        self.assertIsNone(await asyncio.wait_for(waiter, 0.3))
        self.assertFalse(plugin._hub_group_batches)
        plugin._llm_call.assert_not_awaited()

    async def test_stopped_plugin_rejects_new_batch_without_hanging(self):
        plugin = self.plugin()

        def stopped(operation, *, label):
            operation.close()
            return None

        plugin._create_lifecycle_background_task = stopped
        self.assertIsNone(await asyncio.wait_for(self.call(plugin, "m1"), 0.3))
        self.assertFalse(plugin._hub_group_batches)
        plugin._llm_call.assert_not_awaited()

    def test_context_compaction_is_bounded_and_keeps_protected_queue(self):
        payload = {
            "current_message": "当前完整请求" * 100,
            "recent_messages": [
                {"text": "历史" * 500, "message_id": str(i)} for i in range(12)
            ],
            "group_participation": {
                "bot_replies_in_window": 9,
                "recent_bot_replies": [{"text": "回复" * 500} for _ in range(12)],
            },
            "platform_scene": {
                "pending_requests": [
                    {"event_id": "task", "protected": True, "text": "任务要求" * 200},
                    {"event_id": "chat", "protected": False, "text": "闲聊" * 500},
                ]
            },
        }
        original = deepcopy(payload)
        result = optimization.compact_hub_payload(payload, 6)
        self.assertEqual(original, payload)
        self.assertEqual(payload["current_message"], result["current_message"])
        self.assertLessEqual(len(result["recent_messages"]), 6)
        self.assertLessEqual(
            len(result["group_participation"]["recent_bot_replies"]), 3
        )
        self.assertEqual(9, result["group_participation"]["bot_replies_in_window"])
        self.assertEqual(
            payload["platform_scene"]["pending_requests"][0],
            result["platform_scene"]["pending_requests"][0],
        )
        self.assertLess(len(json.dumps(result)), len(json.dumps(payload)) / 2)

    def test_persona_summary_keeps_identity_boundaries_and_emotion_without_changing_source(
        self,
    ):
        text = (
            "你是小七，性格嘴硬。\n"
            + "背景是花园里的普通一天。" * 100
            + "\n被挑逗会破防，然后找补继续嘴硬。\n群聊不抢其他机器人的话，禁止乱认人。"
        )
        result = optimization.summarize_hub_persona(text)
        self.assertLessEqual(len(result), 500)
        self.assertIn("小七", result)
        self.assertIn("破防", result)
        self.assertIn("不抢", result)
        self.assertIn("背景是花园", text)

    def test_semantic_stamp_ignores_clock_but_detects_content_queue_and_identity_changes(
        self,
    ):
        one = {
            "current_time": "10:00",
            "current_message": "你好",
            "platform_scene": {
                "pending_requests": [
                    {"event_id": "m0", "text": "前文", "waiting_seconds": 1}
                ],
            },
            "bot_identity": {"name": "小七"},
        }
        two = deepcopy(one)
        two["current_time"] = "10:01"
        two["platform_scene"]["pending_requests"][0]["waiting_seconds"] = 2
        self.assertEqual(
            optimization.hub_semantic_stamp(one), optimization.hub_semantic_stamp(two)
        )
        for key, value in [
            ("current_message", "重试刚才的任务"),
            ("bot_identity", {"name": "晚柠"}),
        ]:
            changed = {**two, key: value}
            self.assertNotEqual(
                optimization.hub_semantic_stamp(one),
                optimization.hub_semantic_stamp(changed),
            )
        two["platform_scene"]["pending_requests"][0]["text"] = "新补充"
        self.assertNotEqual(
            optimization.hub_semantic_stamp(one), optimization.hub_semantic_stamp(two)
        )

    async def test_judge_compaction_and_persona_switches_control_model_inputs(self):
        from dataclasses import replace

        from astrbot_plugin_private_companion.capability_hub import judge_capabilities

        original = "你是小七，性格嘴硬，被挑逗会破防。\n" + "普通背景。" * 150
        recent = [
            {"scope": "group:g1", "text": "前文" * 200, "sender_id": "u1"}
            for _ in range(12)
        ]
        context = build_capability_context(
            scope="group:g1",
            current_text="你好",
            sender_id="u1",
            sender_name="甲",
            recent_messages=recent,
            available_capabilities=[],
            max_recent=12,
            now=datetime.now(timezone.utc),
        )
        for enabled in (True, False):
            plugin = self.plugin(False)
            plugin.enable_hub_context_compaction = enabled
            plugin.enable_hub_persona_summary = enabled
            plugin.hub_recent_message_limit = 6
            plugin.enable_short_term_emotion = True
            plugin._task_provider = lambda value: "hub"
            await judge_capabilities(
                plugin,
                replace(context),
                available_capabilities=[],
                persona_prompt=original,
            )
            prompt = plugin._llm_call.await_args.args[0]
            payload, _ = json.JSONDecoder().raw_decode(
                prompt.split("输入数据：\n", 1)[1]
            )
            persona = json.loads(prompt.rsplit("\n", 1)[-1])["current_persona"]
            self.assertEqual(6 if enabled else 12, len(payload["recent_messages"]))
            if enabled:
                self.assertLessEqual(len(persona), 500)
                self.assertIn("破防", persona)
            else:
                self.assertEqual(original, persona)

    async def test_same_event_reuse_detects_changed_text_and_protection(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin = self.plugin(False)
        plugin.enable_hub_decision_reuse = True
        plugin._event_scope_key = lambda event: "group:g1"
        plugin._extract_group_id_from_event = lambda event: "g1"
        plugin._get_group = lambda group: {}
        plugin._task_provider = lambda value: "hub"
        plugin._message_debounce_pending_llm = {
            "group:g1:__hub_pending__": {"active_events": {}}
        }
        event = SimpleNamespace(
            message_str="你好",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_group_scene={"trigger": "group_message"},
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "甲",
        )
        await append_capability_guidance(plugin, event, None)
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(1, plugin._llm_call.await_count)
        decided_ts = event._private_companion_hub_decided_ts - 8
        event._private_companion_hub_decided_ts = decided_ts
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(decided_ts, event._private_companion_hub_decided_ts)
        event.private_companion_delayed_image_sources = ["image"]
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(2, plugin._llm_call.await_count)
        event.message_obj.raw_message = [{"type": "record"}]
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(3, plugin._llm_call.await_count)
        task = {"task_id": "t1", "status": "running"}
        plugin._conversation_task_states = SimpleNamespace(
            summary_for_session=lambda *args, **kwargs: dict(task)
        )
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(4, plugin._llm_call.await_count)
        task["status"] = "failed"
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(5, plugin._llm_call.await_count)
        event.message_str = "取消任务"
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(6, plugin._llm_call.await_count)
        plugin.enable_hub_decision_reuse = False
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(7, plugin._llm_call.await_count)

    def reuse_case(self, *, message_id="m1", trigger="group_message"):
        plugin = self.plugin(False)
        plugin.enable_hub_decision_reuse = True
        plugin._event_scope_key = lambda event: "group:g1"
        plugin._extract_group_id_from_event = lambda event: "g1"
        plugin._get_group = lambda group: {}
        plugin._task_provider = lambda value: "hub"
        plugin._message_debounce_pending_llm = {
            "group:g1:__hub_pending__": {"active_events": {}}
        }
        event = SimpleNamespace(
            message_str="你呢",
            message_obj=SimpleNamespace(message_id=message_id),
            private_companion_group_scene={"trigger": trigger},
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "甲",
        )
        return plugin, event

    async def test_hub_failure_is_rejudged_on_the_same_event_with_reuse_on_or_off(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        for enabled in (False, True):
            with self.subTest(reuse=enabled):
                plugin, event = self.reuse_case(trigger="at_bot")
                plugin.enable_hub_decision_reuse = enabled
                plugin._llm_call.return_value = "not-json"
                await append_capability_guidance(plugin, event, None)
                failed = event._private_companion_capability_decision
                self.assertEqual("hub_unavailable", failed.reason)
                self.assertEqual((), failed.actions)
                plugin._llm_call.return_value = '{"reply":true,"actions":["search"]}'
                await append_capability_guidance(plugin, event, None)
                recovered = event._private_companion_capability_decision
                self.assertEqual(("search",), recovered.actions)
                self.assertNotEqual("hub_unavailable", recovered.reason)
                self.assertEqual(3, plugin._llm_call.await_count)
                await append_capability_guidance(plugin, event, None)
                self.assertEqual(3, plugin._llm_call.await_count)

    async def test_new_available_tool_rejudges_but_removal_only_filters_actions(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(trigger="at_bot")
        event.message_str = "查一下天气"
        plugin._llm_call.return_value = '{"reply":true,"actions":["search"]}'
        request = SimpleNamespace(system_prompt="", func_tool=None)
        await append_capability_guidance(plugin, event, request)
        self.assertEqual((), event._private_companion_capability_decision.actions)
        request.func_tool = SimpleNamespace(tools=[SimpleNamespace(name="AIsearch")])
        await append_capability_guidance(plugin, event, request)
        self.assertEqual(("search",), event._private_companion_capability_decision.actions)
        self.assertEqual(2, plugin._llm_call.await_count)
        await append_capability_guidance(plugin, event, request)
        self.assertEqual(2, plugin._llm_call.await_count)
        request.func_tool = None
        await append_capability_guidance(plugin, event, request)
        self.assertEqual((), event._private_companion_capability_decision.actions)
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_successful_correction_is_reused_without_a_second_task_judgment(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(trigger="at_bot")
        plugin._llm_call.side_effect = [
            "not-json", '{"reply":true,"actions":["search"],"route":"task"}',
        ]
        await append_capability_guidance(plugin, event, None)
        decision = event._private_companion_capability_decision
        self.assertEqual("task", decision.route)
        request = SimpleNamespace(
            system_prompt="", func_tool=SimpleNamespace(tools=[SimpleNamespace(name="AIsearch")]),
        )
        await append_capability_guidance(plugin, event, request)
        self.assertIs(decision, request._private_companion_capability_decision)
        self.assertEqual(("search",), decision.actions)
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_missing_batch_item_repairs_only_that_original_message(self):
        from astrbot_plugin_private_companion.capability_hub import judge_capabilities

        plugin = self.plugin()
        plugin._llm_call.side_effect = [
            '{"decisions":[{"message_id":"m1","reply":false,"actions":[]}]}',
            '{"reply":true,"actions":[]}',
        ]
        first, second = await asyncio.gather(*(
            judge_capabilities(
                plugin, self.context(), available_capabilities=(),
                scene={"message_id": identity, "trigger": "group_message", "sender_identity": {"kind": "human"}},
            )
            for identity in ("m1", "m2")
        ))
        self.assertFalse(first.reply)
        self.assertTrue(second.reply)
        self.assertEqual(2, plugin._llm_call.await_count)
        correction_prompt = plugin._llm_call.await_args.args[0]
        self.assertIn('"message_id":"m2"', correction_prompt)
        self.assertNotIn('"message_id":"m1"', correction_prompt)

    async def test_preflight_capability_removal_keeps_one_successful_request(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(trigger="at_bot")
        plugin._llm_call.return_value = '{"reply":true,"actions":["search"]}'
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(("search",), event._private_companion_capability_decision.actions)
        request = SimpleNamespace(system_prompt="", func_tool=None)
        await append_capability_guidance(plugin, event, request)
        self.assertEqual((), event._private_companion_capability_decision.actions)
        plugin._llm_call.assert_awaited_once()

    async def test_event_snapshot_failure_omits_context_without_granting_workspace(self):
        from pathlib import Path
        from unittest.mock import Mock, patch

        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )
        from astrbot_plugin_private_companion.task_permissions import TaskActor

        plugin, event = self.reuse_case(trigger="at_bot")
        event.unified_msg_origin = "bot1:GroupMessage:g1"
        plugin._persistent_task_runtime = SimpleNamespace(
            workspace=Path("C:/AstrBotTasks"),
            actor=lambda _: TaskActor("bot1", "g1", "u1", False),
            permissions=SimpleNamespace(check=Mock(return_value=False)),
            snapshot=Mock(side_effect=RuntimeError("api_key=private-value")),
            snapshot_for_scope=Mock(return_value=""),
        )
        plugin._llm_call.return_value = '{"reply":true,"actions":["workspace"]}'
        with patch("astrbot.api.logger.warning") as warning:
            await append_capability_guidance(plugin, event, None)
        decision = getattr(event, "_private_companion_capability_decision", None)
        self.assertIsNotNone(decision)
        self.assertTrue(decision.reply)
        self.assertEqual((), decision.actions)
        plugin._llm_call.assert_awaited_once()
        self.assertIn("RuntimeError", str(warning.call_args))
        self.assertNotIn("private-value", str(warning.call_args))

    async def test_first_group_judgment_gets_recipient_before_any_request(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        del event.private_companion_group_scene
        scene = {
            "trigger": "at_bot",
            "talking_to": "bot",
            "at_targets": [{"user_id": "bot1", "is_bot": True}],
        }
        plugin._infer_group_scene = lambda *args, **kwargs: deepcopy(scene)
        await append_capability_guidance(plugin, event, None)
        payload = json.loads(
            plugin._llm_call.await_args.args[0].split("输入数据：\n", 1)[1]
        )
        self.assertEqual("at_bot", payload["platform_scene"].get("trigger"))
        event.private_companion_group_scene = deepcopy(scene)
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(1, plugin._llm_call.await_count)

    async def test_bare_mention_keeps_same_speaker_reference_before_later_arrivals(
        self,
    ):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(trigger="at_bot")
        event.message_str = "小七"
        event._private_companion_bot_identity = {"name": "小七"}
        plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
        now = datetime.now(timezone.utc).timestamp()
        group = {
            "recent_messages": [
                {
                    "message_id": "question",
                    "sender_id": "u1",
                    "text": "明天BLG和TES谁赢？",
                    "ts": now - 3,
                },
                {
                    "message_id": "other",
                    "sender_id": "u2",
                    "text": "今天吃了火锅",
                    "ts": now - 1,
                },
            ]
        }
        plugin._get_group = lambda _: group
        await append_capability_guidance(plugin, event, None)
        group["recent_messages"].append(
            {
                "message_id": "later",
                "sender_id": "u1",
                "text": "之后再聊电影",
                "ts": now + 10,
            }
        )
        request = SimpleNamespace(prompt="小七", system_prompt="")
        await append_capability_guidance(plugin, event, request)
        self.assertIn("明天BLG和TES谁赢？", request.prompt)
        self.assertNotIn("今天吃了火锅", request.prompt)
        self.assertNotIn("之后再聊电影", request.prompt)
        self.assertIn("u1", request.prompt)
        self.assertEqual(1, plugin._llm_call.await_count)

    async def test_bare_mention_does_not_replay_answered_old_or_other_speaker_text(
        self,
    ):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        for mode in ("answered", "old", "other", "substantive"):
            with self.subTest(mode=mode):
                plugin, event = self.reuse_case(trigger="at_bot")
                event.message_str = (
                    "小七，帮我看新问题" if mode == "substantive" else "小七"
                )
                event._private_companion_bot_identity = {"name": "小七"}
                plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
                now = datetime.now(timezone.utc).timestamp()
                group = {
                    "recent_messages": [
                        {
                            "message_id": "previous",
                            "sender_id": "u2" if mode == "other" else "u1",
                            "text": "不要重新回答的旧问题",
                            "ts": now - (180 if mode == "old" else 3),
                        }
                    ]
                }
                if mode == "answered":
                    group["confirmed_bot_replies"] = [
                        {"sender_id": "u1", "text": "已经回答", "ts": now - 1}
                    ]
                plugin._get_group = lambda _: group
                request = SimpleNamespace(prompt=event.message_str, system_prompt="")
                await append_capability_guidance(plugin, event, request)
                self.assertNotIn("不要重新回答的旧问题", request.prompt)
                self.assertIn("本轮回答对象", request.prompt)

    async def test_bare_mention_survives_scene_hints_and_missing_reply_history(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        for mode in ("missing_history", "derived_scene"):
            with self.subTest(mode=mode):
                plugin, event = self.reuse_case(
                    trigger="group_wakeup_direct_word"
                    if mode == "derived_scene"
                    else "at_bot"
                )
                event.message_str = "小七"
                event._private_companion_bot_identity = {"name": "小七"}
                plugin._event_scene_signals = lambda _: {
                    "at_targets": [{"is_bot": True}]
                }
                plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
                group = {
                    "recent_messages": [
                        {
                            "message_id": "question",
                            "sender_id": "u1",
                            "text": "明天BLG和TES谁赢？",
                            "ts": datetime.now(timezone.utc).timestamp() - 3,
                        }
                    ],
                    "confirmed_bot_replies": None,
                }
                plugin._get_group = lambda _: group
                request = SimpleNamespace(prompt="小七", system_prompt="")
                await append_capability_guidance(plugin, event, request)
                self.assertIn("明天BLG和TES谁赢？", request.prompt)
                self.assertEqual(1, plugin._llm_call.await_count)

    async def test_own_long_official_id_is_not_a_new_pending_request(self):
        from astrbot_plugin_private_companion.helpers import _single_line
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(message_id="ROBOT1.0_" + "x" * 180)
        plugin._message_debounce_event_id = lambda current: _single_line(
            current.message_obj.message_id, 120
        )
        await append_capability_guidance(plugin, event, None)
        pending = plugin._message_debounce_pending_llm["group:g1:__hub_pending__"]
        pending["active_events"][plugin._message_debounce_event_id(event)] = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "text": event.message_str,
            "sender_id": "u1",
        }
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertEqual((), event._private_companion_hub_queue_stamp)

    async def test_request_tool_filter_does_not_repeat_the_semantic_judgment(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        plugin._llm_call.return_value = '{"reply":true,"actions":["search"]}'
        await append_capability_guidance(plugin, event, None)
        request = SimpleNamespace(system_prompt="", func_tool=SimpleNamespace(tools=[]))
        await append_capability_guidance(plugin, event, request)
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertNotIn(
            "search", request._private_companion_capability_decision.actions
        )

    async def test_wakeup_explanation_and_fatigue_do_not_repeat_judgment(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case(trigger="at_bot")
        await append_capability_guidance(plugin, event, None)
        event.private_companion_group_scene.update(
            reason="explicit_at_bot",
            wakeup_strength="strong",
            wakeup_fatigue={"count": 1},
        )
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(1, plugin._llm_call.await_count)

    async def test_unchanged_event_survives_slow_preparation_without_new_call(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        await append_capability_guidance(plugin, event, None)
        event._private_companion_hub_decided_ts -= 120
        await append_capability_guidance(plugin, event, None, force_rejudge=True)
        self.assertEqual(1, plugin._llm_call.await_count)

    async def test_changed_message_identity_never_reuses_another_turn(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        await append_capability_guidance(plugin, event, None)
        event.message_obj.message_id = "m2"
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_real_queue_changes_remain_visible_with_reuse_enabled(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
        active = plugin._message_debounce_pending_llm["group:g1:__hub_pending__"][
            "active_events"
        ]
        active["other"] = {
            "ts": datetime.now(timezone.utc).timestamp(),
            "text": "另一条真实请求",
            "sender_id": "u2",
        }
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(1, plugin._llm_call.await_count)
        active["other"]["text"] = "已观察请求的真实补充"
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(2, plugin._llm_call.await_count)
        active["other"]["protected"] = True
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(3, plugin._llm_call.await_count)
        active["other"]["send_started"] = True
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(4, plugin._llm_call.await_count)

    async def test_later_history_and_reply_density_do_not_resurrect_old_message(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        event.message_str = "改好了，我测试一下"
        group = {"recent_messages": [], "confirmed_bot_replies": []}
        plugin._get_group = lambda _: group
        await append_capability_guidance(plugin, event, None)
        plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
        group["recent_messages"].append(
            {
                "message_id": "new-at",
                "sender_id": "u1",
                "text": "@小七",
                "ts": datetime.now(timezone.utc).timestamp(),
            }
        )
        await append_capability_guidance(plugin, event, None)
        group["confirmed_bot_replies"].append(
            {
                "text": "在呢",
                "sender_id": "u1",
                "ts": datetime.now(timezone.utc).timestamp(),
            }
        )
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertFalse(event._private_companion_capability_decision.reply)

    async def test_later_normal_pending_requests_have_their_own_judgments(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        for reply in (True, False):
            with self.subTest(reply=reply):
                plugin, event = self.reuse_case()
                plugin._llm_call.return_value = json.dumps(
                    {"reply": reply, "actions": []}
                )
                await append_capability_guidance(plugin, event, None)
                active = plugin._message_debounce_pending_llm[
                    "group:g1:__hub_pending__"
                ]["active_events"]
                active["new-at"] = {
                    "text": "@另一个机器人",
                    "sender_id": "u2",
                    "ts": datetime.now(timezone.utc).timestamp(),
                }
                await append_capability_guidance(plugin, event, None)
                active["new-at"]["send_started"] = True
                await append_capability_guidance(plugin, event, None)
                active.clear()
                await append_capability_guidance(plugin, event, None)
                self.assertEqual(1, plugin._llm_call.await_count)
                self.assertEqual(
                    reply, event._private_companion_capability_decision.reply
                )

    async def test_new_protected_task_still_rejudges_an_active_reply(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        plugin._llm_call.return_value = '{"reply":true,"actions":[]}'
        await append_capability_guidance(plugin, event, None)
        active = plugin._message_debounce_pending_llm["group:g1:__hub_pending__"][
            "active_events"
        ]
        active["task"] = {
            "text": "正在执行的任务",
            "sender_id": "u2",
            "protected": True,
            "ts": datetime.now(timezone.utc).timestamp(),
        }
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_derived_flow_hints_do_not_reinterpret_the_same_message(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        await append_capability_guidance(plugin, event, None)
        event.private_companion_group_scene.update(
            trigger="reply_in_flow", talking_to="u2"
        )
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(1, plugin._llm_call.await_count)

    async def test_arrival_during_inflight_judgment_does_not_repeat_old_turn(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        event.message_str = "改好了，我测试一下"
        group = {"recent_messages": []}
        plugin._get_group = lambda _: group
        started, release = asyncio.Event(), asyncio.Event()

        async def model(*args, **kwargs):
            started.set()
            await release.wait()
            return '{"reply":false,"actions":[]}'

        plugin._llm_call.side_effect = model
        first = asyncio.create_task(append_capability_guidance(plugin, event, None))
        await asyncio.wait_for(started.wait(), 2)
        group["recent_messages"].append(
            {
                "message_id": "later-at",
                "text": "@小七",
                "sender_id": "u1",
                "ts": datetime.now(timezone.utc).timestamp(),
            }
        )
        plugin._message_debounce_pending_llm["group:g1:__hub_pending__"][
            "active_events"
        ]["later-at"] = {
            "text": "@小七",
            "sender_id": "u1",
            "ts": datetime.now(timezone.utc).timestamp(),
        }
        release.set()
        await asyncio.wait_for(first, 2)
        request = SimpleNamespace(system_prompt="", func_tool=SimpleNamespace(tools=[]))
        await append_capability_guidance(plugin, event, request)
        self.assertEqual(1, plugin._llm_call.await_count)
        self.assertFalse(request._private_companion_capability_decision.reply)

    async def test_bot_identity_and_new_events_have_independent_decisions(self):
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        plugin, event = self.reuse_case()
        event.unified_msg_origin = "bot1:GroupMessage:g1"
        event._private_companion_bot_identity = {"name": "小七"}
        await append_capability_guidance(plugin, event, None)
        event.unified_msg_origin = "bot2:GroupMessage:g1"
        event._private_companion_bot_identity = {"name": "晚柠"}
        await append_capability_guidance(plugin, event, None)
        self.assertEqual(2, plugin._llm_call.await_count)
        _, second = self.reuse_case()
        await append_capability_guidance(plugin, second, None)
        self.assertEqual(3, plugin._llm_call.await_count)

    async def test_live_six_event_pattern_needs_six_calls_across_all_hooks(self):
        from astrbot_plugin_private_companion.group_wakeup import GroupWakeupMixin
        from astrbot_plugin_private_companion.helpers import _single_line
        from astrbot_plugin_private_companion.passive_state_pipeline import (
            append_capability_guidance,
        )

        cases = [
            ("晚柠", "这性格要是能是男的就有点恐怖了", False, False),
            ("小七", "这性格要是能是男的就有点恐怖了", False, False),
            ("小七", "新脑子好不好使，给你精简了上下文", False, True),
            ("晚柠", "本小姐本来就灵光，省下的算你赚到了！", True, False),
            ("晚柠", "你呢", False, True),
            ("小七", "我可不傲娇，安安静静陪着大家就好～", True, False),
        ]
        plugin, _ = self.reuse_case()
        group = {"recent_messages": []}
        plugin._get_group = lambda group_id: group
        plugin._infer_group_scene = GroupWakeupMixin._infer_group_scene.__get__(plugin)
        plugin._event_scene_signals = lambda event: event.signals
        plugin._message_debounce_event_id = lambda event: _single_line(
            event.message_obj.message_id, 120
        )
        for index, (name, text, bot_sender, addressed) in enumerate(cases, 1):
            with self.subTest(persona=name, message=text):
                plugin.bot_name = name
                group.clear()
                group["recent_messages"] = []
                plugin._message_debounce_pending_llm["group:g1:__hub_pending__"][
                    "active_events"
                ].clear()
                plugin._llm_call.return_value = json.dumps(
                    {"reply": addressed, "actions": []}
                )
                _, event = self.reuse_case(message_id=f"ROBOT1.0_{index}_" + "x" * 180)
                del event.private_companion_group_scene
                event.message_str = text
                event.message_obj.author = {"bot": bot_sender}
                event.unified_msg_origin = f"{name}:GroupMessage:g1"
                event._private_companion_bot_identity = {"name": name}
                event.signals = {
                    "self_id": "bot1",
                    "at_targets": [{"user_id": "bot1", "name": name, "is_bot": True}]
                    if addressed
                    else [],
                    "reply_to_id": "",
                    "at_all": False,
                }
                await append_capability_guidance(plugin, event, None)
                event.private_companion_group_scene.update(
                    reason="scene_ready", wakeup_fatigue={"count": 1}
                )
                group["recent_messages"].append(
                    {
                        "message_id": plugin._message_debounce_event_id(event),
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "sender_id": "u1",
                        "text": text,
                    }
                )
                await append_capability_guidance(plugin, event, None)
                plugin._message_debounce_pending_llm["group:g1:__hub_pending__"][
                    "active_events"
                ][plugin._message_debounce_event_id(event)] = {
                    "text": text,
                    "sender_id": "u1",
                    "ts": datetime.now(timezone.utc).timestamp(),
                }
                request = SimpleNamespace(
                    system_prompt="", func_tool=SimpleNamespace(tools=[])
                )
                await append_capability_guidance(plugin, event, request)
                self.assertEqual(index, plugin._llm_call.await_count)
                self.assertEqual(
                    addressed, request._private_companion_capability_decision.reply
                )

    def test_page_exposes_four_flags_and_applies_runtime_changes(self):
        from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi

        plugin = SimpleNamespace(config={})
        api = PrivateCompanionPageApi(plugin)
        for key in (
            "enable_hub_group_batching",
            "enable_hub_context_compaction",
            "enable_hub_persona_summary",
            "enable_hub_decision_reuse",
        ):
            with self.subTest(key=key):
                self.assertIn(key, api._allowed_feature_keys())
                self.assertIn(key, api._schema_bool_keys())
                api._apply_config_value(key, True)
                self.assertTrue(getattr(plugin, key))
                self.assertTrue(api._feature_flags()[key])
                api._apply_config_value(key, False)
                self.assertFalse(getattr(plugin, key))
                self.assertFalse(api._feature_flags()[key])

    def test_page_numeric_settings_have_safe_bounds(self):
        from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi

        api = PrivateCompanionPageApi(SimpleNamespace(config={}))
        self.assertEqual(
            5.0, api._normalize_setting_value("hub_group_batch_seconds", 999)
        )
        self.assertEqual(
            0.0, api._normalize_setting_value("hub_group_batch_seconds", -1)
        )
        self.assertEqual(
            2.5, api._normalize_setting_value("hub_group_batch_seconds", "nan")
        )
        self.assertEqual(
            12, api._normalize_setting_value("hub_recent_message_limit", 999)
        )
        self.assertEqual(
            2, api._normalize_setting_value("hub_recent_message_limit", -1)
        )


if __name__ == "__main__":
    unittest.main()
