"""发送事实的顺序、持久原件和供应商协议隔离回归。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot.api.message_components import File, Image, Plain
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    bind_checkpoint_messages,
    dump_messages_with_checkpoints,
)


def test_archive_metadata_survives_reload_but_never_enters_provider_payload():
    message = AssistantMessageSegment(content="图片在这里")
    message._history_metadata = {"sent_sequence": [{"type": "image", "source": "private-path"}]}
    archive = dump_messages_with_checkpoints([message])
    assert archive[0]["history_metadata"] == message._history_metadata
    restored = bind_checkpoint_messages(archive)[0]
    assert restored._history_metadata == message._history_metadata
    assert "private-path" not in json.dumps(restored.model_dump())


def test_direct_provider_dict_history_also_excludes_internal_attachment_sources():
    from astrbot.core.provider.provider import Provider
    archive = [{"role": "assistant", "content": "图片", "history_metadata": {
        "sent_sequence": [{"type": "image", "source": "private-path"}]}}]
    payload = Provider._ensure_message_to_dicts(None, archive)
    assert payload == [{"role": "assistant", "content": "图片"}]
    assert archive[0]["history_metadata"]["sent_sequence"][0]["source"] == "private-path"


@pytest.mark.asyncio
async def test_sent_sequence_keeps_interleaving_duplicate_names_and_durable_bytes(tmp_path):
    from astrbot_plugin_private_companion.delivery_media_history import capture_sequence

    image = tmp_path / "emoji.png"
    image.write_bytes(b"original-image")
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    (one / "result.txt").write_bytes(b"one")
    (two / "result.txt").write_bytes(b"two")
    chain = [Plain("先看表情"), Image.fromFileSystem(str(image)), Plain("再看文件"),
             File(name="result.txt", file=str(one / "result.txt")),
             File(name="result.txt", file=str(two / "result.txt"))]
    sequence = await capture_sequence(chain, tmp_path / "history", send_index=2, timestamp=123)
    assert [row["type"] for row in sequence] == ["text", "image", "text", "file", "file"]
    assert [row["position"] for row in sequence] == list(range(5))
    assert all(row["send_index"] == 2 and row["timestamp"] == 123 for row in sequence)
    image.unlink()
    (one / "result.txt").unlink()
    (two / "result.txt").unlink()
    assert Path(sequence[1]["source"]).read_bytes() == b"original-image"
    assert Path(sequence[3]["source"]).read_bytes() == b"one"
    assert Path(sequence[4]["source"]).read_bytes() == b"two"


@pytest.mark.asyncio
async def test_unavailable_media_keeps_position_and_does_not_claim_original_exists(tmp_path):
    from astrbot_plugin_private_companion.delivery_media_history import capture_sequence

    sequence = await capture_sequence(
        [Plain("文件"), File(name="gone.txt", file=str(tmp_path / "gone.txt")), Plain("结束")],
        tmp_path / "history", send_index=0, timestamp=1,
    )
    assert sequence[1]["availability"] == "unavailable"
    assert "source" not in sequence[1]
    assert sequence[2]["text"] == "结束"


@pytest.mark.asyncio
async def test_media_resolver_failure_does_not_erase_the_confirmed_send(tmp_path, monkeypatch):
    from astrbot_plugin_private_companion.delivery_media_history import capture_sequence
    monkeypatch.setattr(Image, "convert_to_file_path", AsyncMock(side_effect=Exception("download failed")))
    rows = await capture_sequence([Image(file="https://example.test/x.png"), Plain("后面还有文字")],
        tmp_path / "history", send_index=0, timestamp=1)
    assert rows[0]["availability"] == "unavailable" and "source" not in rows[0]
    assert rows[1]["text"] == "后面还有文字"
