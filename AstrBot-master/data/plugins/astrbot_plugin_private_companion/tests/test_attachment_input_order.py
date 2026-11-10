from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.message.components import File, Image, Plain, Reply
from astrbot_plugin_private_companion import task_attachments as attachments


def event_with(*parts):
    return SimpleNamespace(
        message_obj=SimpleNamespace(message_id="m-2", message=list(parts)),
        get_sender_id=lambda: "user-1",
    )


def test_index_keeps_interleaved_text_images_and_duplicate_filenames():
    event = event_with(
        Plain("先看这个"), File(name="资料.zip", file="a.zip"),
        Image(file="photo.png"), Plain("然后对照第二份"),
        File(name="资料.zip", file="b.zip"),
    )
    index = attachments.describe_event_input(event)
    assert index["message_id"] == "m-2"
    assert index["sender_id"] == "user-1"
    assert [x["type"] for x in index["items"]] == ["text", "file", "image", "text", "file"]
    assert [x["position"] for x in index["items"]] == ["1", "2", "3", "4", "5"]
    assert [x["attachment_index"] for x in index["items"] if x["type"] != "text"] == [1, 2, 3]
    assert index["items"][3]["text"] == "然后对照第二份"
    assert all(x["source"] == "current" for x in index["items"])


def test_quoted_file_is_distinct_from_current_upload():
    event = event_with(
        Reply(id="old-1", chain=[File(name="旧文件.zip", file="old.zip")]),
        File(name="新文件.zip", file="new.zip"),
    )
    rows = attachments.describe_event_input(event)["items"]
    assert [(x["source"], x["message_id"], x["position"]) for x in rows] == [
        ("quoted", "old-1", "1.1"), ("current", "m-2", "2"),
    ]


@pytest.mark.asyncio
async def test_staging_preserves_duplicate_occurrences_and_original_positions(tmp_path):
    source = tmp_path / "材料.zip"
    source.write_bytes(b"content")
    event = event_with(
        Plain("第一份"), File(name="一.zip", file=str(source)), Image(file="p.png"),
        File(name="二.zip", file=str(source)),
    )
    rows = await attachments.stage_event_files(event, tmp_path / "tasks", (tmp_path,), lambda: True)
    assert len(rows) == 2
    assert [x["position"] for x in rows] == ["2", "4"]
    assert [x["attachment_index"] for x in rows] == [1, 3]
    assert [x["name"] for x in rows] == ["一.zip", "二.zip"]
    assert rows[0]["path"] == rows[1]["path"]
    assert len(list((tmp_path / "tasks").iterdir())) == 1


def test_file_only_message_is_not_empty_input():
    from astrbot_plugin_private_companion.capability_hub import (
        build_capability_context,
        decide_capabilities,
    )

    context = build_capability_context(scope="private:u", current_text="", sender_id="u", sender_name="用户", now=None, has_current_file=True)
    decision = decide_capabilities(context)
    assert decision.reply
    assert decision.reason != "empty_message"


@pytest.mark.asyncio
async def test_frontend_and_hub_receive_ordered_index_without_implicit_summary():
    from astrbot_plugin_private_companion.passive_state_pipeline import (
        append_capability_guidance,
    )

    plugin = SimpleNamespace(
        _event_scope_key=lambda _: "private:user-1", _recall_message_cache={},
        _task_provider=lambda *args: "reply-primary",
        _llm_call=AsyncMock(return_value='{"reply":true,"actions":[]}'),
    )
    event = event_with(File(name="第一份.zip", file="a.zip"), Image(file="pic.png"), File(name="第二份.zip", file="b.zip"))
    event.is_private_chat = lambda: True
    event.get_sender_name = lambda: "用户"
    event.message_str = ""
    req = SimpleNamespace(system_prompt="原人格", prompt="总结一下文件里面讲了什么？")
    await append_capability_guidance(plugin, event, req)
    assert "第一份.zip" in req.prompt and "第二份.zip" in req.prompt
    assert req.prompt.index("第一份.zip") < req.prompt.index('"type": "image"') < req.prompt.index("第二份.zip")
    assert "总结一下文件里面讲了什么？" not in req.prompt
    assert "确认收到" in req.system_prompt
    hub_prompt = str(plugin._llm_call.call_args)
    assert "has_file" in hub_prompt and "第一份.zip" in hub_prompt
    original = req.prompt
    await append_capability_guidance(plugin, event, req)
    assert req.prompt == original
