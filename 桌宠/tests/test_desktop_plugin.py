import ast
import base64
import importlib
import inspect
import json
import re
import sys
import types
import typing
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer


def real_command_parser():
    """只读提取本机真实框架类，避免加载整个AstrBot运行环境。"""
    source = Path(
        "C:/Test_Program/AstrBot/AstrBot-master/astrbot/core/star/filter/command.py"
    )
    if not source.exists():
        pytest.skip("真实AstrBot源码不在本测试机，跳过框架兼容性检查")
    parsed = ast.parse(source.read_text(encoding="utf-8"))
    selected = [
        node
        for node in parsed.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {"GreedyStr", "unwrap_optional", "CommandFilter"}
    ]
    namespace = {
        "inspect": inspect,
        "re": re,
        "types": types,
        "typing": typing,
        "Any": typing.Any,
        "AstrBotConfig": dict,
        "AstrMessageEvent": object,
        "StarHandlerMetadata": object,
        "HandlerFilter": object,
        "CustomFilter": object,
    }
    exec(  # noqa: S102 -- 只执行明确路径下只读提取的真实框架类，检查解析兼容性。
        compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"),
        namespace,
    )
    return namespace


@pytest.fixture
def plugin_module(tmp_path, monkeypatch):
    path = (
        __import__("pathlib").Path(__file__).parents[1]
        / "cloud_plugin/astrbot_plugin_desktop_link/main.py"
    )
    assert path.exists(), "尚未实现AstrBot桌宠插件"
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None)
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        command=lambda *a, **k: lambda f: f, llm_tool=lambda *a, **k: lambda f: f
    )
    star = types.ModuleType("astrbot.api.star")

    class FakeStar:
        def __init__(self, context):
            self.context = context

    star.Context = object
    star.Star = FakeStar
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda *a: tmp_path)
    star.register = lambda *a, **k: lambda cls: cls
    command = types.ModuleType("astrbot.core.star.filter.command")
    real_parser = real_command_parser()
    command.GreedyStr = real_parser["GreedyStr"]
    command.CommandFilter = real_parser["CommandFilter"]
    for name, value in {
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.star": star,
        "astrbot.core.star.filter.command": command,
    }.items():
        monkeypatch.setitem(sys.modules, name, value)
    module_name = "cloud_plugin.astrbot_plugin_desktop_link.main"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class FakeEvent:
    def __init__(self, admin=True, group=""):
        self.admin = admin
        self.group = group
        self.is_at_or_wake_command = True
        self.message = ""
        self.extra = {}

    def is_admin(self):
        return self.admin

    def get_sender_id(self):
        return "owner"

    def get_group_id(self):
        return self.group

    def plain_result(self, text):
        return text

    def get_message_str(self):
        return self.message

    def set_extra(self, key, value):
        self.extra[key] = value


def fake_context():
    return types.SimpleNamespace(
        get_config=lambda *a: {
            "admins_id": ["owner"],
            "dashboard": {"jwt_secret": "test-only-secret"},
        }
    )


@pytest.mark.asyncio
async def test_pair_command_is_private_admin_only(plugin_module):
    plugin = plugin_module.DesktopLinkPlugin(fake_context(), {})
    denied = [
        x
        async for x in plugin.desktop_command(
            FakeEvent(admin=False), "配对 pc-one 我的电脑"
        )
    ]
    assert "管理员" in denied[0]
    assert plugin.server.devices() == []
    group = [
        x
        async for x in plugin.desktop_command(
            FakeEvent(group="123"), "配对 pc-one 我的电脑"
        )
    ]
    assert "私聊" in group[0]
    paired = [
        x async for x in plugin.desktop_command(FakeEvent(), "配对 pc-one 我的电脑")
    ]
    encoded = paired[0].split("配对码：", 1)[1].strip()
    assert encoded.startswith("desktop-link:")
    encoded = encoded.removeprefix("desktop-link:")
    data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert data["url"] == "wss://localhost/desktop/ws"
    assert data["device_id"] == "pc-one"
    assert len(data["token"]) > 30
    await plugin.terminate()


@pytest.mark.asyncio
async def test_tools_reject_non_admin_and_unknown_operation(plugin_module):
    plugin = plugin_module.DesktopLinkPlugin(fake_context(), {})
    result = await plugin.desktop_devices(FakeEvent(admin=False))
    assert "管理员" in result
    plugin.server.pair("one", "一")
    result = await plugin.desktop_send_command(FakeEvent(), "one", "shell", "{}")
    assert "say" in result
    result = await plugin.desktop_send_command(FakeEvent(), "one", "status", "{}")
    command = json.loads(result)
    assert command["status"] == "queued"
    assert "token" not in await plugin.desktop_devices(FakeEvent())
    await plugin.terminate()


@pytest.mark.asyncio
async def test_cloud_chat_uses_loopback_jwt_and_stable_webchat_session(plugin_module):
    seen = []

    async def new_session(request):
        seen.append(("new", request.headers["Authorization"]))
        return web.json_response(
            {"status": "ok", "data": {"session_id": "fixed-session"}}
        )

    async def send(request):
        seen.append(("send", await request.json()))
        return web.Response(
            body=(
                'data: {"type":"plain","data":"你好","streaming":true}\n\n'
                'data: {"type":"plain","data":"呀","streaming":true}\n\n'
                'data: {"type":"end","data":""}\n\n'
            ).encode(),
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_get("/api/chat/new_session", new_session)
    app.router.add_post("/api/chat/send", send)
    async with TestServer(app) as local:
        plugin = plugin_module.DesktopLinkPlugin(
            fake_context(), {"dashboard_port": local.port}
        )
        plugin.server.pair("one", "一")
        assert await plugin._cloud_chat("one", "第一条") == "你好呀"
        assert await plugin._cloud_chat("one", "第二条") == "你好呀"
        assert [item[0] for item in seen] == ["new", "send", "send"]
        token = seen[0][1].split(" ", 1)[1]
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        assert payload["username"].startswith("desktop_")
        assert seen[1][1]["session_id"] == "fixed-session"
        assert seen[2][1]["session_id"] == "fixed-session"
        await plugin.terminate()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("桌宠 配对 pc-one 我的电脑", "配对 pc-one 我的电脑"),
        ("桌宠 发送 pc-one 你好 世界", "发送 pc-one 你好 世界"),
        ("桌宠", ""),
    ],
)
def test_real_astrbot_command_filter_keeps_greedy_arguments(
    plugin_module, text, expected
):
    command_module = sys.modules["astrbot.core.star.filter.command"]
    parser = command_module.CommandFilter("桌宠")
    parser.init_handler_md(
        types.SimpleNamespace(handler=plugin_module.DesktopLinkPlugin.desktop_command)
    )
    event = FakeEvent()
    event.message = text
    assert parser.filter(event, {}) is True
    assert event.extra["parsed_params"] == {"action": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (
            [
                {"type": "plain", "data": "第一句", "streaming": False},
                {"type": "plain", "data": "第二句", "streaming": False},
                {"type": "end", "data": ""},
            ],
            "第一句\n第二句",
        ),
        (
            [
                {"type": "plain", "data": "流", "streaming": True},
                {"type": "plain", "data": "式", "streaming": True},
                {"type": "plain", "data": "独立消息", "streaming": False},
                {"type": "plain", "data": "最后", "streaming": True},
                {"type": "complete", "data": "流式独立消息最后"},
                {"type": "end", "data": ""},
            ],
            "流式\n独立消息\n最后",
        ),
        (
            [
                {"type": "plain", "data": "第一句", "streaming": False},
                {"type": "complete", "data": "第一句"},
                {
                    "type": "plain",
                    "data": "推理内容",
                    "streaming": True,
                    "chain_type": "reasoning",
                },
                {"type": "plain", "data": "第二句", "streaming": False},
                {"type": "complete", "data": "第二句"},
                {"type": "end", "data": ""},
            ],
            "第一句\n第二句",
        ),
    ],
)
async def test_cloud_chat_preserves_mixed_text_segments(
    plugin_module, events, expected
):
    async def send(request):
        return web.Response(
            body="".join(
                "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                for item in events
            ).encode(),
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/api/chat/send", send)
    async with TestServer(app) as local:
        plugin = plugin_module.DesktopLinkPlugin(
            fake_context(), {"dashboard_port": local.port}
        )
        plugin.server.pair("one", "一")
        plugin.server.set_session("one", "fixed-session")
        try:
            assert await plugin._cloud_chat("one", "测试") == expected
        finally:
            await plugin.terminate()
