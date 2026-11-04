from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astrbot.core.provider import Provider


def test_chat_provider_is_selected_before_framework_image_processing():
    from astrbot_plugin_private_companion.conversation_route import (
        prepare_native_chat_entry,
    )

    provider = MagicMock(spec=Provider)
    provider.provider_config = {"enable": True, "modalities": ["text", "image"]}
    extras = {"selected_provider": "old-model", "selected_model": "old-model"}
    event = SimpleNamespace(set_extra=lambda key, value: extras.update({key: value}))
    plugin = SimpleNamespace(
        enabled=True,
        enable_conversation_model_routing=True,
        conversation_chat_provider_id="gemini",
        context=SimpleNamespace(get_provider_by_id=lambda _: provider),
    )
    prepare_native_chat_entry(plugin, event)
    assert extras["selected_provider"] == "gemini"
    assert extras["selected_model"] is None
    assert event._private_companion_native_multimodal is True


def test_native_image_input_wins_over_optional_caption_provider():
    from astrbot_plugin_private_companion.private_image import PrivateImageMixin

    assert (
        PrivateImageMixin._private_image_delivery_mode(
            has_visual_provider=True,
            main_provider_supports_image=True,
            has_dynamic_gif=False,
        )
        == "direct"
    )


def test_unconfigured_modalities_follow_core_native_image_default():
    from astrbot_plugin_private_companion.conversation_route import (
        prepare_native_chat_entry,
    )

    provider = MagicMock(spec=Provider)
    provider.provider_config = {"enable": True}
    event = SimpleNamespace(set_extra=lambda *_: None)
    plugin = SimpleNamespace(
        enabled=True,
        enable_conversation_model_routing=True,
        conversation_chat_provider_id="gemini",
        context=SimpleNamespace(get_provider_by_id=lambda _: provider),
    )
    prepare_native_chat_entry(plugin, event)
    assert event._private_companion_native_multimodal is True
    from astrbot_plugin_private_companion.private_image import PrivateImageMixin

    assert PrivateImageMixin._provider_supports_image(provider)


@pytest.mark.asyncio
async def test_native_turn_does_not_prefetch_caption_for_keyword_routing():
    from astrbot_plugin_private_companion.private_image import PrivateImageMixin

    plugin = SimpleNamespace(enabled=True)
    event = SimpleNamespace(
        _private_companion_native_multimodal=True,
        private_companion_delayed_image_vision_text="旧缓存不应用于当前原图",
    )
    assert (
        await PrivateImageMixin.prepare_keyword_model_router_image_caption(
            plugin, event
        )
        == ""
    )


@pytest.mark.asyncio
async def test_native_history_is_not_stripped_before_gemini():
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot_plugin_private_companion.main import PrivateCompanionPlugin

    req = ProviderRequest(
        contexts=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YQ=="},
                    }
                ],
            }
        ]
    )
    await PrivateCompanionPlugin.sanitize_historical_image_blocks_before_provider.__wrapped__(
        SimpleNamespace(enabled=True),
        SimpleNamespace(_private_companion_native_multimodal=True),
        req,
    )
    assert req.contexts[0]["content"][0]["type"] == "image_url"
