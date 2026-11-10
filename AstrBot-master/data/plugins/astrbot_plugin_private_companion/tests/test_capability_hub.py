from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_private_companion.capability_hub import (
    build_capability_context,
    capability_allowed,
    decide_capabilities,
    decision_action_allowed,
    explicit_singing_request,
    format_capability_guidance,
    judge_capabilities,
    parse_model_capability_decision,
)
from astrbot_plugin_private_companion.passive_state_pipeline import (
    append_capability_guidance,
    gate_request_search_tools,
)


class CapabilityHubTests(unittest.TestCase):
    def test_frontend_management_flag_preserves_chat_but_not_mixed_execution(self):
        context = self._context("归档历史任务，保留明早提醒")
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "route": "chat", "actions": [], "task_manage": True}),
            available_capabilities=("schedule", "workspace"), context=context,
        )
        self.assertTrue(getattr(decision, "task_manage", False))
        mixed = parse_model_capability_decision(
            json.dumps({"reply": True, "route": "mixed", "actions": ["workspace"], "task_manage": True,
                        "segments": [{"kind": "task", "text": "运行新脚本"}]}),
            available_capabilities=("schedule", "workspace"), context=context,
        )
        self.assertFalse(mixed.task_manage)
        self.assertEqual("mixed", mixed.route)

    def _context(self, text: str, *, recent=(), scope="group:g1"):
        return build_capability_context(
            scope=scope,
            current_text=text,
            sender_id="u1",
            sender_name="小明",
            now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc),
            previous_message_at=datetime(2026, 9, 28, 11, 59, 50, tzinfo=timezone.utc),
            recent_messages=recent,
        )

    def test_task_mode_distinguishes_explicit_text_from_execution_without_actions(self):
        context = self._context("已经调整好了，你可以试试", scope="private:u1")
        for value, expected in ((None, "tools"), ("tools", "tools"), ("reasoning", "reasoning"), ("invalid", "tools"), ({}, "tools")):
            with self.subTest(value=value):
                payload = {"reply": True, "route": "task", "actions": []}
                if value is not None:
                    payload["task_mode"] = value
                decision = parse_model_capability_decision(
                    json.dumps(payload), available_capabilities=("workspace", "schedule"), context=context,
                )
                self.assertEqual(expected, decision.task_mode)

    def test_concrete_execution_action_overrides_conflicting_reasoning_mode(self):
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "route": "task", "actions": ["workspace"], "task_mode": "reasoning"}),
            available_capabilities=("workspace",), context=self._context("运行脚本"),
        )
        self.assertEqual("tools", decision.task_mode)

    def test_chat_mode_does_not_inherit_task_reasoning_restriction(self):
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "route": "chat", "actions": [], "task_mode": "reasoning"}),
            available_capabilities=(), context=self._context("你好"),
        )
        self.assertEqual("", decision.task_mode)

    def test_decision_uses_one_deduplicated_action_list(self) -> None:
        decision = decide_capabilities(
            self._context("帮我看看这张图，顺便查一下今天的天气"),
            available_capabilities={"vision", "search", "emoji", "voice"},
        )

        self.assertTrue(decision.reply)
        self.assertEqual(("vision", "search"), decision.actions)
        self.assertNotIn("voice", decision.actions)
        self.assertFalse(decision.merge_messages)

    def test_model_decision_parses_actions_tone_and_burst(self) -> None:
        decision = parse_model_capability_decision(
            '{"reply":true,"actions":["emoji","unknown"],'
            '"tone":"轻松调侃","emotion":"开心",'
            '"merge_messages":true,"reason":"同话题连续消息"}',
            available_capabilities={"emoji", "vision"},
            context=self._context("大家都在聊这个梗"),
        )

        self.assertIsNotNone(decision)
        self.assertTrue(decision.reply)
        self.assertEqual(("emoji",), decision.actions)
        self.assertEqual("轻松调侃", decision.reply_style)
        self.assertEqual("开心", decision.emotion)
        self.assertTrue(decision.merge_messages)

    def test_model_decision_bounds_reply_candidate_and_clears_it_when_not_replying(self) -> None:
        candidate = "接住群友刚才的问题"
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "actions": [], "reply_candidate": candidate}, ensure_ascii=False),
            available_capabilities=(),
            context=self._context("这个怎么用？"),
        )
        self.assertIsNotNone(decision)
        self.assertEqual(candidate, decision.reply_candidate)

        long_decision = parse_model_capability_decision(
            json.dumps({"reply": True, "actions": [], "reply_candidate": "甲" * 300}, ensure_ascii=False),
            available_capabilities=(),
            context=self._context("这个怎么用？"),
        )
        self.assertIsNotNone(long_decision)
        self.assertLessEqual(len(long_decision.reply_candidate), 240)

        non_string_decision = parse_model_capability_decision(
            '{"reply":true,"actions":[],"reply_candidate":{"text":"不应保留"}}',
            available_capabilities=(),
            context=self._context("这个怎么用？"),
        )
        self.assertIsNotNone(non_string_decision)
        self.assertEqual("", non_string_decision.reply_candidate)

        silent_decision = parse_model_capability_decision(
            '{"reply":false,"actions":[],"reply_candidate":"不应发送"}',
            available_capabilities=(),
            context=self._context("这个怎么用？"),
        )
        self.assertIsNotNone(silent_decision)
        self.assertEqual("", silent_decision.reply_candidate)

    def test_model_decision_rejects_image_edit_without_explicit_model_intent(self) -> None:
        decision = parse_model_capability_decision(
            '{"reply":true,"actions":["image_edit"],'
            '"image_edit_requested":false,"tone":"自然"}',
            available_capabilities={"image_edit"},
            context=self._context("这张图怎么样"),
        )

        self.assertIsNotNone(decision)
        self.assertNotIn("image_edit", decision.actions)

    def test_model_decision_bad_json_returns_none(self) -> None:
        self.assertIsNone(
            parse_model_capability_decision(
                "不是 JSON",
                available_capabilities={"emoji"},
                context=self._context("你好"),
            )
        )

    def test_reference_intent_is_retained_only_for_approved_image_actions(self) -> None:
        for reply, actions, intent, expected in (
            (True, ["image_generate"], True, True),
            (True, ["image_edit"], True, True),
            (True, ["vision"], True, False),
            (True, [], True, False),
            (False, ["image_generate"], True, False),
            (True, ["image_generate"], "true", False),
            (True, ["image_generate"], 1, False),
            (True, ["image_generate"], False, False),
        ):
            with self.subTest(reply=reply, actions=actions, intent=intent):
                decision = parse_model_capability_decision(
                    json.dumps({"reply": reply, "actions": actions,
                                "image_edit_requested": intent}),
                    available_capabilities={"image_generate", "image_edit", "vision"},
                    context=self._context("按刚刚要求重新生图", scope="private:u1"),
                )
                self.assertIsNotNone(decision)
                self.assertIs(expected, getattr(decision, "image_edit_requested", False))

        unavailable = parse_model_capability_decision(
            '{"reply":true,"actions":["image_generate"],"image_edit_requested":true}',
            available_capabilities={"vision"}, context=self._context("再试试"),
        )
        self.assertFalse(getattr(unavailable, "image_edit_requested", False))

    def test_request_scoped_decision_is_authoritative_for_external_actions(self) -> None:
        decision = decide_capabilities(
            self._context("请发一个表情包"),
            available_capabilities=("emoji", "image_edit"),
        )
        self.assertEqual(decision.actions, ("emoji",))
        self.assertTrue(decision_action_allowed(decision, "emoji"))
        self.assertFalse(decision_action_allowed(decision, "image_edit"))
        self.assertTrue(decision_action_allowed(None, "image_edit"))

    def test_decision_only_edits_when_user_requests_image_change(self) -> None:
        decision = decide_capabilities(
            self._context("这张图挺好看的"),
            available_capabilities={"vision", "image_edit"},
        )
        self.assertNotIn("image_edit", decision.actions)

        edit = decide_capabilities(
            self._context("把这张图的背景改成夜景"),
            available_capabilities={"vision", "image_edit"},
        )
        self.assertIn("image_edit", edit.actions)

    def test_visual_followup_and_original_refresh_select_vision(self) -> None:
        for text in ("这张图是什么", "重新看一下原图", "再仔细看一下刚才的照片"):
            with self.subTest(text=text):
                decision = decide_capabilities(
                    self._context(text), available_capabilities={"vision"}
                )
                self.assertIn("vision", decision.actions)

    def test_current_image_without_text_selects_vision(self) -> None:
        decision = decide_capabilities(
            self._context(""),
            available_capabilities={"vision"},
            has_current_image=True,
        )
        self.assertTrue(decision.reply)
        self.assertEqual(("vision",), decision.actions)

    def test_inbound_voice_is_context_only_not_outbound_voice_request(self) -> None:
        context = build_capability_context(
            scope="private:u1",
            current_text="",
            sender_id="u1",
            sender_name="小明",
            now=datetime(2026, 9, 28, 12, tzinfo=timezone.utc),
            has_current_voice=True,
        )
        decision = decide_capabilities(
            context,
            available_capabilities={"voice", "vision"},
        )
        self.assertTrue(decision.reply)
        self.assertEqual((), decision.actions)
        self.assertTrue(context.has_current_voice)

    def test_explicit_singing_request_selects_voice_and_not_music(self) -> None:
        decision = decide_capabilities(
            self._context("唱首歌给我听"),
            available_capabilities={"voice", "music"},
        )

        self.assertEqual(("voice",), decision.actions)
        self.assertEqual("explicit_singing_request", decision.reason)

    def test_bare_singing_request_selects_voice(self) -> None:
        decision = decide_capabilities(
            self._context("唱"),
            available_capabilities={"voice", "music"},
        )

        self.assertEqual(("voice",), decision.actions)

    def test_singing_retry_phrases_keep_the_singing_intent(self) -> None:
        for text in ("重新唱一下", "再唱一遍", "刚刚没唱出来，再来一次"):
            with self.subTest(text=text):
                self.assertTrue(explicit_singing_request(text))
                decision = decide_capabilities(
                    self._context(text),
                    available_capabilities={"voice", "music"},
                )
                self.assertEqual(("voice",), decision.actions)
                self.assertEqual("explicit_singing_request", decision.reason)

    def test_music_playback_request_selects_music_and_not_voice(self) -> None:
        for text in ("来一首歌", "放首歌", "播放音乐", "听歌"):
            with self.subTest(text=text):
                decision = decide_capabilities(
                    self._context(text),
                    available_capabilities={"voice", "music"},
                )

                self.assertEqual(("music",), decision.actions)
                self.assertEqual("music_playback_request", decision.reason)

    def test_song_mention_and_non_singing_terms_do_not_trigger_media_actions(self) -> None:
        for text in ("我喜欢这首歌", "唱片推荐一下", "这个唱腔很特别", "别跟我唱反调"):
            with self.subTest(text=text):
                decision = decide_capabilities(
                    self._context(text),
                    available_capabilities={"voice", "music"},
                )

                self.assertEqual((), decision.actions)

    def test_model_cannot_cross_route_explicit_media_intent(self) -> None:
        singing = parse_model_capability_decision(
            '{"reply":true,"actions":["music"],"tone":"自然"}',
            available_capabilities={"voice", "music"},
            context=self._context("唱首歌给我听"),
        )
        playback = parse_model_capability_decision(
            '{"reply":true,"actions":["voice"],"tone":"自然"}',
            available_capabilities={"voice", "music"},
            context=self._context("来一首歌"),
        )

        self.assertEqual(("voice",), singing.actions)
        self.assertEqual(("music",), playback.actions)

    def test_decision_merges_cross_sender_burst_only_after_pending_reply(self) -> None:
        decision = decide_capabilities(
            self._context("那就继续说", recent=(
                {"scope": "group:g1", "sender_id": "u2", "text": "小七怎么看"},
                {"scope": "group:g1", "sender_id": "u3", "text": "我也想知道"},
            )),
            available_capabilities=(),
            pending_reply=True,
            burst_count=3,
        )
        self.assertTrue(decision.merge_messages)
        self.assertTrue(decision.reply)

    def test_decision_does_not_merge_unrelated_message_or_repeat_old_actions(self) -> None:
        decision = decide_capabilities(
            self._context("群友们，明天几点集合", recent=(
                {"scope": "group:g1", "sender_id": "u2", "text": "小七看下这张图"},
            )),
            available_capabilities={"vision", "search"},
            pending_reply=True,
            burst_count=4,
            topic_related=False,
            previous_actions=("vision",),
        )
        self.assertFalse(decision.merge_messages)
        self.assertNotIn("vision", decision.actions)
        self.assertTrue(decision.reply)

    def test_context_keeps_only_same_scope_recent_messages(self) -> None:
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        context = build_capability_context(
            scope="group:g1",
            current_text="刚才那张图呢",
            sender_id="u1",
            sender_name="小明",
            now=now,
            recent_messages=(
                {"scope": "group:g1", "text": "这是照片", "sender_name": "小明"},
                {"scope": "group:g2", "text": "不要混入其他群", "sender_name": "小红"},
            ),
        )

        self.assertEqual("group", context.conversation_type)
        self.assertEqual(1, len(context.recent_messages))
        self.assertEqual("这是照片", context.recent_messages[0].text)

    def test_context_calculates_nonnegative_gap_from_aware_times(self) -> None:
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        context = build_capability_context(
            scope="private:u1",
            current_text="你好",
            sender_id="u1",
            sender_name="小明",
            now=now,
            previous_message_at=now - timedelta(seconds=75),
        )
        future_context = build_capability_context(
            scope="private:u1",
            current_text="你好",
            sender_id="u1",
            sender_name="小明",
            now=now,
            previous_message_at=now + timedelta(seconds=5),
        )

        self.assertEqual(75.0, context.last_message_gap_seconds)
        self.assertEqual(0.0, future_context.last_message_gap_seconds)

    def test_naive_or_missing_timestamps_remain_unknown(self) -> None:
        context = build_capability_context(
            scope="private:u1",
            current_text="你好",
            sender_id="u1",
            sender_name="小明",
            now=datetime(2026, 9, 28, 12),
            previous_message_at="not-a-timestamp",
        )

        self.assertIsNone(context.now)
        self.assertIsNone(context.last_message_gap_seconds)

    def test_guidance_includes_time_gap_speaker_and_image_rules(self) -> None:
        now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
        context = build_capability_context(
            scope="group:g1",
            current_text="把背景换成夜景",
            sender_id="u1",
            sender_name="小明",
            now=now,
            previous_message_at=now - timedelta(seconds=30),
            recent_messages=(
                {"scope": "group:g1", "text": "[图片]", "image_summary": "海边照片"},
            ),
            available_capabilities=("image_read", "image_edit", "voice_send"),
        )

        guidance = format_capability_guidance(context)
        self.assertIn("小明", guidance)
        self.assertIn("30 秒", guidance)
        self.assertIn("海边照片", guidance)
        self.assertIn("仅当用户确实在请求修改图片", guidance)
        self.assertIn("群聊或私聊", guidance)
        self.assertIn("不可信对话数据", guidance)

    def test_image_edit_requires_contextual_user_intent_not_topic_mention(self) -> None:
        self.assertFalse(
            capability_allowed(
                "image_edit", confidence=0.99, explicit_user_intent=False
            )
        )
        self.assertTrue(
            capability_allowed("image_edit", confidence=0.7, explicit_user_intent=True)
        )
        self.assertFalse(
            capability_allowed(
                "image_edit",
                confidence=0.99,
                explicit_user_intent=True,
                platform_supported=False,
            )
        )

    def test_proactive_voice_is_allowed_in_private_and_group(self) -> None:
        for conversation_type in ("private", "group"):
            with self.subTest(conversation_type=conversation_type):
                self.assertTrue(
                    capability_allowed(
                        "voice_send",
                        confidence=0.8,
                        explicit_user_intent=False,
                        conversation_type=conversation_type,
                    )
                )

    def test_quote_and_mention_require_reliable_target_and_purpose(self) -> None:
        self.assertFalse(
            capability_allowed(
                "quote_reply",
                confidence=0.9,
                explicit_user_intent=True,
                target_available=False,
            )
        )
        self.assertFalse(
            capability_allowed(
                "mention",
                confidence=0.9,
                explicit_user_intent=False,
                target_available=True,
            )
        )
        self.assertTrue(
            capability_allowed(
                "mention",
                confidence=0.8,
                explicit_user_intent=True,
                target_available=True,
            )
        )


class CapabilityGuidanceInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_action_available_only_for_current_file_authorization(self):
        from pathlib import Path
        from unittest.mock import Mock

        from astrbot_plugin_private_companion.task_permissions import TaskActor
        for allowed in (False, True):
            runtime = SimpleNamespace(
                workspace=Path("C:/AstrBotTasks"),
                actor=lambda _: TaskActor("bot1", "", "u1", False),
                permissions=SimpleNamespace(check=Mock(return_value=allowed)),
                snapshot_for_scope=Mock(return_value=""),
                snapshot=Mock(return_value=""),
            )
            plugin = SimpleNamespace(
                _persistent_task_runtime=runtime,
                context=SimpleNamespace(get_config=lambda **_: {}),
                _event_scope_key=lambda _: "private:u1", _recall_message_cache={},
                _llm_call=AsyncMock(return_value='{"reply":true,"actions":["workspace"],"route":"chat"}'),
            )
            event = SimpleNamespace(
                unified_msg_origin="bot1:FriendMessage:u1", message_str="读取共享脚本",
                message_obj=SimpleNamespace(message_id="workspace"),
                is_private_chat=lambda: True, get_sender_id=lambda: "u1", get_sender_name=lambda: "甲",
            )
            await append_capability_guidance(plugin, event, None)
            decision = event._private_companion_capability_decision
            self.assertEqual(("workspace",) if allowed else (), decision.actions)
            if allowed:
                self.assertEqual("task", decision.route)

    async def test_hub_reads_persistent_task_snapshot_for_exact_scope(self):
        from dataclasses import replace
        from unittest.mock import Mock

        runtime = SimpleNamespace(snapshot_for_scope=Mock(return_value="task-new: running"))
        plugin = SimpleNamespace(
            _persistent_task_runtime=runtime,
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":[],"route":"chat"}'),
        )
        context = replace(build_capability_context(
            scope="private:u1", current_text="好了吗", sender_id="u1", sender_name="甲",
            now=datetime.now(timezone.utc),
        ), task_session_key="bot1:FriendMessage:u1")
        await judge_capabilities(plugin, context, available_capabilities=())
        runtime.snapshot_for_scope.assert_called_once_with("bot1", "bot1:FriendMessage:u1", "u1")
        self.assertIn("task-new: running", plugin._llm_call.await_args.args[0])

    async def test_task_snapshot_failure_does_not_block_hub_or_leak_error_details(self):
        from dataclasses import replace
        from unittest.mock import Mock, patch

        plugin = SimpleNamespace(
            _persistent_task_runtime=SimpleNamespace(
                snapshot_for_scope=Mock(side_effect=RuntimeError("api_key=private-value")),
            ),
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["workspace"]}'),
        )
        context = replace(build_capability_context(
            scope="private:u1", current_text="你好", sender_id="u1", sender_name="甲",
            now=datetime.now(timezone.utc),
        ), task_session_key="bot1:FriendMessage:u1")
        with patch("astrbot.api.logger.warning") as warning:
            decision = await judge_capabilities(plugin, context, available_capabilities=())
        self.assertIsNotNone(decision)
        self.assertTrue(decision.reply)
        self.assertEqual((), decision.actions)
        plugin._llm_call.assert_awaited_once()
        prompt = plugin._llm_call.await_args.args[0]
        self.assertNotIn("private-value", prompt)
        self.assertNotIn("persistent_tasks", json.loads(prompt.split("输入数据：\n", 1)[1]))
        self.assertIn("RuntimeError", str(warning.call_args))
        self.assertNotIn("private-value", str(warning.call_args))

    def test_scheduling_a_future_media_reminder_does_not_perform_media_now(self):
        for text in ("十分钟后提醒我唱歌", "明天七点提醒我播放音乐"):
            with self.subTest(text=text):
                context = build_capability_context(
                    scope="private:u1", current_text=text, sender_id="u1",
                    sender_name="甲", now=datetime.now(timezone.utc),
                )
                decision = parse_model_capability_decision(
                    '{"reply":true,"actions":["schedule"],"route":"task"}',
                    available_capabilities=("schedule", "voice", "music"), context=context,
                )
                self.assertEqual(("schedule",), decision.actions)

    def test_schedule_action_requires_task_route_and_real_state_query(self):
        context = build_capability_context(
            scope="private:u1", current_text="定时任务启动了吗", sender_id="u1",
            sender_name="甲", now=datetime.now(timezone.utc),
        )
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "actions": ["schedule"],
                        "route": "chat", "task_query": True}),
            available_capabilities=("schedule",), context=context,
        )
        self.assertEqual(("schedule",), decision.actions)
        self.assertEqual("task", decision.route)
        self.assertFalse(decision.task_query)

    async def test_schedule_preflight_and_request_share_one_task_decision(self):
        from unittest.mock import MagicMock

        getter = MagicMock(return_value={
            "provider_settings": {"proactive_capability": {"add_cron_tools": True}},
        })
        plugin = SimpleNamespace(
            context=SimpleNamespace(get_config=getter, cron_manager=object()),
            _event_scope_key=lambda event: "private:u1", _recall_message_cache={},
            enable_hub_decision_reuse=True,
            _llm_call=AsyncMock(return_value=json.dumps({
                "reply": True, "actions": ["schedule"], "route": "chat",
            })),
        )
        event = SimpleNamespace(
            unified_msg_origin="bot1:FriendMessage:u1",
            message_str="小七，你定个闹钟，明天早上7:30叫我起床上早八",
            message_obj=SimpleNamespace(message_id="timer-preflight"),
            is_private_chat=lambda: True, get_sender_id=lambda: "u1",
            get_sender_name=lambda: "甲",
        )
        await append_capability_guidance(plugin, event, None)
        decision = event._private_companion_capability_decision
        prompt = plugin._llm_call.await_args.args[0]
        payload = json.loads(prompt.split("输入数据：\n", 1)[1])
        self.assertIn("schedule", payload["available_actions"])
        self.assertIn("future_task", prompt)
        self.assertEqual(("schedule",), decision.actions)
        self.assertEqual("task", decision.route)
        getter.assert_called_with(umo="bot1:FriendMessage:u1")
        request = SimpleNamespace(system_prompt="当前人格：小七", func_tool=SimpleNamespace(
            tools=[SimpleNamespace(name="future_task")],
        ))
        await append_capability_guidance(plugin, event, request)
        self.assertIs(decision, request._private_companion_capability_decision)
        self.assertIn("future_task", request.system_prompt)
        plugin._llm_call.assert_awaited_once()

    async def test_schedule_preflight_respects_config_and_scheduler_availability(self):
        for enabled, manager in ((False, object()), (True, None), (True, object())):
            with self.subTest(enabled=enabled, scheduler=manager is not None):
                plugin = SimpleNamespace(
                    context=SimpleNamespace(
                        get_config=lambda **kwargs: {"provider_settings": {
                            "proactive_capability": {"add_cron_tools": enabled},
                        }}, cron_manager=manager,
                    ),
                    _event_scope_key=lambda event: "private:u1", _recall_message_cache={},
                    _llm_call=AsyncMock(return_value='{"reply":true,"actions":["schedule"]}'),
                )
                event = SimpleNamespace(
                    unified_msg_origin="bot1:FriendMessage:u1", message_str="十分钟后提醒我喝水",
                    message_obj=SimpleNamespace(message_id="timer-availability"),
                    is_private_chat=lambda: True, get_sender_id=lambda: "u1",
                    get_sender_name=lambda: "甲",
                )
                await append_capability_guidance(plugin, event, None)
                self.assertEqual(enabled and manager is not None,
                                 "schedule" in event._private_companion_capability_decision.actions)
                request = SimpleNamespace(system_prompt="", func_tool=None)
                await append_capability_guidance(plugin, event, request)
                self.assertNotIn("schedule", request._private_companion_capability_decision.actions)
                plugin._llm_call.assert_awaited_once()

    async def test_schedule_status_uses_direct_background_reader(self):
        plugin = SimpleNamespace(
            enable_conversation_model_routing=True,
            _llm_call=AsyncMock(return_value=json.dumps({
                "reply": True, "actions": [], "route": "chat", "task_query": True,
            })),
        )
        context = build_capability_context(
            scope="private:u1", current_text="看看我明天的提醒有没有设置成功",
            sender_id="u1", sender_name="甲", now=datetime.now(timezone.utc),
            available_capabilities=("schedule",),
        )
        decision = await judge_capabilities(plugin, context, available_capabilities=("schedule",))
        self.assertEqual("chat", decision.route)
        self.assertTrue(decision.task_query)
        prompt = plugin._llm_call.await_args.args[0]
        self.assertIn("pc_read_background", prompt)
        self.assertIn("命令", prompt)
        self.assertNotIn("这些都选择 schedule", prompt)
        self.assertIn("pc_read_background", format_capability_guidance(context))
        self.assertIn("持久", prompt)

    async def test_group_ambiguous_tool_followup_requires_own_context_or_task(self):
        now = datetime.now(timezone.utc)
        for owner in ("u1", "u2"):
            with self.subTest(owner=owner):
                plugin = SimpleNamespace(_llm_call=AsyncMock(return_value=json.dumps({
                    "reply": True, "actions": ["image_generate"], "route": "task",
                    "image_edit_requested": True,
                    "segments": [{"kind": "task", "text": "生成头像"}],
                })))
                context = build_capability_context(
                    scope="group:g1", current_text="那你现在调用", sender_id="u1", sender_name="甲", now=now,
                    recent_messages=[{"scope": "group:g1", "sender_id": owner,
                                      "text": "帮我生成一张头像", "timestamp": now}],
                )
                decision = await judge_capabilities(plugin, context, available_capabilities=("image_generate",))
                self.assertEqual(owner == "u1", "image_generate" in decision.actions)
                self.assertEqual(owner == "u1", decision.image_edit_requested)
                if owner != "u1":
                    self.assertTrue(decision.reply)
                    self.assertEqual("chat", decision.route)
                    self.assertFalse(decision.segments)

    async def test_private_continuation_guidance_is_not_in_group_prompt(self):
        plugin = SimpleNamespace(_llm_call=AsyncMock(return_value='{"reply":true,"actions":[]}'))
        context = build_capability_context(
            scope="group:g1", current_text="那你现在调用", sender_id="u2", sender_name="乙",
            now=datetime.now(timezone.utc),
        )
        await judge_capabilities(plugin, context, available_capabilities=("image_generate",))
        self.assertNotIn("例如此前要求生成头像", plugin._llm_call.await_args.args[0])

    async def test_private_preflight_reads_current_conversation_once_for_short_followup(self):
        history = [
            {"role": "system", "content": "不能泄漏的系统提示"},
            {"role": "user", "content": "帮我生成一张二次元头像"},
            {"role": "assistant", "content": "暂时调不了生图接口"},
            {"role": "tool", "content": "不能传播的工具原始输出"},
        ]
        manager = SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="c1"),
            get_conversation=AsyncMock(return_value=SimpleNamespace(
                cid="c1", history=json.dumps(history),
            )),
        )
        plugin = SimpleNamespace(
            context=SimpleNamespace(conversation_manager=manager),
            _event_scope_key=lambda event: "private:u1",
            _recall_message_cache={},
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["image_generate"],"route":"task"}'),
        )
        event = SimpleNamespace(
            unified_msg_origin="bot1:FriendMessage:u1", message_str="那你现在调用",
            message_obj=SimpleNamespace(message_id="m1"),
            is_private_chat=lambda: True, get_sender_id=lambda: "u1",
            get_sender_name=lambda: "甲",
        )
        await append_capability_guidance(plugin, event, None)
        prompt = plugin._llm_call.await_args.args[0]
        self.assertIn("帮我生成一张二次元头像", prompt)
        self.assertIn("暂时调不了生图接口", prompt)
        self.assertNotIn("不能泄漏的系统提示", prompt)
        self.assertNotIn("不能传播的工具原始输出", prompt)
        self.assertEqual("task", event._private_companion_capability_decision.route)
        manager.get_conversation.assert_awaited_once_with("bot1:FriendMessage:u1", "c1")
        await append_capability_guidance(plugin, event, SimpleNamespace(system_prompt="", func_tool=None))
        plugin._llm_call.assert_awaited_once()
        manager.get_conversation.assert_awaited_once()

    async def test_private_conversation_projection_is_bounded_and_ignores_media_urls(self):
        history = [{"role": "user", "content": "旧话题"}] + [
            {"role": "assistant", "content": "长回复" * 600} for _ in range(12)
        ] + [{"role": "user", "content": [
            {"type": "text", "text": "按刚才要求继续画"},
            {"type": "image_url", "image_url": {"url": "https://private/image?secret=value"}},
        ]}]
        plugin = SimpleNamespace(
            _event_scope_key=lambda event: "private:u1", _recall_message_cache={},
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":[]}'),
        )
        event = SimpleNamespace(
            unified_msg_origin="bot1:FriendMessage:u1", message_str="继续",
            message_obj=SimpleNamespace(message_id="m2"),
            is_private_chat=lambda: True, get_sender_id=lambda: "u1", get_sender_name=lambda: "甲",
        )
        await append_capability_guidance(plugin, event, SimpleNamespace(
            contexts=history, system_prompt="", func_tool=None,
        ))
        prompt = plugin._llm_call.await_args.args[0]
        self.assertIn("按刚才要求继续画", prompt)
        self.assertNotIn("secret=value", prompt)
        self.assertNotIn("旧话题", prompt)
        self.assertLess(prompt.count("长回复"), 1000)

    async def test_hub_uses_independent_provider_and_persona_emotion_context(self):
        llm_call = AsyncMock(return_value=json.dumps({
            "reply": True, "actions": [], "route": "mixed",
            "emotion_event": {"kind": "tease", "intensity": 0.7, "confidence": 0.9,
                              "targets_bot": True, "phase": "covering"},
        }))
        plugin = SimpleNamespace(
            llm_provider_id="legacy-gpt", conversation_hub_provider_id="hub-fixed",
            enable_short_term_emotion=True, enable_conversation_model_routing=True,
            _task_provider=lambda *ids: next((item for item in ids if item), ""),
            _llm_call=llm_call,
        )
        context = build_capability_context(
            scope="private:u1", current_text="你就是关心我，顺便帮我算一下",
            sender_id="u1", sender_name="甲", now=datetime.now(timezone.utc),
        )
        decision = await judge_capabilities(
            plugin, context, available_capabilities=(), persona_prompt="嘴硬但关心对方",
            short_term_state={"embarrassment": 0.3},
        )
        self.assertEqual("hub-fixed", llm_call.await_args.kwargs["provider_id"])
        self.assertEqual("mixed", decision.route)
        self.assertEqual("tease", decision.emotion_event["kind"])
        self.assertIn("嘴硬但关心对方", llm_call.await_args.args[0])
        self.assertIn("embarrassment", llm_call.await_args.args[0])

    def test_route_and_emotion_cannot_inject_provider_or_unbounded_values(self):
        context = build_capability_context(
            scope="private:u1", current_text="你好", sender_id="u1", sender_name="甲",
            now=datetime.now(timezone.utc),
        )
        decision = parse_model_capability_decision(
            json.dumps({"reply": True, "actions": [], "route": "openai/gpt",
                        "emotion_event": {"kind": "invented", "intensity": 999}}),
            available_capabilities=(), context=context,
        )
        self.assertEqual("chat", decision.route)
        self.assertEqual({}, decision.emotion_event)

    def test_task_estimate_and_mixed_segments_are_bounded(self):
        context = build_capability_context(
            scope="private:u1", current_text="先说明一下，再帮我分析项目",
            sender_id="u1", sender_name="甲", now=datetime.now(timezone.utc),
        )
        decision = parse_model_capability_decision(
            json.dumps({
                "reply": True, "actions": [], "route": "mixed",
                "estimated_seconds": 99999,
                "segments": [
                    {"kind": "chat", "text": "先说明一下"},
                    {"kind": "task", "text": "帮我分析项目"},
                    {"kind": "evil", "text": "丢弃"},
                ],
            }),
            available_capabilities=(), context=context,
        )
        self.assertEqual(3600, decision.estimated_seconds)
        self.assertEqual(("chat", "先说明一下"), decision.segments[0])
        self.assertEqual(("task", "帮我分析项目"), decision.segments[1])
        self.assertEqual(2, len(decision.segments))

    async def test_model_judge_uses_reply_provider_and_standard_fallback_route(self) -> None:
        llm_call = AsyncMock(
            return_value='{"reply":true,"actions":[],"tone":"自然"}'
        )
        plugin = SimpleNamespace(
            llm_provider_id="reply-primary",
            _task_provider=lambda *provider_ids: provider_ids[0] or "",
            _llm_call=llm_call,
        )
        context = build_capability_context(
            scope="private:u1",
            current_text="你好",
            sender_id="u1",
            sender_name="小明",
            now=datetime(2026, 9, 29, tzinfo=timezone.utc),
        )

        decision = await judge_capabilities(
            plugin,
            context,
            available_capabilities=("emoji",),
        )

        self.assertTrue(decision.reply)
        self.assertEqual("reply-primary", llm_call.await_args.kwargs["provider_id"])
        self.assertEqual("astrbot_reply", llm_call.await_args.kwargs["task"])

    async def test_model_judge_repairs_malformed_output_once_in_the_same_call(self):
        for malformed in ("not-json", '{"reply":"yes","actions":[]}'):
            with self.subTest(malformed=malformed):
                plugin = SimpleNamespace(
                    conversation_hub_provider_id="hub",
                    _llm_call=AsyncMock(side_effect=[
                        malformed, '{"reply":true,"actions":["workspace"],"route":"task"}',
                    ]),
                )
                context = build_capability_context(
                    scope="private:u1", current_text="运行我的脚本", sender_id="u1",
                    sender_name="甲", now=datetime.now(timezone.utc),
                )
                decision = await judge_capabilities(
                    plugin, context, available_capabilities=("workspace",),
                )
                self.assertIsNotNone(decision)
                self.assertEqual(("workspace",), decision.actions)
                self.assertEqual("task", decision.route)
                self.assertEqual(2, plugin._llm_call.await_count)
                self.assertEqual(
                    "CONVERSATION_HUB_PROVIDER_ID",
                    plugin._llm_call.await_args.kwargs["timeout_key"],
                )
                self.assertIn("结构", plugin._llm_call.await_args.args[0])

    async def test_model_judge_repeated_malformed_output_never_creates_approval(self):
        plugin = SimpleNamespace(_llm_call=AsyncMock(return_value="not-json"))
        context = build_capability_context(
            scope="private:u1", current_text="运行脚本", sender_id="u1", sender_name="甲",
            now=datetime.now(timezone.utc),
        )
        self.assertIsNone(await judge_capabilities(
            plugin, context, available_capabilities=("workspace",),
        ))
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_explicit_group_reaction_request_is_not_rejected_as_group_chat(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {"recent_messages": []},
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["emoji"],"tone":"轻松"}'),
        )
        event = SimpleNamespace(
            message_str="发一个表情包",
            message_obj=SimpleNamespace(message_id="m-explicit-emoji"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )

        await append_capability_guidance(plugin, event, None)

        self.assertTrue(event._private_companion_capability_decision.reply)
        self.assertIn("emoji", event._private_companion_capability_decision.actions)

    async def test_group_reply_qualification_is_owned_by_hub(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {"recent_messages": []},
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(side_effect=[
                '{"reply":false,"actions":[],"tone":"none"}',
                '{"reply":true,"actions":[],"tone":"自然"}',
            ]),
        )
        event = SimpleNamespace(
            message_str="这句话是在群里聊天",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )

        await append_capability_guidance(plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)

        event = SimpleNamespace(
            message_str="这句话是在继续和小七说",
            message_obj=SimpleNamespace(message_id="m2"),
            private_companion_group_scene={
                "talking_to": "bot",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        await append_capability_guidance(plugin, event, None)

        self.assertTrue(event._private_companion_capability_decision.reply)

    async def test_group_hub_reply_upgrades_non_wakeup_event(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {"recent_messages": []},
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":[],"tone":"自然"}'),
        )
        event = SimpleNamespace(
            message_str="你现在是什么模型",
            message_obj=SimpleNamespace(message_id="m-upgrade"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
            is_wake=False,
            is_at_or_wake_command=False,
        )

        await append_capability_guidance(plugin, event, None)

        self.assertTrue(event._private_companion_capability_decision.reply)
        self.assertTrue(event.is_wake)
        self.assertTrue(event.is_at_or_wake_command)

    async def test_group_hub_silence_does_not_upgrade_non_wakeup_event(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {"recent_messages": []},
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":false,"actions":[],"tone":"none"}'),
        )
        event = SimpleNamespace(
            message_str="普通闲聊",
            message_obj=SimpleNamespace(message_id="m-silent"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
            is_wake=False,
            is_at_or_wake_command=False,
        )

        await append_capability_guidance(plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)
        self.assertFalse(event.is_wake)
        self.assertFalse(event.is_at_or_wake_command)

    async def test_related_group_topic_without_hub_does_not_wake_from_history(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {
                "recent_messages": [
                    {
                        "message_id": "m0",
                        "ts": datetime.now(timezone.utc).timestamp() - 280,
                        "sender_id": "u1",
                        "name": "小明",
                        "text": "小七，评价一下贴吧鱼",
                        "talking_to": "bot",
                        "scene_trigger": "group_wakeup_direct_word",
                    },
                    {
                        "message_id": "b0",
                        "ts": datetime.now(timezone.utc).timestamp() - 270,
                        "sender_id": "bot",
                        "name": "小七",
                        "text": "贴吧鱼这个说法需要结合上下文判断",
                        "talking_to": "group",
                        "scene_trigger": "bot_reply",
                    },
                ]
            },
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
        )
        event = SimpleNamespace(
            message_str="贴吧鱼是AI",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )

        await append_capability_guidance(plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)

    async def test_history_bot_target_does_not_wake_when_hub_is_unavailable(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {
                "recent_messages": [
                    {
                        "message_id": "m0",
                        "ts": datetime.now(timezone.utc).timestamp() - 20,
                        "sender_id": "u1",
                        "name": "小明",
                        "text": "只准自己看哈",
                        "talking_to": "bot",
                        "scene_trigger": "reply_bot",
                    }
                ]
            },
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
        )
        event = SimpleNamespace(
            message_str="自己看什么",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_group_scene={
                "talking_to": "group",
                "trigger": "group_message",
            },
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )

        await append_capability_guidance(plugin, event, None)

        self.assertFalse(event._private_companion_capability_decision.reply)

    def test_short_followup_inherits_recent_explicit_emoji_request(self) -> None:
        now = datetime.now(timezone.utc)
        context = build_capability_context(
            scope="private:u1",
            current_text="现在呢",
            sender_id="u1",
            sender_name="小明",
            now=now,
            recent_messages=[
                {
                    "scope": "private:u1",
                    "sender_id": "u1",
                    "text": "现在表情包了吗",
                    "timestamp": now,
                }
            ],
            available_capabilities=("emoji",),
        )

        decision = decide_capabilities(
            context,
            available_capabilities=("emoji",),
        )

        self.assertIn("emoji", decision.actions)

    def test_repeat_image_request_inherits_emoji_without_repeating_keyword(self) -> None:
        now = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        context = build_capability_context(
            scope="group:g1",
            current_text="再来一张",
            sender_id="u1",
            sender_name="小明",
            now=now,
            recent_messages=[
                {
                    "scope": "group:g1",
                    "sender_id": "u1",
                    "text": "发一个表情包",
                    "timestamp": now - timedelta(seconds=20),
                }
            ],
            available_capabilities=("emoji",),
        )

        decision = decide_capabilities(context, available_capabilities=("emoji",))

        self.assertIn("emoji", decision.actions)

    def test_group_followup_does_not_inherit_another_members_tool_request(self) -> None:
        now = datetime(2026, 9, 29, 10, tzinfo=timezone.utc)
        context = build_capability_context(
            scope="group:g1",
            current_text="再来一张",
            sender_id="u1",
            sender_name="小明",
            now=now,
            recent_messages=[
                {
                    "scope": "group:g1",
                    "sender_id": "u2",
                    "text": "发一个表情包",
                    "timestamp": now - timedelta(seconds=20),
                }
            ],
            available_capabilities=("emoji",),
        )

        decision = decide_capabilities(context, available_capabilities=("emoji",))

        self.assertNotIn("emoji", decision.actions)

    def test_short_followup_inherits_each_recent_tool_request(self) -> None:
        cases = (
            ("刚才说的图片现在呢", "看看这张图片", "vision"),
            ("结果呢", "查一下最新天气", "search"),
            ("现在可以了吗", "把这张图改成黑白", "image_edit"),
            ("发了吗", "请发语音朗读", "voice"),
            ("现在呢", "现在表情包了吗", "emoji"),
        )
        for current, previous, expected in cases:
            now = datetime.now(timezone.utc)
            context = build_capability_context(
                scope="private:u1",
                current_text=current,
                sender_id="u1",
                sender_name="小明",
                now=now,
                recent_messages=[
                    {
                        "scope": "private:u1",
                        "sender_id": "u1",
                        "text": previous,
                        "timestamp": now,
                    }
                ],
                available_capabilities=("vision", "search", "image_edit", "voice", "emoji"),
            )
            decision = decide_capabilities(
                context,
                available_capabilities=context.available_capabilities,
            )
            self.assertIn(expected, decision.actions, (current, previous))

    async def test_group_explicit_wakeup_bypasses_reply_qualification(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {"recent_messages": []},
            _recall_message_cache={},
            _central_hub_scheduler_enabled=True,
        )
        for trigger in ("at_bot", "reply_bot", "group_wakeup_name"):
            event = SimpleNamespace(
                message_str="小七，回答一下",
                message_obj=SimpleNamespace(message_id=f"m-{trigger}"),
                private_companion_group_scene={
                    "talking_to": "group",
                    "trigger": trigger,
                },
                is_private_chat=lambda: False,
                get_sender_id=lambda: "u1",
                get_sender_name=lambda: "小明",
            )

            await append_capability_guidance(plugin, event, None)

            self.assertTrue(
                event._private_companion_capability_decision.reply,
                trigger,
            )

    async def test_image_preflight_decision_is_reused_by_request(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _recall_message_cache={},
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["vision"],"tone":"自然"}'),
        )
        event = SimpleNamespace(
            message_str="看看今天拍的照片",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_delayed_image_sources=["saved-image"],
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        await append_capability_guidance(plugin, event, None)
        preflight = event._private_companion_capability_decision
        self.assertIn("vision", preflight.actions)

        request = SimpleNamespace(
            system_prompt="原有提示",
            func_tool=SimpleNamespace(tools=[SimpleNamespace(name="web_search_tavily")]),
        )
        await append_capability_guidance(plugin, event, request)

        self.assertIs(preflight, request._private_companion_capability_decision)
        self.assertIn("vision", request._private_companion_capability_decision.actions)
        self.assertNotIn("search", request._private_companion_capability_decision.actions)
        plugin._llm_call.assert_awaited_once()

    async def test_reply_guidance_uses_candidate_without_tone_or_emotion_commands(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _recall_message_cache={},
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(
                return_value=(
                    '{"reply":true,"actions":["emoji"],"tone":"活泼",'
                    '"emotion":"开心","reply_candidate":"接住刚才的玩笑"}'
                )
            ),
        )
        event = SimpleNamespace(
            message_str="哈哈这个梗好笑",
            message_obj=SimpleNamespace(message_id="candidate-guidance"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        await append_capability_guidance(plugin, event, None)
        request = SimpleNamespace(system_prompt="当前人格提示", func_tool=None)

        await append_capability_guidance(plugin, event, request)

        self.assertIn("接住刚才的玩笑", request.system_prompt)
        self.assertIn("不可直接发送", request.system_prompt)
        self.assertNotIn("语气=活泼", request.system_prompt)
        self.assertNotIn("情绪线索=开心", request.system_prompt)
        self.assertIn("当前人格提示", request.system_prompt)
        self.assertEqual("", request.prompt if hasattr(request, "prompt") else "")
        self.assertEqual(("emoji",), request._private_companion_capability_decision.actions)
        plugin._llm_call.assert_awaited_once()

    async def test_preflight_search_is_removed_when_request_has_no_tool(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _recall_message_cache={},
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["vision"],"tone":"自然"}'),
        )
        event = SimpleNamespace(
            message_str="看看今天拍的照片",
            message_obj=SimpleNamespace(message_id="m1"),
            private_companion_delayed_image_sources=["saved-image"],
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        await append_capability_guidance(plugin, event, None)
        request = SimpleNamespace(system_prompt="原有提示", func_tool=None)

        await append_capability_guidance(plugin, event, request)

        self.assertIn("vision", request._private_companion_capability_decision.actions)
        self.assertNotIn("search", request._private_companion_capability_decision.actions)

    async def test_search_tools_follow_hub_decision(self) -> None:
        class Tools:
            def __init__(self) -> None:
                self.tools = [SimpleNamespace(name=name) for name in (
                    "web_search_tavily", "AIsearch", "find_music", "deliver_music", "future_task", "safe_tool"
                )]

            def remove_tool(self, name: str) -> None:
                self.tools = [tool for tool in self.tools if tool.name != name]

        request = SimpleNamespace(func_tool=Tools())
        request._private_companion_capability_decision = SimpleNamespace(actions=())
        gate_request_search_tools(request)
        self.assertEqual(["safe_tool"], [tool.name for tool in request.func_tool.tools])

        approved = SimpleNamespace(func_tool=Tools())
        approved._private_companion_capability_decision = SimpleNamespace(actions=("search",))
        gate_request_search_tools(approved)
        self.assertEqual(["web_search_tavily", "AIsearch", "safe_tool"], [tool.name for tool in approved.func_tool.tools])

        music_approved = SimpleNamespace(func_tool=Tools())
        music_approved._private_companion_capability_decision = SimpleNamespace(actions=("music",))
        gate_request_search_tools(music_approved)
        self.assertEqual(["find_music", "deliver_music", "safe_tool"], [tool.name for tool in music_approved.func_tool.tools])

        schedule_approved = SimpleNamespace(func_tool=Tools())
        schedule_approved._private_companion_capability_decision = SimpleNamespace(actions=("schedule",))
        gate_request_search_tools(schedule_approved)
        self.assertEqual(["future_task", "safe_tool"], [tool.name for tool in schedule_approved.func_tool.tools])

    async def test_chat_managed_search_does_not_need_hub_action(self) -> None:
        from astrbot.core.agent.tool import FunctionTool, ToolSet

        tools = ToolSet()
        for name in ("web_search_tavily", "tavily_extract_web_page", "find_music", "future_task"):
            tools.add_tool(FunctionTool(
                name=name, description=name,
                parameters={"type": "object", "properties": {}},
            ))
        request = SimpleNamespace(
            func_tool=tools,
            _private_companion_chat_search_managed=True,
            _private_companion_capability_decision=SimpleNamespace(actions=()),
        )

        gate_request_search_tools(request)

        self.assertIsNotNone(tools.get_tool("web_search_tavily"))
        self.assertIsNotNone(tools.get_tool("tavily_extract_web_page"))
        self.assertIsNone(tools.get_tool("find_music"))
        self.assertIsNone(tools.get_tool("future_task"))

    async def test_injects_same_group_recent_messages_and_image_summary(self) -> None:
        previous_ts = datetime.now(timezone.utc).timestamp() - 30
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {
                "recent_messages": [
                    {
                        "message_id": "m1",
                        "ts": previous_ts,
                        "sender_id": "u1",
                        "name": "小明",
                        "text": "看这张图片",
                        "image_vision": "一只橘猫在窗台上",
                    },
                    {
                        "message_id": "m2",
                        "ts": datetime.now(timezone.utc).timestamp(),
                        "sender_id": "u1",
                        "name": "小明",
                        "text": "这是什么品种",
                    },
                ]
            },
            _recall_message_cache={},
        )
        event = SimpleNamespace(
            message_str="这是什么品种",
            message_obj=SimpleNamespace(message_id="m2"),
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(system_prompt="原有系统提示")

        await append_capability_guidance(plugin, event, request)

        self.assertIn("原有系统提示", request.system_prompt)
        self.assertIn("一只橘猫在窗台上", request.system_prompt)
        self.assertIn("距前一条消息：", request.system_prompt)
        self.assertIn("群聊", request.system_prompt)
        decision = request._private_companion_capability_decision
        self.assertIs(event._private_companion_capability_decision, decision)
        self.assertEqual(decision.actions, ())

    async def test_request_gets_one_deduplicated_capability_decision(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _extract_group_id_from_event=lambda _event: "",
            _get_group=lambda _group_id: {},
            _recall_message_cache={},
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["vision","search"],"tone":"自然"}'),
        )
        event = SimpleNamespace(
            message_str="帮我看看这张图，顺便查一下今天的天气",
            message_obj=SimpleNamespace(message_id="m1"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(
            system_prompt="原有提示",
            func_tool=SimpleNamespace(tools=[SimpleNamespace(name="web_search_tavily")]),
        )

        await append_capability_guidance(plugin, event, request)

        decision = request._private_companion_capability_decision
        self.assertIs(event._private_companion_capability_decision, decision)
        self.assertEqual(decision.actions, ("vision", "search"))

        self.assertIn("vision、search", request.system_prompt)

    async def test_force_rejudge_uses_merged_message_stream(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "group:g1",
            _extract_group_id_from_event=lambda _event: "g1",
            _get_group=lambda _group_id: {
                "recent_messages": [
                    {"sender_id": "u1", "sender_name": "甲", "text": "算法复杂度"},
                    {"sender_id": "u2", "sender_name": "乙", "text": "空间复杂度呢"},
                ]
            },
            _recall_message_cache={},
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(
                side_effect=[
                    '{"reply":true,"actions":[],"tone":"自然","merge_messages":true}',
                    '{"reply":false,"actions":[],"tone":"none","merge_messages":false}',
                ]
            ),
        )
        event = SimpleNamespace(
            message_str="空间复杂度呢",
            private_companion_group_text="算法复杂度\n空间复杂度呢",
            message_obj=SimpleNamespace(message_id="m2"),
            private_companion_group_scene={"talking_to": "group"},
            is_private_chat=lambda: False,
            get_sender_id=lambda: "u2",
            get_sender_name=lambda: "乙",
        )
        request = SimpleNamespace(system_prompt="原有提示")

        await append_capability_guidance(plugin, event, None)
        first = event._private_companion_capability_decision
        await append_capability_guidance(plugin, event, request, force_rejudge=True)

        self.assertTrue(first.merge_messages)
        self.assertFalse(request._private_companion_capability_decision.reply)
        self.assertEqual(2, plugin._llm_call.await_count)

    async def test_existing_hub_marker_still_refreshes_decision_from_history(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _task_provider=lambda *args: "reply-primary",
            _llm_call=AsyncMock(return_value='{"reply":true,"actions":["emoji"],"tone":"自然"}'),
            _recall_message_cache={
                "m0": {
                    "message_id": "m0",
                    "scope": "private:u1",
                    "ts": datetime.now(timezone.utc).timestamp() - 5,
                    "sender_id": "u1",
                    "sender_name": "小明",
                    "text": "请发一个表情包",
                }
            },
        )
        event = SimpleNamespace(
            message_str="现在呢",
            message_obj=SimpleNamespace(message_id="m1"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(
            system_prompt="旧提示\n<!-- private_companion_capability_hub_v1 -->",
            func_tool=SimpleNamespace(tools=[]),
        )

        await append_capability_guidance(plugin, event, request)

        self.assertIn("emoji", request._private_companion_capability_decision.actions)
        self.assertIn("请发一个表情包", request.system_prompt)

    async def test_request_does_not_advertise_unavailable_search(self) -> None:
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _recall_message_cache={},
        )
        event = SimpleNamespace(
            message_str="查一下今天的天气",
            message_obj=SimpleNamespace(message_id="m1"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(system_prompt="原有提示", func_tool=None)

        await append_capability_guidance(plugin, event, request)

        self.assertNotIn("search", request._private_companion_capability_decision.actions)

    async def test_injects_private_recent_context_without_other_scope_leak(
        self,
    ) -> None:
        now = datetime.now(timezone.utc).timestamp()
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _extract_group_id_from_event=lambda _event: "",
            _get_group=lambda _group_id: {},
            _recall_message_cache={
                "m1": {
                    "message_id": "m1",
                    "scope": "private:u1",
                    "ts": now - 60,
                    "sender_id": "u1",
                    "sender_name": "小明",
                    "text": "我们刚才说到橘猫",
                },
                "other": {
                    "message_id": "other",
                    "scope": "private:u2",
                    "ts": now - 30,
                    "sender_id": "u2",
                    "sender_name": "小红",
                    "text": "不能泄露这条消息",
                },
            },
        )
        event = SimpleNamespace(
            message_str="它多大了",
            message_obj=SimpleNamespace(message_id="m2"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(system_prompt="原有私聊提示")

        await append_capability_guidance(plugin, event, request)

        self.assertIn("我们刚才说到橘猫", request.system_prompt)
        self.assertNotIn("不能泄露这条消息", request.system_prompt)
        self.assertIn("私聊", request.system_prompt)

    async def test_private_context_reuses_cached_image_description_only(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        summary_getter = AsyncMock(return_value="一只橘猫在窗台上")
        plugin = SimpleNamespace(
            _event_scope_key=lambda _event: "private:u1",
            _extract_group_id_from_event=lambda _event: "",
            _get_group=lambda _group_id: {},
            _context_image_cached_summary=summary_getter,
            _recall_message_cache={
                "m1": {
                    "message_id": "m1",
                    "scope": "private:u1",
                    "ts": now - 60,
                    "sender_id": "u1",
                    "sender_name": "小明",
                    "text": "[图片]",
                    "images": ["opaque-image-reference"],
                    "image_items": [{"source": "opaque-image-reference"}],
                },
                "m2": {
                    "message_id": "m2",
                    "scope": "private:u1",
                    "ts": now,
                    "sender_id": "u1",
                    "sender_name": "小明",
                    "text": "那只猫呢",
                },
            },
        )
        event = SimpleNamespace(
            message_str="它多大了",
            message_obj=SimpleNamespace(message_id="m2"),
            is_private_chat=lambda: True,
            get_sender_id=lambda: "u1",
            get_sender_name=lambda: "小明",
        )
        request = SimpleNamespace(system_prompt="原有私聊提示")

        await append_capability_guidance(plugin, event, request)

        self.assertIn("一只橘猫在窗台上", request.system_prompt)
        summary_getter.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
