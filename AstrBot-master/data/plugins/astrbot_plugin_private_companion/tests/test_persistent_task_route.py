import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from jsonschema.exceptions import ValidationError

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


class PersistentTaskRouteTests(unittest.IsolatedAsyncioTestCase):
    def setup_route(self, route="task"):
        runtime = SimpleNamespace(
            register=MagicMock(return_value=True),
            submit=AsyncMock(return_value={"accepted": True, "task_id": "persisted"}),
            snapshot=MagicMock(return_value="持久任务：排队中"),
            make_intent_tool=MagicMock(return_value=FunctionTool(
                name="pc_submit_task_intent", description="Submit intent", parameters={"type": "object"},
            )),
        )
        decision = SimpleNamespace(reply=True, route=route, actions=("search",), segments=(("task", "查询官方公告"),))
        event = SimpleNamespace(
            _private_companion_capability_decision=decision,
            unified_msg_origin="bot:FriendMessage:u", message_str="查询官方公告",
            stop_event=MagicMock(),
        )
        plugin = SimpleNamespace(
            enabled=True, enable_conversation_model_routing=True,
            _persistent_task_runtime=runtime, enable_reply_task_intents=True,
        )
        return plugin, event, ProviderRequest(prompt=event.message_str), runtime

    async def invoke(self, plugin, event, req):
        handler = PrivateCompanionPlugin.route_conversation_models.__wrapped__
        with patch("astrbot_plugin_private_companion.main.configure_conversation_request") as configure:
            await handler(plugin, event, req)
        return configure

    async def test_task_goes_to_service_without_running_old_request(self):
        plugin, event, req, runtime = self.setup_route()
        configure = await self.invoke(plugin, event, req)
        runtime.submit.assert_awaited_once()
        event.stop_event.assert_called_once()
        configure.assert_not_called()
        self.assertNotIn("_private_companion_task_id", vars(event))

    async def test_task_handoff_releases_only_current_pending_debounce(self):
        plugin, event, req, runtime = self.setup_route()
        plugin.settle_pending_message_debounce = AsyncMock()
        await self.invoke(plugin, event, req)
        plugin.settle_pending_message_debounce.assert_awaited_once_with(event, None)

    async def test_chat_keeps_single_supplemental_tool_and_snapshot(self):
        plugin, event, req, runtime = self.setup_route("chat")
        req.func_tool = ToolSet()
        await self.invoke(plugin, event, req)
        self.assertIn("持久任务", req.system_prompt)
        self.assertEqual(["pc_submit_task_intent"], [tool.name for tool in req.func_tool.tools])
        runtime.submit.assert_not_awaited()

    async def test_silent_event_never_gets_supplemental_entry(self):
        plugin, event, req, runtime = self.setup_route("chat")
        event._private_companion_capability_decision.reply = False
        await self.invoke(plugin, event, req)
        runtime.register.assert_not_called()
        runtime.submit.assert_not_awaited()

    async def test_existing_task_query_uses_original_chat_request_without_submission(self):
        plugin, event, req, runtime = self.setup_route()
        event.message_str = "查到了吗"
        req.prompt = event.message_str
        req.system_prompt = "原聊天人格和记忆"
        runtime.prepare_task_query = MagicMock(return_value=True)
        configure = await self.invoke(plugin, event, req)
        runtime.submit.assert_not_awaited()
        configure.assert_called_once()
        self.assertEqual("chat", event._private_companion_capability_decision.route)
        self.assertEqual((), event._private_companion_capability_decision.actions)
        self.assertEqual("查到了吗", req.prompt)
        self.assertIn("原聊天人格和记忆", req.system_prompt)
        self.assertNotIn("pc_submit_task_intent", [t.name for t in (req.func_tool.tools if req.func_tool else [])])

    async def test_failed_persistence_does_not_run_old_task(self):
        plugin, event, req, runtime = self.setup_route()
        runtime.submit.return_value = {"accepted": False, "code": "permission_denied"}
        event.send = AsyncMock()
        event.plain_result = lambda value: value
        configure = await self.invoke(plugin, event, req)
        configure.assert_not_called()
        event.stop_event.assert_called_once()

    async def test_tool_schema_failure_is_not_reported_as_model_configuration(self):
        plugin, event, req, runtime = self.setup_route()
        runtime.register.side_effect = ValidationError("api_key=topsecret invalid required")
        event.send = AsyncMock()
        event.plain_result = lambda value: value
        with patch("astrbot_plugin_private_companion.main.logger.warning") as warning:
            configure = await self.invoke(plugin, event, req)
        configure.assert_not_called()
        runtime.submit.assert_not_awaited()
        event.stop_event.assert_called_once()
        message = event.send.await_args.args[0]
        self.assertIn("工具定义校验失败", message)
        self.assertNotIn("模型设置", message)
        self.assertNotIn("topsecret", message)
        self.assertIn("invalid required", str(warning.call_args))
        self.assertNotIn("topsecret", str(warning.call_args))

    async def test_mixed_keeps_chat_and_separate_stable_task_fragments(self):
        plugin, event, req, runtime = self.setup_route("mixed")
        event._private_companion_capability_decision.segments = (
            ("chat", "今天辛苦了"), ("task", "查询公告"), ("task", "写分析"),
        )
        configure = await self.invoke(plugin, event, req)
        self.assertEqual(2, runtime.submit.await_count)
        self.assertEqual(["main", "task:1"], [call.args[1]["fragment_key"] for call in runtime.submit.await_args_list])
        event.stop_event.assert_not_called()
        configure.assert_called_once()
        self.assertEqual("chat", event._private_companion_capability_decision.route)
        self.assertIn("今天辛苦了", req.prompt)
