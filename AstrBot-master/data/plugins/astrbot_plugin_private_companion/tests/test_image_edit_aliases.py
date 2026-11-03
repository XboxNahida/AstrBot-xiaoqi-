import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

from astrbot_plugin_private_companion.capability_hub import (
    build_capability_context,
    parse_model_capability_decision,
)
from astrbot_plugin_private_companion.command_handlers import CommandHandlersMixin
from astrbot_plugin_private_companion.image_history import ImageHistoryStore
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class ImageEditAliasTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = PrivateCompanionPlugin.__new__(PrivateCompanionPlugin)
        plugin._proactive_only_blocks_passive_event = lambda *args: False
        plugin._reaction_expression_authorization = lambda event: None
        plugin._pc_generate_photo_impl = AsyncMock(return_value='{"status":"success"}')
        return plugin

    async def test_image_edit_alias_uses_edit_authorization_and_workflow(self):
        plugin = self.make_plugin()
        event = SimpleNamespace(
            message_str="改成二次元风格图片",
            _private_companion_capability_decision=SimpleNamespace(actions=("image_edit",)),
        )
        result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
            plugin, event, prompt="将参考照片改绘为二次元插画", kind="image_edit"
        )
        self.assertEqual("success", json.loads(result)["status"])
        self.assertEqual("edit", plugin._pc_generate_photo_impl.await_args.kwargs["kind"])

    async def test_edit_alias_cannot_bypass_hub_authorization(self):
        plugin = self.make_plugin()
        for actions in (("image_generate",), ("text_only",), ()):
            with self.subTest(actions=actions):
                event = SimpleNamespace(
                    message_str="看看这个图片",
                    _private_companion_capability_decision=SimpleNamespace(actions=actions),
                )
                result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
                    plugin, event, prompt="改图", kind="image_edit"
                )
                self.assertEqual("hub_denied_image_edit", json.loads(result)["skip_reason"])
        plugin._pc_generate_photo_impl.assert_not_awaited()

    async def test_reference_generation_can_use_edit_workflow(self):
        for text in (
            "那我要让你以这个为参考图，生成q版头像，能读到引用图吗",
            "参考这张图生成一个头像",
            "以引用图片为参考画一张Q版头像",
        ):
            with self.subTest(text=text):
                plugin = self.make_plugin()
                event = SimpleNamespace(
                    message_str=text,
                    _private_companion_capability_decision=SimpleNamespace(actions=("image_generate",)),
                )
                result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
                    plugin, event, prompt="chibi avatar based on reference", kind="edit"
                )
                self.assertEqual("success", json.loads(result)["status"])
                self.assertEqual("edit", plugin._pc_generate_photo_impl.await_args.kwargs["kind"])

    async def test_model_prompt_cannot_authorize_reference_edit(self):
        for text, actions in (
            ("生成一张星空", ("image_generate",)),
            ("这个参考图你能看到吗", ("image_generate",)),
            ("参考这张图生成头像", ("vision",)),
            ("参考这张图生成头像", ()),
        ):
            with self.subTest(text=text, actions=actions):
                plugin = self.make_plugin()
                event = SimpleNamespace(
                    message_str=text,
                    _private_companion_capability_decision=SimpleNamespace(actions=actions),
                )
                result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
                    plugin, event, prompt="参考这张图生成头像", kind="edit"
                )
                self.assertEqual("hub_denied_image_edit", json.loads(result)["skip_reason"])
                plugin._pc_generate_photo_impl.assert_not_awaited()

    async def test_hub_approved_reference_retry_reaches_edit_workflow(self):
        for text in ("充了，你再试试看", "按刚刚要求重新生图"):
            with self.subTest(text=text):
                plugin = self.make_plugin()
                context = build_capability_context(
                    scope="bot-a:private:user-a", current_text=text,
                    sender_id="user-a", sender_name="用户", now=None,
                )
                decision = parse_model_capability_decision(
                    json.dumps({"reply": True, "actions": ["image_generate"],
                                "image_edit_requested": True, "route": "task"}),
                    available_capabilities={"image_generate", "image_edit"},
                    context=context,
                )
                event = SimpleNamespace(
                    message_str=text, _private_companion_capability_decision=decision,
                )
                result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
                    plugin, event, prompt="按此前要求使用刚才引用的原图生成Q版头像", kind="edit"
                )
                self.assertEqual("success", json.loads(result)["status"])
                self.assertEqual("edit", plugin._pc_generate_photo_impl.await_args.kwargs["kind"])

    async def test_reference_retry_requires_hub_action_and_boolean_intent(self):
        for reply, actions, intent in (
            (True, ["image_generate"], False),
            (True, ["image_generate"], "true"),
            (True, ["image_generate"], 1),
            (True, ["vision"], True),
            (True, [], True),
            (False, ["image_generate"], True),
        ):
            with self.subTest(reply=reply, actions=actions, intent=intent):
                plugin = self.make_plugin()
                context = build_capability_context(
                    scope="bot-a:private:user-a", current_text="充了，你再试试看",
                    sender_id="user-a", sender_name="用户", now=None,
                )
                decision = parse_model_capability_decision(
                    json.dumps({"reply": reply, "actions": actions,
                                "image_edit_requested": intent}),
                    available_capabilities={"image_generate", "image_edit", "vision"},
                    context=context,
                )
                event = SimpleNamespace(
                    message_str=context.current_text,
                    _private_companion_capability_decision=decision,
                )
                result = await PrivateCompanionPlugin.pc_generate_photo.__wrapped__(
                    plugin, event, prompt="参考原图生成头像", kind="edit"
                )
                self.assertEqual("hub_denied_image_edit", json.loads(result)["skip_reason"])
                plugin._pc_generate_photo_impl.assert_not_awaited()


class ImageHistoryReferenceTests(unittest.TestCase):
    def test_explicit_this_picture_reference_resolves_only_current_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.png"
            Image.new("RGB", (8, 8), "blue").save(source)
            store = ImageHistoryStore(temp, {})
            current = "bot-a:private:user-a"
            item = store.add_file(current, "user", "user-a", "", source)
            store.add_file("bot-b:private:user-a", "user", "user-a", "", source)
            store.add_file("bot-a:private:user-b", "user", "user-b", "", source)
            harness = CommandHandlersMixin()
            harness._image_history_store = store
            harness._event_scope_key = lambda event: event.scope
            for text in ("以这个图为参考图，生成头像", "参考这幅图的服装", "以此图为参考", "参考该图外观"):
                with self.subTest(text=text):
                    event = SimpleNamespace(scope=current, message_str=text)
                    sources = harness._photo_reference_sources_from_history(event, prompt="头像插画")
                    self.assertEqual([str(store.resolve(current, item["image_id"]))], sources)
            event = SimpleNamespace(scope="bot-c:private:user-a", message_str="以这个图为参考图")
            self.assertEqual([], harness._photo_reference_sources_from_history(event))

    def test_ordinary_chat_does_not_attach_history(self):
        for text in ("今天吃什么", "你喜欢这个风格吗", "参考答案是什么", "生成一张星空"):
            with self.subTest(text=text):
                self.assertFalse(CommandHandlersMixin._photo_reference_history_prompt_matches(text))
