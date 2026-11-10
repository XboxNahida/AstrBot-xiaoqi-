# -*- coding: utf-8 -*-
from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class _Event:
    def __init__(
        self,
        text: str,
        *,
        private: bool = True,
        sender_id: str = "user-1",
        message_id: str = "message-1",
        group_id: str = "group-1",
    ) -> None:
        self.message_str = text
        self.unified_msg_origin = (
            f"default:FriendMessage:{sender_id}"
            if private
            else f"default:GroupMessage:{group_id}"
        )
        self._private = private
        self._sender_id = sender_id
        self._group_id = group_id
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            raw_message={
                "post_type": "message",
                "message_type": "private" if private else "group",
                "user_id": sender_id,
                "group_id": group_id,
            },
        )

    def is_private_chat(self) -> bool:
        return self._private

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_group_id(self) -> str:
        return self._group_id


class PendingMessageDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unified_queue_keeps_original_event_out_of_legacy_text_buffer(self):
        event = _Event("补充" * 500)
        event._private_companion_unified_queue = True
        self.plugin._message_debounce_mark_llm_pending(_Event("原问题"))
        self.assertFalse(self.plugin._message_debounce_absorb_pending_message(event, event.message_str))
        await self.plugin.guard_pending_message_debounce(event)
        self.assertFalse(getattr(event, "_private_companion_hub_pending_marked", False))

    def setUp(self) -> None:
        self.plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        self.plugin.enable_message_debounce = True
        self.plugin.text_message_debounce_seconds = 1.0
        self.plugin._semantic_message_buffers = {}

    async def test_private_followup_restores_consumed_prompt_and_discards_old_response(self) -> None:
        first = _Event("第一句", message_id="message-1")
        second = _Event("补充", message_id="message-2")
        key = "private:user-1:user-1"
        self.plugin._semantic_message_buffers[key] = {
            "first_ts": 1.0,
            "updated_ts": 1.0,
            "wait_seconds": 1.0,
            "kind": "text",
            "messages": [{"ts": 1.0, "text": "第一句", "sender_name": ""}],
        }

        self.plugin._message_debounce_mark_llm_pending(first)
        self.assertEqual("第一句", self.plugin._message_debounce_pending_llm[key]["prompt_text"])

        self.assertTrue(self.plugin._message_debounce_absorb_pending_message(second, "补充"))
        texts = [item["text"] for item in self.plugin._semantic_message_buffers[key]["messages"]]
        self.assertEqual(["第一句", "补充"], texts)
        self.assertIn("message-1", self.plugin._message_debounce_pending_llm[key]["stale_event_ids"])

        old_response = SimpleNamespace(completion_text="旧回复", result_chain=["旧链路"])
        await self.plugin.settle_pending_message_debounce(first, old_response)
        self.assertEqual("", old_response.completion_text)
        self.assertIsNone(old_response.result_chain)
        self.assertIn(key, self.plugin._message_debounce_pending_llm)

        self.plugin._message_debounce_mark_llm_pending(second)
        new_response = SimpleNamespace(completion_text="新回复")
        await self.plugin.settle_pending_message_debounce(second, new_response)
        self.assertEqual("新回复", new_response.completion_text)
        self.assertNotIn(key, self.plugin._message_debounce_pending_llm)

    def test_group_pending_isolated_by_sender_and_group(self) -> None:
        first = _Event("群里第一句", private=False, sender_id="user-1", message_id="message-1")
        other_sender = _Event("别人的补话", private=False, sender_id="user-2", message_id="message-2")
        other_group = _Event("另一群补话", private=False, sender_id="user-1", group_id="group-2", message_id="message-3")
        same_sender = _Event("同一人的补话", private=False, sender_id="user-1", message_id="message-4")

        self.plugin._message_debounce_mark_llm_pending(first)
        self.assertFalse(self.plugin._message_debounce_absorb_pending_message(other_sender, other_sender.message_str))
        self.assertFalse(self.plugin._message_debounce_absorb_pending_message(other_group, other_group.message_str))
        self.assertTrue(self.plugin._message_debounce_absorb_pending_message(same_sender, same_sender.message_str))

        key = "group:group-1:user-1"
        texts = [item["text"] for item in self.plugin._semantic_message_buffers[key]["messages"]]
        self.assertEqual(["群里第一句", "同一人的补话"], texts)

    async def test_waiting_hook_ignores_unrelated_group_request(self) -> None:
        first = _Event("群里第一句", private=False, sender_id="user-1", message_id="message-1")
        unrelated = _Event("其他插件的消息", private=False, sender_id="user-1", message_id="message-2")
        self.plugin._message_debounce_mark_llm_pending(first)

        await self.plugin.guard_pending_message_debounce(unrelated)

        self.assertEqual({}, self.plugin._semantic_message_buffers)


if __name__ == "__main__":
    unittest.main()
