"""短记忆接入正式聊天、旧资料迁移和通知的行为回归。"""

import copy
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.platform.message_type import MessageType
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_private_companion.group_observation import GroupObservationMixin
from astrbot_plugin_private_companion.tests.test_task_runtime import (  # noqa: F401
    event,
    register,
)
from astrbot_plugin_private_companion.tests.test_task_runtime import (
    runtime as runtime_fixture,
)
from astrbot_plugin_private_companion.user_memory import UserMemoryMixin


@pytest.fixture
def runtime(tmp_path):
    return runtime_fixture.__wrapped__(tmp_path)


def private_event(user="owner", mid="memory"):
    e = event(user, mid)
    e.message_obj.type = MessageType.FRIEND_MESSAGE
    e.message_obj.group_id = ""
    e.session_id = user
    e._private_companion_emotion_persona_id = "小七"
    return e


@pytest.mark.asyncio
async def test_each_chat_loads_entire_latest_note_without_forcing_a_write(runtime):
    e = private_event()
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    tool = runtime.test_request.func_tool.get_tool("pc_manage_memory")
    first = json.loads(await tool.call(None, action="read"))
    text = "称呼阿明；喜欢红茶；11月7日一起整理了报告；下周三考试。"
    assert json.loads(
        await tool.call(
            None, action="write", text=text, expected_revision=first["note"]["revision"]
        )
    )["ok"]
    other = private_event(mid="unrelated-message")
    register(runtime, other)
    await runtime.memory.attach(other, runtime.test_request)
    blocks = [part.text for part in runtime.test_request.extra_user_content_parts]
    assert sum(text in block for block in blocks) == 1
    assert "不必每轮" in runtime.test_request.system_prompt
    assert "800" in runtime.test_request.system_prompt
    assert runtime._events[other]["managed_memory"]["scopes"]["self"]


@pytest.mark.asyncio
async def test_group_memory_never_imports_private_or_other_group(runtime):
    private = private_event()
    register(runtime, private)
    await runtime.memory.attach(private, runtime.test_request)
    private_key = runtime._events[private]["managed_memory"]["scopes"]["self"]
    original = runtime.memory.store.read(private_key)
    runtime.memory.store.write(private_key, "私聊秘密", original["revision"])
    group = event("owner", "group-message")
    group._private_companion_emotion_persona_id = "小七"
    register(runtime, group)
    await runtime.memory.attach(group, runtime.test_request)
    assert "私聊秘密" not in "".join(
        part.text for part in runtime.test_request.extra_user_content_parts
    )
    tool = runtime.test_request.func_tool.get_tool("pc_manage_memory")
    shared = json.loads(await tool.call(None, action="read", target="group"))
    assert json.loads(
        await tool.call(
            None,
            action="write",
            target="group",
            text="群里周五联机",
            expected_revision=shared["note"]["revision"],
        )
    )["ok"]
    group.message_obj.group_id = "other-group"
    group.session_id = "other-group"
    register(runtime, group)
    await runtime.memory.attach(group, runtime.test_request)
    assert "群里周五联机" not in "".join(
        part.text for part in runtime.test_request.extra_user_content_parts
    )


@pytest.mark.asyncio
async def test_migration_uses_only_current_persona_and_matching_private_origin(runtime):
    e = private_event()
    runtime.plugin._active_persona_scope = lambda: "小七"
    runtime.plugin._private_user_id_for_event = lambda _: "owner"
    old = {
        "umo": e.unified_msg_origin,
        "companion_memory": {
            "profile": {"strong_memories": ["叫阿明"], "interests": ["茶"]},
            "items": [{"text": "以前喜欢咖啡", "created_at": "2026-10-01"}],
        },
    }
    runtime.plugin.data = {"users": {"owner": old}}
    retained = copy.deepcopy(old)
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    tool = runtime.test_request.func_tool.get_tool("pc_manage_memory")
    first = json.loads(await tool.call(None))
    assert "阿明" in first["note"]["text"]
    assert "咖啡" in json.loads(await tool.call(None, action="legacy"))["text"]
    assert old == retained
    assert json.loads(
        await tool.call(None, action="write", text="", expected_revision=1)
    )["ok"]
    await runtime.memory.attach(e, runtime.test_request)
    assert json.loads(await tool.call(None))["note"]["text"] == ""
    other = private_event("other", "other-person")
    runtime.plugin._private_user_id_for_event = lambda _: "other"
    runtime.plugin.data["users"]["other"] = retained
    register(runtime, other)
    await runtime.memory.attach(other, runtime.test_request)
    assert "阿明" not in "".join(
        part.text for part in runtime.test_request.extra_user_content_parts
    )


@pytest.mark.asyncio
async def test_notification_reads_latest_memory_instead_of_dispatch_snapshot(runtime):
    e = private_event()
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    metadata = copy.deepcopy(runtime._events[e])
    job = runtime.service.submit(
        runtime.scope(e), "m", "memory", "做报告", runtime.workspace, metadata=metadata
    )
    memory_key = metadata["managed_memory"]["scopes"]["self"]
    runtime.memory.store.write(memory_key, "现在称呼阿明，报告已经不需要了。", 1)
    provider = SimpleNamespace(
        text_chat=AsyncMock(
            return_value=LLMResponse(
                role="assistant", completion_text="阿明，后台刚连上。"
            )
        )
    )
    runtime.plugin.context.get_provider_by_id = lambda _: provider
    await runtime.render_notification(
        SimpleNamespace(kind="model_retry", payload={}), job
    )
    kwargs = provider.text_chat.await_args.kwargs
    combined = kwargs["system_prompt"] + "".join(
        part.text for part in kwargs["extra_user_content_parts"]
    )
    assert "现在称呼阿明" in combined
    assert "报告已经不需要了" in combined


@pytest.mark.asyncio
async def test_old_automatic_memory_does_not_write_or_request_models_in_new_mode():
    plugin = SimpleNamespace(
        _gemini_memory_enabled=True,
        enable_companion_memory=True,
        enable_dialogue_episode_memory=True,
        enable_group_episode_memory=True,
        _llm_call=AsyncMock(),
    )
    user = {"companion_memory": {"items": []}}
    UserMemoryMixin._update_companion_memory_from_message(plugin, user, "以后叫我阿明")
    await UserMemoryMixin._maybe_refresh_companion_memory(plugin, "u", user)
    await UserMemoryMixin._maybe_refresh_dialogue_episode(plugin, "u", user)
    await GroupObservationMixin._maybe_refresh_group_episode(plugin, "g", {})
    assert UserMemoryMixin._format_private_chat_context_injection(plugin, user) == ""
    assert user["companion_memory"]["items"] == []
    plugin._llm_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [None, [], "旧格式"])
@pytest.mark.parametrize("group", [False, True])
async def test_malformed_legacy_container_does_not_break_chat(runtime, broken, group):
    e = event() if group else private_event()
    e._private_companion_emotion_persona_id = "小七"
    runtime.plugin._active_persona_scope = lambda: "小七"
    runtime.plugin._private_user_id_for_event = lambda _: "owner"
    runtime.plugin.data = {"groups" if group else "users": broken}
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    tool = runtime.test_request.func_tool.get_tool("pc_manage_memory")
    assert json.loads(await tool.call(None))["note"]["text"] == ""


@pytest.mark.asyncio
async def test_actual_chat_hook_adds_memory_after_chat_tool_filter(runtime):
    from astrbot.core.provider import Provider
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

    runtime.plugin.enabled = True
    runtime.plugin.enable_conversation_model_routing = True
    runtime.plugin._persistent_task_runtime = runtime
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"enable": True, "model": "gemini"}
    provider.get_model.return_value = "gemini"
    runtime.plugin.context.get_provider_by_id = lambda _: provider
    e = private_event()
    e._private_companion_capability_decision = SimpleNamespace(
        route="chat", reply=True, actions=(), reason="test"
    )
    req = ProviderRequest(prompt="以后叫我阿明", system_prompt="原人格说明")
    await PrivateCompanionPlugin.route_conversation_models.__wrapped__(
        runtime.plugin, e, req
    )
    assert req.func_tool.get_tool("pc_manage_memory") is not None
    assert "原人格说明" in req.system_prompt
    assert any("Gemini自主短记忆" in part.text for part in req.extra_user_content_parts)
    assert not hasattr(e, "_private_companion_task_id")


@pytest.mark.asyncio
async def test_runtime_attachment_isolated_by_persona_and_bot(runtime):
    e = private_event()
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    key = runtime._events[e]["managed_memory"]["scopes"]["self"]
    runtime.memory.store.write(key, "仅小七当前机器人的记忆", 1)
    for persona, bot in (("晚柠", "bot"), ("小七", "another-bot")):
        other = private_event(mid=persona + bot)
        other._private_companion_emotion_persona_id = persona
        other.platform_meta.id = bot
        register(runtime, other)
        await runtime.memory.attach(other, runtime.test_request)
        assert "仅小七当前机器人的记忆" not in "".join(
            part.text for part in runtime.test_request.extra_user_content_parts
        )


@pytest.mark.asyncio
async def test_notification_rejects_foreign_memory_keys(runtime):
    e = private_event()
    register(runtime, e)
    await runtime.memory.attach(e, runtime.test_request)
    key = runtime._events[e]["managed_memory"]["scopes"]["self"]
    runtime.memory.store.write(key, "不属于其他用户", 1)
    other = private_event("other")
    register(runtime, other)
    assert (
        await runtime.memory.context_for_metadata(
            runtime._events[e], runtime.scope(other)
        )
        == ""
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["seed", "read"])
async def test_temporarily_unavailable_memory_does_not_interrupt_chat(
    runtime, monkeypatch, operation
):
    e = private_event()
    register(runtime, e)

    def unavailable(*args):
        raise sqlite3.OperationalError("locked")

    monkeypatch.setattr(runtime.memory.store, operation, unavailable)
    await runtime.memory.attach(e, runtime.test_request)
    assert runtime.test_request.func_tool.get_tool("pc_manage_memory") is not None
    assert any(
        "暂不可用" in part.text
        for part in runtime.test_request.extra_user_content_parts
    )


@pytest.mark.asyncio
async def test_dynamic_memory_is_sent_but_not_persisted_as_user_history(runtime):
    from astrbot.core.agent.message import Message, dump_messages_with_checkpoints
    from astrbot.core.provider.entities import ProviderRequest

    e = private_event()
    register(runtime, e)
    req = ProviderRequest(prompt="本轮用户原文")
    await runtime.memory.attach(e, req)
    assembled = await req.assemble_context()
    assert "Gemini自主短记忆" in json.dumps(assembled, ensure_ascii=False)
    saved = json.dumps(
        dump_messages_with_checkpoints([Message.model_validate(assembled)]),
        ensure_ascii=False,
    )
    assert "本轮用户原文" in saved
    assert "Gemini自主短记忆" not in saved
