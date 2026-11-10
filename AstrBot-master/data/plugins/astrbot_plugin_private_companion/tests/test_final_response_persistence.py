from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import File, Image, Plain
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_private_companion.final_response_persistence import (
    FinalResponsePersistenceMixin,
    collect_proactive_delivery,
)
from astrbot_plugin_private_companion.helpers import (
    _strip_internal_message_blocks,
    _strip_outbound_control_blocks,
)
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin
from astrbot_plugin_private_companion.proactive_message import ProactiveMessageMixin

UMO = "default:FriendMessage:10001"
LIVING_MODULE = "data.plugins.astrbot_plugin_livingmemory.main"
MEMORY_COMPANION_MODULE = "data.plugins.astrbot_plugin_memory_companion.main"
COMPANION_MODULE = "data.plugins.astrbot_plugin_private_companion.main"


class _ConversationManager:
    def __init__(self) -> None:
        self.history = [{"role": "user", "content": "真实用户消息"}]

    async def get_curr_conversation_id(self, _umo: str) -> str:
        return "conversation-1"

    async def get_conversation(self, _umo: str, _cid: str):
        return SimpleNamespace(history=json.dumps(self.history, ensure_ascii=False))

    async def update_conversation(self, _umo: str, _cid: str, *, history=None, **_kwargs):
        self.history = list(history or [])

    async def add_message_pair(self, *, cid: str, user_message, assistant_message):
        assert cid == "conversation-1"
        self.history.extend([user_message.model_dump(), assistant_message.model_dump()])


class _Event:
    def __init__(self) -> None:
        self.unified_msg_origin = UMO
        self.plugins_name = None
        self._has_send_oper = True
        self._private_companion_persistence_managed = True
        self._private_companion_livingmemory_plugin_names = ("LivingMemory",)
        self._private_companion_response_conversation_id = "conversation-1"
        self._extras = {
            "provider_request": SimpleNamespace(
                conversation=SimpleNamespace(cid="conversation-1")
            )
        }

    def get_extra(self, key: str, default=None):
        return self._extras.get(key, default)


class _SendTrackerEvent(_Event):
    def __init__(
        self,
        *,
        send_error: Exception | None = None,
        send_result=None,
    ) -> None:
        super().__init__()
        self._result = SimpleNamespace(chain=[Plain("审核后的待发送回复")])
        self.send_error = send_error
        self.send_result = send_result
        self.sent = []
        self.stopped = False

    def get_result(self):
        return self._result

    async def send(self, message):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(message)
        return self.send_result

    def is_stopped(self) -> bool:
        return self.stopped


@dataclass(frozen=True)
class _ActiveOutcome:
    delivered: bool
    delivered_text: str = ""
    delivery_umo: str = ""
    delivered_chain: tuple = ()
    history_metadata: dict | None = None


class _ActiveCollector(FinalResponsePersistenceMixin):
    @collect_proactive_delivery
    async def send(self, umo: str) -> _ActiveOutcome:
        self._confirm_outbound_delivery(umo, [Plain("平台实际收到的主动回复")])
        return _ActiveOutcome(True, delivered_text="审核后的候选回复")


class _Registry:
    def __init__(self, handlers) -> None:
        self.handlers = list(handlers)

    def get_handlers_by_event_type(self, _event_type, *, plugins_name=None):
        if plugins_name not in (None, ["*"]) and "LivingMemory" not in plugins_name:
            return []
        return list(self.handlers)


class _Harness(ProactiveMessageMixin):
    enable_livingmemory_integration = True
    bot_name = "陪伴者"

    def __init__(self) -> None:
        self.conversation_manager = _ConversationManager()
        self.context = SimpleNamespace(conversation_manager=self.conversation_manager)
        self.memory_companion_captured: list[str] = []

    @staticmethod
    def _memory_companion_bridge():
        async def record_visible_turn(**_kwargs):
            return None

        return SimpleNamespace(record_visible_turn=record_visible_turn)

    async def _memory_companion_record_confirmed_assistant_message(
        self,
        _event,
        *,
        content: str,
        delivery_id: str = "",
    ) -> bool:
        self.memory_companion_captured.append(content)
        return True

    @staticmethod
    def _event_message_id(_event) -> str:
        return "message-1"

    @staticmethod
    def _proactive_synthetic_event(umo: str, *, prompt: str, name: str):
        event = _Event()
        event.unified_msg_origin = umo
        event._private_companion_persistence_managed = False
        return event


class FinalResponsePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_delivery_waits_for_core_archive_before_appending(self):
        from astrbot.core.utils.session_lock import session_lock_manager
        harness = _Harness()
        async with session_lock_manager.acquire_lock(UMO):
            task = asyncio.create_task(harness._record_confirmed_outbound_delivery(
                UMO, [Plain("后台发的"), File(name="结果.zip", file="gone.zip")]))
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(1, len(harness.conversation_manager.history))
        await task
        self.assertEqual("assistant", harness.conversation_manager.history[-1]["role"])

    async def test_proactive_collector_keeps_media_metadata(self):
        class Sender(FinalResponsePersistenceMixin):
            @collect_proactive_delivery
            async def send(self, umo):
                await self._record_confirmed_outbound_delivery(umo, [Image(file="gone.png"), Plain("图片之后")])
                return _ActiveOutcome(True)
        outcome = await Sender().send(UMO)
        self.assertEqual(["image", "text"], [r["type"] for r in outcome.history_metadata["sent_sequence"]])

    async def test_confirmed_media_attached_to_official_message_in_actual_send_order(self):
        from astrbot.core.agent.message import dump_messages_with_checkpoints
        harness = _Harness()
        event = _SendTrackerEvent()
        coordinator = harness._final_response_persistence_coordinator()
        coordinator.begin_passive(event)
        official = Message(role="assistant", content="最终回复")
        event._private_companion_official_assistant_message = official
        try:
            await harness._record_confirmed_outbound_delivery(UMO, [Plain("前一条"), Image(file="gone.png")])
            coordinator.mark_final_response_ready(event)
            await harness._record_confirmed_outbound_delivery(UMO, [Plain("后一条"), File(name="结果.txt", file="gone.txt")])
            await coordinator.finalize_passive(event)
            row = dump_messages_with_checkpoints([official])[0]
            sequence = row["history_metadata"]["sent_sequence"]
            self.assertEqual(["text", "image", "text", "file"], [r["type"] for r in sequence])
            self.assertEqual([0, 0, 1, 1], [r["send_index"] for r in sequence])
            self.assertIn("结果.txt", row["content"][0]["text"])
        finally:
            coordinator._reset_context(event._private_companion_delivery_ledger)

    async def test_standalone_background_file_is_archived_without_fake_user_turn(self):
        harness = _Harness()
        await harness._record_confirmed_outbound_delivery(UMO, [File(name="后台.zip", file="gone.zip")])
        row = harness.conversation_manager.history[-1]
        self.assertEqual("assistant", row["role"])
        self.assertEqual(2, len(harness.conversation_manager.history))
        self.assertEqual("后台.zip", row["history_metadata"]["sent_sequence"][0]["name"])

    def test_delivered_image_is_archived_as_internal_media_marker(self):
        harness = _Harness()

        archived = harness._delivered_assistant_text_from_chain(
            [Plain("正文"), Image(file="image.png")]
        )

        self.assertIn("正文", archived)
        self.assertIn('<pc_history_media images="1" />', archived)
        self.assertNotIn("发送了一张图片", archived)

    def test_outbound_cleanup_removes_legacy_and_internal_media_placeholders(self):
        raw = (
            "第一段\n（发送了一张图片，发送了 2 条语音）\n"
            "（发送了一条语音）\n"
            '<pc_history_media images="1" records="2" />\n第二段'
        )

        self.assertEqual("第一段\n\n第二段", _strip_outbound_control_blocks(raw))

    def test_outbound_cleanup_removes_leaked_emotion_controls(self):
        raw = "[affectionate]嗯……\n[shy]才没有呢。[公告]明天见。"

        self.assertEqual("嗯……\n才没有呢。[公告]明天见。", _strip_outbound_control_blocks(raw))

    def test_cleanup_removes_photo_tool_silent_sentinel_only(self):
        marker = "[[PC_PHOTO_SENT_NO_FOLLOWUP]]"
        mixed = f"图片已经发出。{marker}"
        visible_brackets = "图片已经发出。[[正常备注]]"

        for cleaner in (_strip_internal_message_blocks, _strip_outbound_control_blocks):
            with self.subTest(cleaner=cleaner.__name__, case="marker_only"):
                self.assertEqual("", cleaner(marker))
            with self.subTest(cleaner=cleaner.__name__, case="mixed"):
                self.assertEqual("图片已经发出。", cleaner(mixed))
            with self.subTest(cleaner=cleaner.__name__, case="unrelated_brackets"):
                self.assertEqual(visible_brackets, cleaner(visible_brackets))

    def test_proactive_archive_uses_internal_marker_instead_of_visible_placeholder(self):
        harness = _Harness()

        archived = harness._build_proactive_archive_assistant_text(
            text="主动正文",
            image_path="image.png",
            action_summary="发图",
        )

        self.assertIn('<pc_history_media images="1" />', archived)
        self.assertNotIn("随消息发送了一张图片", archived)

    async def test_raw_assistant_is_deferred_and_only_delivered_text_is_persisted(self):
        captured: list[str] = []

        async def livingmemory_handler(_event, response):
            captured.append(response.completion_text)

        handler = SimpleNamespace(
            handler=livingmemory_handler,
            handler_name="handle_memory_reflection",
            handler_module_path=LIVING_MODULE,
        )
        memory_companion_handler = SimpleNamespace(
            handler=livingmemory_handler,
            handler_name="capture_assistant_response",
            handler_module_path=MEMORY_COMPANION_MODULE,
        )
        plugins = {
            LIVING_MODULE: SimpleNamespace(
                name="LivingMemory", activated=True, reserved=False
            ),
            MEMORY_COMPANION_MODULE: SimpleNamespace(
                name="MemoryCompanion", activated=True, reserved=False
            ),
            COMPANION_MODULE: SimpleNamespace(
                name="Private Companion", activated=True, reserved=False
            ),
        }
        harness = _Harness()
        event = _Event()
        run_context = SimpleNamespace(
            messages=[
                Message(role="user", content=[TextPart(text="真实用户消息")]),
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")]),
            ]
        )

        with patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_handlers_registry",
            _Registry([handler, memory_companion_handler]),
        ), patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_map",
            plugins,
        ):
            self.assertTrue(harness._defer_livingmemory_response_capture(event))
            self.assertEqual(["Private Companion"], event.plugins_name)

            harness._prepare_final_response_persistence(
                event,
                run_context,
                LLMResponse(role="assistant", completion_text="审核改写回复"),
            )
            self.assertTrue(run_context.messages[-1]._no_save)
            harness._restore_livingmemory_response_capture(event)
            self.assertIsNone(event.plugins_name)

            await harness._finalize_passive_delivered_response(
                event,
                chain=[Plain("实际发送回复")],
            )

        self.assertEqual("实际发送回复", captured[-1])
        self.assertEqual(["实际发送回复"], harness.memory_companion_captured)
        self.assertFalse(run_context.messages[-1]._no_save)
        self.assertEqual(
            "实际发送回复",
            harness._message_content_text(run_context.messages[-1]),
        )
        # AstrBot serializes run_context only after RespondStage and its
        # after-message-sent hooks return.
        harness.conversation_manager.history = [
            item.model_dump()
            for item in run_context.messages
            if not item._no_save
        ]
        self.assertEqual(
            ["user", "assistant"],
            [item["role"] for item in harness.conversation_manager.history],
        )
        self.assertEqual(
            "实际发送回复",
            harness.conversation_manager.history[-1]["content"][0]["text"],
        )

    async def test_passive_official_history_falls_back_to_direct_append_without_agent_turn(self):
        harness = _Harness()
        event = _Event()

        written = await harness._finalize_passive_delivered_response(
            event,
            chain=[Plain("特殊发送路径的实际回复")],
        )

        self.assertTrue(written)
        self.assertEqual(
            "特殊发送路径的实际回复",
            harness.conversation_manager.history[-1]["content"],
        )

    async def test_missing_memory_plugins_does_not_block_official_history(self):
        harness = _Harness()
        event = _Event()

        with patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_handlers_registry",
            _Registry([]),
        ), patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_map",
            {},
        ):
            self.assertFalse(harness._defer_livingmemory_response_capture(event))
            written = await harness._finalize_passive_delivered_response(
                event,
                chain=[Plain("没有记忆插件也要保存")],
            )

        self.assertTrue(written)
        self.assertEqual(
            "没有记忆插件也要保存",
            harness.conversation_manager.history[-1]["content"],
        )

    async def test_proactive_placeholder_stays_out_of_livingmemory(self):
        captured: list[str] = []

        async def livingmemory_handler(_event, response):
            captured.append(response.completion_text)

        handler = SimpleNamespace(
            handler=livingmemory_handler,
            handler_name="handle_memory_reflection",
            handler_module_path=LIVING_MODULE,
        )
        plugins = {
            LIVING_MODULE: SimpleNamespace(
                name="LivingMemory", activated=True, reserved=False
            )
        }
        harness = _Harness()

        with patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_handlers_registry",
            _Registry([handler]),
        ), patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_map",
            plugins,
        ):
            await harness._archive_proactive_message_to_conversation(
                user={"umo": UMO},
                user_prompt="【主动承接占位】",
                assistant_response="实际发出的主动消息",
            )
            await harness._record_final_assistant_in_livingmemory(
                umo=UMO,
                assistant_response="实际发出的主动消息",
                delivery_id="proactive-1",
            )

        self.assertEqual(
            ["user", "assistant"],
            [item["role"] for item in harness.conversation_manager.history[-2:]],
        )
        self.assertEqual("【主动承接占位】", harness.conversation_manager.history[-2]["content"])
        self.assertEqual(["实际发出的主动消息"], captured)

    async def test_livingmemory_prefers_plugin_public_handler_when_available(self):
        direct_handler = AsyncMock()
        registry_handler = AsyncMock()
        handler = SimpleNamespace(
            handler=registry_handler,
            handler_name="handle_memory_reflection",
            handler_module_path=LIVING_MODULE,
        )
        plugins = {
            LIVING_MODULE: SimpleNamespace(
                name="LivingMemory",
                activated=True,
                reserved=False,
                star_cls=SimpleNamespace(handle_memory_reflection=direct_handler),
            )
        }
        harness = _Harness()

        with patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_handlers_registry",
            _Registry([handler]),
        ), patch(
            "astrbot_plugin_private_companion.final_response_persistence.star_map",
            plugins,
        ):
            written = await harness._record_final_assistant_in_livingmemory(
                umo=UMO,
                assistant_response="平台确认后的回复",
                delivery_id="direct-livingmemory-1",
            )

        self.assertTrue(written)
        direct_handler.assert_awaited_once()
        registry_handler.assert_not_awaited()

    async def test_send_tracker_persists_only_after_successful_send(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent()
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")])
            ]
        )

        await plugin.capture_final_outbound_chain_for_persistence(event)
        # The agent finished before the platform confirmed delivery, so the
        # official assistant message is staged before the after-sent finalizer.
        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="审核后的候选回复"),
        )
        outbound = SimpleNamespace(chain=[Plain("适配器实际接收的回复")])
        await event.send(outbound)
        await plugin.persist_confirmed_passive_reply(event)

        plugin._finalize_passive_delivered_response.assert_awaited_once()
        call = plugin._finalize_passive_delivered_response.await_args
        self.assertEqual("适配器实际接收的回复", call.kwargs["fallback_text"])
        self.assertTrue(call.kwargs["force"])
        self.assertFalse(event._private_companion_send_tracking_installed)

    async def test_send_tracker_does_not_persist_failed_send(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent(send_error=RuntimeError("adapter send failed"))
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")])
            ]
        )

        await plugin.capture_final_outbound_chain_for_persistence(event)
        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="审核后的候选回复"),
        )
        with self.assertRaisesRegex(RuntimeError, "adapter send failed"):
            await event.send(SimpleNamespace(chain=[Plain("不会送达")]))
        await plugin.persist_confirmed_passive_reply(event)

        plugin._finalize_passive_delivered_response.assert_not_awaited()
        self.assertFalse(event._private_companion_send_tracking_installed)

    async def test_send_tracker_treats_explicit_false_as_failed_send(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent(send_result=False)
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")])
            ]
        )

        await plugin.capture_final_outbound_chain_for_persistence(event)
        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="审核后的候选回复"),
        )
        result = await event.send(SimpleNamespace(chain=[Plain("平台拒绝接收")]))
        await plugin.persist_confirmed_passive_reply(event)

        self.assertFalse(result)
        plugin._finalize_passive_delivered_response.assert_not_awaited()
        self.assertFalse(event._private_companion_send_tracking_installed)

    async def test_passive_finalizer_waits_for_segmented_remainder(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent()
        event._has_send_oper = False
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")])
            ]
        )

        plugin._begin_final_response_persistence(event)
        plugin._capture_final_outbound_delivery(event)
        # The agent finished before the segmented reply is sent, so the
        # official assistant message is staged first.
        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="审核后的候选回复"),
        )
        await event.send(SimpleNamespace(chain=[Plain("第一段")]))

        async def send_remainder():
            await asyncio.sleep(0)
            await event.send(SimpleNamespace(chain=[Plain("第二段")]))

        task = asyncio.create_task(send_remainder())
        plugin._track_final_response_background_task(
            task,
            "segmented_llm_remainder",
        )
        await plugin.persist_confirmed_passive_reply(event)

        plugin._finalize_passive_delivered_response.assert_awaited_once()
        call = plugin._finalize_passive_delivered_response.await_args
        self.assertEqual("第一段\n第二段", call.kwargs["fallback_text"])

    async def test_direct_send_that_stops_event_uses_fallback_finalizer(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent()
        event._has_send_oper = False
        event.stopped = True

        plugin._begin_final_response_persistence(event)
        await event.send(SimpleNamespace(chain=[Plain("直接发送后终止传播")]))
        ledger = event._private_companion_delivery_ledger
        self.assertIsNotNone(ledger.fallback_task)
        await asyncio.wait_for(ledger.fallback_task, timeout=1)

        plugin._finalize_passive_delivered_response.assert_awaited_once()
        call = plugin._finalize_passive_delivered_response.await_args
        self.assertEqual("直接发送后终止传播", call.kwargs["fallback_text"])

    async def test_tool_intermediate_send_waits_for_final_reply(self):
        # A tool-calling turn sends intermediate assistant text before the
        # agent finishes. The intermediate send must NOT be finalised: doing
        # so would lock the ledger before the real reply arrives, leaving the
        # real reply's _no_save flag stuck True and dropping it from history.
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent()
        run_context = SimpleNamespace(
            messages=[
                Message(
                    role="assistant",
                    content=[TextPart(text="好的，我先把这条记下来")],
                ),
            ]
        )

        plugin._begin_final_response_persistence(event)

        # Intermediate assistant text of the tool call is sent while the agent
        # is still running (no official assistant message staged yet).
        await event.send(SimpleNamespace(chain=[Plain("好的，我先把这条记下来")]))
        await plugin.persist_confirmed_passive_reply(event)

        # The intermediate send must not finalize nor lock the ledger.
        plugin._finalize_passive_delivered_response.assert_not_awaited()
        self.assertFalse(event._private_companion_delivery_ledger.finalized)

        # The runner appends the final assistant message before on_agent_done.
        run_context.messages.append(
            Message(role="assistant", content=[TextPart(text="已记录")])
        )
        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="已记录"),
        )

        # Final reply is sent and only now does the finalizer run.
        await event.send(SimpleNamespace(chain=[Plain("已记录")]))
        await plugin.persist_confirmed_passive_reply(event)

        plugin._finalize_passive_delivered_response.assert_awaited_once()
        call = plugin._finalize_passive_delivered_response.await_args
        self.assertEqual("已记录", call.kwargs["fallback_text"])
        self.assertEqual(
            run_context.messages[-1],
            event._private_companion_official_assistant_message,
        )
        self.assertTrue(event._private_companion_delivery_ledger.finalized)

    async def test_proactive_collector_replaces_candidate_with_confirmed_chain(self):
        outcome = await _ActiveCollector().send(UMO)

        self.assertTrue(outcome.delivered)
        self.assertEqual(UMO, outcome.delivery_umo)
        self.assertEqual("平台实际收到的主动回复", outcome.delivered_text)
        self.assertEqual(
            "平台实际收到的主动回复",
            outcome.delivered_chain[0].text,
        )

    async def test_streaming_response_persists_only_confirmed_stream_chunks(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._finalize_passive_delivered_response = AsyncMock(return_value=True)
        event = _SendTrackerEvent()

        async def send_streaming(generator, *_args, **_kwargs):
            async for _ in generator:
                pass

        event.send_streaming = send_streaming
        event._has_send_oper = False
        plugin._begin_final_response_persistence(event)
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="Agent 原始回复")])
            ]
        )

        await plugin._prepare_final_response_after_agent(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text="审核后的候选回复"),
        )

        async def chunks():
            yield SimpleNamespace(chain=[Plain("实际流式")])
            yield SimpleNamespace(chain=[Plain("发送回复")])

        await event.send_streaming(chunks())
        await plugin.persist_confirmed_passive_reply(event)

        plugin._finalize_passive_delivered_response.assert_awaited_once()
        call = plugin._finalize_passive_delivered_response.await_args
        self.assertEqual("实际流式发送回复", call.kwargs["fallback_text"])
        self.assertTrue(call.kwargs["force"])

    async def test_intercepted_reply_does_not_dispatch_or_append_assistant(self):
        harness = _Harness()
        event = _Event()
        run_context = SimpleNamespace(
            messages=[
                Message(role="assistant", content=[TextPart(text="会被拦截的原始回复")])
            ]
        )

        harness._prepare_final_response_persistence(
            event,
            run_context,
            LLMResponse(role="assistant", completion_text=""),
        )

        self.assertTrue(run_context.messages[-1]._no_save)
        self.assertEqual(
            [{"role": "user", "content": "真实用户消息"}],
            harness.conversation_manager.history,
        )


if __name__ == "__main__":
    unittest.main()
