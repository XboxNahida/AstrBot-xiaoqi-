from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider import Provider
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_private_companion.capability_hub import (
    CapabilityDecision,
    decision_action_allowed,
)
from astrbot_plugin_private_companion.conversation_route import (
    configure_conversation_request,
)
from astrbot_plugin_private_companion.tts_enhancement import TtsEnhancementMixin


def test_gemini_receives_media_tools_without_hub_action_and_not_shell():
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"model": "gemini"}
    provider.get_model.return_value = "gemini"
    plugin = SimpleNamespace(
        enable_conversation_model_routing=True,
        conversation_chat_provider_id="gemini",
        conversation_chat_fallback_provider_id="",
        context=SimpleNamespace(get_provider_by_id=lambda _: provider),
    )
    event = SimpleNamespace(_private_companion_capability_decision=CapabilityDecision(
        True, "text", "neutral", (), False, "test"
    ))
    names = ["pc_generate_photo", "pc_image_history", "pc_send_current_media", "pc_find_reaction_image", "localShell"]
    req = ProviderRequest(prompt="把刚才的图做成竖版", func_tool=ToolSet([
        FunctionTool(name=name, description=name, parameters={"type": "object"}) for name in names
    ]))
    configure_conversation_request(plugin, event, req)
    assert {tool.name for tool in req.func_tool.tools} == set(names[:-1])
    assert decision_action_allowed(event._private_companion_capability_decision, "image_edit")
    assert not decision_action_allowed(event._private_companion_capability_decision, "workspace")


def test_gemini_voice_permission_is_not_automatic_voice_selection():
    harness = TtsEnhancementMixin()
    harness.enable_conversation_model_routing = True
    decision = CapabilityDecision(True, "text", "neutral", ("voice",), False, "test")
    event = SimpleNamespace(_private_companion_capability_decision=decision)
    assert harness._auto_voice_trigger_reason("文字回复应保持文字", event) == (False, "")
    assert harness._tts_trigger_probability_allows(event, reason="tag")


def test_explicit_image_size_overrides_web_default_ratio():
    from data.plugins.astrbot_plugin_image_companion.image_runtime import (
        ProactiveMessageMixin,
    )
    harness = SimpleNamespace(external_image_api_size="2K", external_image_api_ratio="1:1")
    harness._normalize_agnes_image_ratio = ProactiveMessageMixin._normalize_agnes_image_ratio
    assert ProactiveMessageMixin._agnes_image_size_and_ratio(harness, "2K@9:16") == ("2K", "9:16")
    assert ProactiveMessageMixin._agnes_image_size_and_ratio(harness, "") == ("2K", "1:1")


@pytest.mark.asyncio
async def test_model_selected_voice_tags_survive_legacy_postprocess_setting():
    from astrbot_plugin_private_companion.tests.test_tts_postprocess_tag_guard import (
        _TtsHarness,
    )
    harness = _TtsHarness()
    harness.enable_conversation_model_routing = True
    event = SimpleNamespace(
        message_str="继续上一首", unified_msg_origin="bot:FriendMessage:u",
        _private_companion_tts_request_applied=True,
    )
    response = SimpleNamespace(completion_text="<pc_tts>(唱歌)啦啦啦</pc_tts>")
    await harness.protect_tts_enhancement_response_blocks(event, response)
    restored = harness._restore_protected_tts_blocks(response.completion_text, event)
    assert "(唱歌)" in restored
    assert "<tts>" in restored
    harness._convert_text_to_tts_markup = AsyncMock()
    assert await harness._maybe_convert_plain_reply_to_tts("这次先用文字说明", event) == []
    harness._convert_text_to_tts_markup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,ratio,resolution,expected", [
    ("agnes", "9:16", "2K", ("2K", "9:16")),
    ("agnes", "", "1024x768", ("1024x768", "")),
    ("openai", "9:16", "2K", ("1152x2048", "1:1")),
    ("openai", "", "", ("", "1:1")),
])
async def test_image_parameters_reach_endpoint_without_changing_web_defaults(platform, ratio, resolution, expected):
    from data.plugins.astrbot_plugin_image_companion.image_runtime import (
        ProactiveMessageMixin,
    )

    class Harness(ProactiveMessageMixin):
        external_image_api_ratio = "4:3"
        external_image_api_size = "1K"

        def _apply_external_image_api_endpoint_runtime(self, endpoint):
            self.external_image_api_ratio = "1:1"
            self.external_image_api_size = "2K"

    harness = Harness()
    harness._request_aspect_ratio, harness._request_resolution = ratio, resolution
    harness._resolved_external_image_api_platform = lambda: platform
    harness._external_image_endpoint_multi_reference_capacity = lambda *args: 1
    harness._photo_reference_prompt_for_backend_capacity = lambda prompt, **kwargs: prompt
    seen = []

    async def generate(prompt, **kwargs):
        seen.append((kwargs["image_size"], harness.external_image_api_ratio))
        return "result.png", "ok"

    harness._run_external_photo_generation_once = generate
    outcome = await harness._run_external_photo_generation_with_endpoint({}, "测试", session_key="s")
    assert outcome.image_path == "result.png"
    assert seen == [expected]
    assert harness.external_image_api_ratio == "4:3"
    assert harness.external_image_api_size == "1K"
