from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from astrbot.api.message_components import Image as ImageComponent, Reply
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class InlineQuoteImageReferenceTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, root: Path):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin.data_dir = str(root)
        plugin.data = {}
        plugin._image_history_store = None
        plugin._save_data_sync = lambda: None
        plugin._event_scope_key = lambda event: event.scope
        plugin._event_components = lambda event: event.message_obj.message
        plugin._event_is_platform_message_event = lambda event: True
        plugin._event_raw_payload = lambda event: {"post_type": "message"}
        plugin._event_message_id_candidates = lambda event: [event.message_id]
        plugin._event_message_id = lambda event: event.message_id
        plugin._event_reply_message_ids = lambda event: []
        plugin._event_text_for_recall_cache = lambda event, **kwargs: event.message_str
        plugin._event_sender_id = lambda event: "user-a"
        plugin._event_self_id = lambda event: "bot-a"
        plugin._sender_display_name = lambda event: "用户"
        plugin._cleanup_recall_message_cache = lambda: None
        plugin._cleanup_framework_delivery_caches = lambda: None
        plugin._find_reply_image_sources_for_event = AsyncMock(return_value=[])
        plugin._raw_private_image_sources = lambda event: []
        plugin._persist_private_inbound_images = AsyncMock(return_value=[])
        return plugin

    def make_event(self, image: Path, *, scope="bot-a:private:user-a"):
        quote = Reply(id="", chain=[ImageComponent.fromFileSystem(str(image))])
        return SimpleNamespace(
            scope=scope,
            message_id="quote-event",
            message_str="这个图片，你看得到吗",
            message_obj=SimpleNamespace(message=[quote]),
        )

    async def test_quote_without_message_id_is_a_real_generation_reference(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "platform" / "quoted.png"
            source.parent.mkdir()
            Image.new("RGB", (10, 10), "blue").save(source)
            plugin = self.make_plugin(root / "plugin")
            event = self.make_event(source)
            images, seen = await plugin._photo_reference_images_from_command_context(event, "user-a")
            self.assertTrue(seen)
            self.assertEqual(1, len(images))
            self.assertEqual(source.read_bytes(), Path(images[0][0]).read_bytes())
            self.assertEqual("引用消息里的图片", images[0][1])

    async def test_quote_is_persisted_for_followup_in_same_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "quoted.png"
            Image.new("RGB", (10, 10), "blue").save(source)
            plugin = self.make_plugin(root / "plugin")
            event = self.make_event(source)
            await plugin._cache_message_for_recall(event)
            await plugin._cache_message_for_recall(event)
            rows = plugin._image_history_store.list(event.scope)
            self.assertEqual(1, len(rows))
            source.unlink()
            followup = SimpleNamespace(scope=event.scope, message_str="那我要让你以这个为参考图，生成q版头像")
            paths = plugin._photo_reference_sources_from_history(followup)
            self.assertEqual(1, len(paths))
            self.assertTrue(Path(paths[0]).is_file())
            for other_scope in ("bot-b:private:user-a", "bot-a:private:user-b", "bot-a:group:g1"):
                followup.scope = other_scope
                self.assertEqual([], plugin._photo_reference_sources_from_history(followup))

    async def test_missing_explicit_quote_does_not_replace_it_with_old_image(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "old.png"
            Image.new("RGB", (10, 10), "red").save(source)
            plugin = self.make_plugin(root / "plugin")
            event = self.make_event(root / "missing.png")
            store = plugin._initialize_image_history_store()
            store.add_file(event.scope, "user", "user-a", "", source)
            images, seen = await plugin._photo_reference_images_from_command_context(
                event, "user-a", prompt="把这张图改成头像"
            )
            self.assertTrue(seen)
            self.assertEqual([], images)

    async def test_plain_text_quote_does_not_record_image_history(self):
        with tempfile.TemporaryDirectory() as folder:
            plugin = self.make_plugin(Path(folder))
            event = SimpleNamespace(
                scope="bot-a:private:user-a", message_id="text-quote", message_str="说得对",
                message_obj=SimpleNamespace(message=[Reply(id="", chain=[])]),
            )
            await plugin._cache_message_for_recall(event)
            self.assertIsNone(plugin._image_history_store)
