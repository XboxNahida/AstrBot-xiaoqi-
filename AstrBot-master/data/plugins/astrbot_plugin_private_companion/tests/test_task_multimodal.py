import copy
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from astrbot.core.agent.message import TextPart


@pytest.mark.asyncio
async def test_current_image_and_text_survive_temp_cleanup_and_keep_old_question(
    tmp_path,
):
    from astrbot_plugin_private_companion.task_multimodal import (
        capture_input,
        freeze_images,
        materialize_images,
    )

    image = tmp_path / "current.png"
    Image.new("RGB", (8, 8), "red").save(image)
    req = SimpleNamespace(
        prompt="这道题怎么做",
        image_urls=[str(image)],
        contexts=[{"role": "user", "content": "上一题：8点FFT"}],
        extra_user_content_parts=[TextPart(text="本轮：求H(z)，系数3/4和1/8")],
    )
    request = capture_input(
        req, "current-message", lambda value, limit: str(value)[:limit]
    )
    await freeze_images(request)
    image.unlink()
    restored = json.loads(json.dumps(request))
    assert restored["current_input"]["message_id"] == "current-message"
    assert restored["contexts"][0]["content"] == "上一题：8点FFT"
    current = restored["current_input"]["content"]
    assert "这道题怎么做" in str(current) and "H(z)" in str(current)
    assert "pc-task-image://" in str(current)
    contexts = [{"role": "user", "content": current}]
    outgoing = materialize_images(contexts, restored)
    assert outgoing[0]["content"][-1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "pc-task-image://" in str(contexts)
    assert len(str(contexts)) < 500


@pytest.mark.asyncio
async def test_native_history_images_are_preserved_and_deduplicated(tmp_path):
    from astrbot_plugin_private_companion.task_multimodal import (
        capture_input,
        freeze_images,
    )

    path = tmp_path / "old.png"
    Image.new("RGB", (4, 4)).save(path)
    parts = [
        {"type": "text", "text": "历史题目"},
        {"type": "image_url", "image_url": {"url": str(path)}},
    ]
    req = SimpleNamespace(
        prompt="再详细些",
        contexts=[{"role": "user", "content": parts}],
        image_urls=[str(path)],
        extra_user_content_parts=[],
    )
    original = copy.deepcopy(parts)
    request = capture_input(req, "followup", lambda value, limit: str(value)[:limit])
    await freeze_images(request)
    assert len(request["media"]) == 1
    assert (
        request["contexts"][0]["content"][1] == request["current_input"]["content"][-1]
    )
    assert parts == original


@pytest.mark.asyncio
async def test_missing_current_image_is_reported_and_never_replaced_with_history(
    tmp_path,
):
    from astrbot_plugin_private_companion.task_multimodal import (
        capture_input,
        freeze_images,
    )

    req = SimpleNamespace(
        prompt="新题",
        image_urls=[str(tmp_path / "missing.png")],
        contexts=[{"role": "user", "content": "上一题FFT"}],
        extra_user_content_parts=[],
    )
    request = capture_input(req, "new", lambda value, limit: str(value)[:limit])
    with pytest.raises(ValueError, match="image_input_unavailable"):
        await freeze_images(request)


def test_unknown_checkpoint_reference_does_not_silently_drop_image():
    from astrbot_plugin_private_companion.task_multimodal import materialize_images

    with pytest.raises(ValueError, match="image_input_unavailable"):
        materialize_images(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "pc-task-image://missing"},
                        }
                    ],
                }
            ],
            {"media": {}},
        )
