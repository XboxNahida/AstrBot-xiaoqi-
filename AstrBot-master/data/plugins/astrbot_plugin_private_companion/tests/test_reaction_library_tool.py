import json
from pathlib import Path

import pytest
from PIL import Image

from astrbot_plugin_private_companion.tests.test_task_runtime import event, register
from astrbot_plugin_private_companion.tests.test_task_runtime import runtime as runtime


@pytest.fixture
def pack(runtime, tmp_path, monkeypatch):
    from meme_manager.backend import pack_resolver

    root = tmp_path / "selected-pack"
    (root / "happy").mkdir(parents=True)
    for name, color in (("one", "red"), ("two", "blue")):
        Image.new("RGB", (20, 20), color).save(root / "happy" / (name + ".png"))
    other = tmp_path / "other-bot-pack"
    other.mkdir()
    Image.new("RGB", (20, 20)).save(other / "foreign.png")
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs)
        return {"pack_id": "selected", "memes_dir": root, "category_mapping": {"happy": "开心"}}

    monkeypatch.setattr(pack_resolver, "resolve_pack_context", resolve)
    return root, other, calls


@pytest.mark.asyncio
async def test_library_is_bound_to_current_bot_and_supports_search_page_and_original(runtime, pack):
    root, other, calls = pack
    e = event(mid="library")
    register(runtime, e)
    tool = runtime.make_reaction_library_tool(e)
    groups = json.loads(await tool.call(None, action="groups"))
    assert groups["groups"][0]["name"] == "happy"
    assert groups["groups"][0]["count"] == 2
    assert calls[-1]["bot_id"] == "bot"
    result = json.loads(await tool.call(None, action="list", query="one", page_size=1))
    assert result["total"] == 1
    assert "foreign" not in str(result)
    item = result["items"][0]
    preview = await tool.call(None, action="get", asset_ids=[item["id"]])
    assert preview.content[1].type == "image"
    detail = json.loads(preview.content[0].text)["items"][0]
    assert Path(detail["path"]).read_bytes() == (root / "happy/one.png").read_bytes()
    assert not Path(detail["path"]).is_relative_to(other)
    assert Path(detail["path"]).is_relative_to(runtime.workspace)
    forged = json.loads(await tool.call(None, action="list", bot_id="other"))
    assert not forged["ok"]


@pytest.mark.asyncio
async def test_library_sends_multiple_images_and_reports_partial_failure(runtime, pack):
    e = event(mid="multi-reactions")
    register(runtime, e)
    tool = runtime.make_reaction_library_tool(e)
    listing = json.loads(await tool.call(None, action="list"))
    runtime.plugin.context.send_message.side_effect = [True, False]
    receipt = json.loads(await tool.call(None, action="send", asset_ids=[i["id"] for i in listing["items"]]))
    assert not receipt["ok"]
    assert [i["sent"] for i in receipt["items"]] == [True, False]
    assert runtime.plugin.context.send_message.await_count == 2
    assert {call.args[0] for call in runtime.plugin.context.send_message.await_args_list} == {e.unified_msg_origin}


@pytest.mark.asyncio
async def test_library_rejects_paths_links_and_stale_ids(runtime, pack):
    root, other, _ = pack
    e = event(mid="boundary")
    register(runtime, e)
    tool = runtime.make_reaction_library_tool(e)
    listing = json.loads(await tool.call(None, action="list"))
    selected = listing["items"][0]
    (root / "happy" / selected["name"]).unlink()
    result = json.loads(await tool.call(None, action="get", asset_ids=[selected["id"], "../../foreign.png"]))
    assert not result["ok"] and all(not item["ok"] and "path" not in item for item in result["items"])
    try:
        (root / "linked").symlink_to(other, target_is_directory=True)
    except OSError:
        pass
    result = json.loads(await tool.call(None, action="list"))
    assert "foreign" not in str(result)
    runtime.plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_background_can_get_library_image_then_use_it_for_edit(runtime, pack, monkeypatch):
    from types import SimpleNamespace
    import astrbot_plugin_private_companion.task_runtime as module

    monkeypatch.setattr(module, "AstrAgentContext", lambda **kw: SimpleNamespace(**kw))
    def test_broker(actor):
        return module.TaskExecutionBroker(runtime.permissions, task_root=runtime.workspace,
                                          protected_roots=[Path(module.__file__).parent])

    runtime.get_execution_broker = test_broker
    e = event(mid="background-reaction")
    register(runtime, e)
    await runtime.submit(e, {"goal": "找表情包改图"})
    job = runtime.service.claim("test")
    tool = runtime.resolve_tools(job, None)["pc_reaction_library"]
    listing = await runtime.execute_tool(job, tool, {"action": "list"})
    asset_id = json.loads(listing["content"])["items"][0]["id"]
    result = await runtime.execute_tool(job, tool, {"action": "get", "asset_ids": [asset_id]})
    path = json.loads(result["content"])["items"][0]["path"]
    assert runtime.authorize_tool(job, runtime.test_tools[2], {"kind": "edit", "reference_image_path": path})
