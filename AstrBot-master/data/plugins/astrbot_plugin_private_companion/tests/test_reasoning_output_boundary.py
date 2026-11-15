from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import Plain
from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_private_companion.conversation_route import _task_chat_text
from astrbot_plugin_private_companion.helpers import (
    _strip_internal_message_blocks,
    _strip_outbound_control_blocks,
)
from astrbot_plugin_private_companion.main import PrivateCompanionPlugin


@pytest.mark.parametrize("tag", ["think", "thinking", "reasoning", "THINKING"])
@pytest.mark.parametrize("closed", [True, False])
@pytest.mark.parametrize(
    "cleaner",
    [_strip_outbound_control_blocks, _strip_internal_message_blocks, _task_chat_text],
)
def test_generated_reasoning_never_becomes_visible(cleaner, tag, closed):
    raw = f"我先核对资料。<{tag}>PRIVATE_REASONING\n```text\nPRIVATE_CODE\n```\n"
    if closed:
        raw += f"</{tag}>资料核对完了。"
    actual = cleaner(raw)
    assert "PRIVATE" not in actual
    assert tag.lower() not in actual.lower()
    assert "我先核对资料。" in actual
    assert ("资料核对完了。" in actual) == closed


@pytest.mark.parametrize("cleaner", [_strip_outbound_control_blocks, _task_chat_text])
def test_literal_code_tags_are_preserved_but_later_reasoning_is_removed(cleaner):
    code = "```xml\n<thinking>演示标签</thinking>\n```"
    raw = f"示例：\n{code}\n`<think>` 是标签。<thinking>PRIVATE_REASONING</thinking>结束。"
    actual = cleaner(raw)
    assert code in actual
    assert "`<think>`" in actual
    assert "PRIVATE_REASONING" not in actual
    assert actual.endswith("结束。")


@pytest.mark.parametrize("cleaner", [_strip_outbound_control_blocks, _task_chat_text])
def test_multiple_reasoning_blocks_do_not_swallow_visible_text(cleaner):
    raw = "<thinking>PRIVATE_A</thinking>收到。<think>PRIVATE_B</think>继续处理。"
    assert cleaner(raw) == "收到。继续处理。"


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [True, False])
async def test_formal_send_boundary_cleans_tool_intermediate_response(closed):
    plugin = object.__new__(PrivateCompanionPlugin)
    plugin.enabled = True
    plugin.enable_tts_enhancement = False
    raw = "<thinking>PRIVATE_REASONING"
    if closed:
        raw += "</thinking>我来核对。"
    result = SimpleNamespace(chain=[Plain(raw)])
    event = SimpleNamespace(
        unified_msg_origin="test:FriendMessage:1", get_result=lambda: result
    )
    await plugin.final_strip_outbound_control_blocks_before_send(event)
    visible = "".join(item.text for item in result.chain)
    assert visible == ("我来核对。" if closed else "")


@pytest.mark.asyncio
async def test_reasoning_is_removed_before_tool_recovery_and_tts_processing():
    recovery = AsyncMock(side_effect=lambda _event, _resp, text: (text, {}))
    plugin = SimpleNamespace(
        enabled=True,
        _recover_plaintext_photo_tool_call=recovery,
        _guard_unread_creative_work_response=lambda _event, text: text,
        _proactive_only_blocks_passive_event=lambda _event, _feature: True,
    )
    response = LLMResponse(
        role="assistant",
        completion_text="<thinking><tts>PRIVATE_REASONING</tts></thinking>我来核对。",
        tools_call_name=["pc_read_background"],
        tools_call_args=["{}"],
    )
    await PrivateCompanionPlugin.normalize_tts_enhancement_response(
        plugin, SimpleNamespace(), response
    )
    assert recovery.await_args.args[2] == "我来核对。"
    assert response.completion_text == "我来核对。"
    assert response.tools_call_name == ["pc_read_background"]
