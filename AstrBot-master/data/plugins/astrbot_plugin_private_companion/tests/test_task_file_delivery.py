"""文件产物交付：使用临时文件及合成发送器，不发真实平台消息。"""

import asyncio
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def delivery(tmp_path):
    from astrbot_plugin_private_companion.task_file_delivery import (
        TaskArtifactReader,
        TaskFileDelivery,
    )

    root = tmp_path / "工作区"
    root.mkdir()
    reader = TaskArtifactReader(
        current_workspace=lambda _: root,
        authorize=lambda _: True,
        sensitive_roots=[],
        redact=lambda text, limit: text.replace("real-host-credential", "[REDACTED]")[
            :limit
        ],
    )
    sender = AsyncMock(return_value={"status": "sent", "message_id": "message-1"})
    return SimpleNamespace(
        root=root,
        reader=reader,
        sender=sender,
        service=TaskFileDelivery(reader, sender),
        actor=object(),
    )


async def capture_send(delivery, paths, **kwargs):
    captures = []

    async def capture(session, chain):
        file = chain.chain[0]
        captures.append((session, file.name, Path(file.file).read_bytes(), file.file))
        return {"status": "sent", "message_id": "real-id"}

    delivery.sender.side_effect = capture
    result = await delivery.service.send(
        delivery.actor, "bot:FriendMessage:user", paths=paths, **kwargs
    )
    return result, captures


@pytest.mark.asyncio
async def test_single_file_sends_snapshot_without_pasting_or_modifying(delivery):
    original = delivery.root / "公式渲染器.html"
    original.write_text("<p>公式</p>", encoding="utf-8")
    result, captured = await capture_send(delivery, [str(original)])
    assert result["success"] and result["items"][0]["message_id"] == "real-id"
    assert captured[0][:3] == (
        "bot:FriendMessage:user",
        original.name,
        original.read_bytes(),
    )
    assert not Path(captured[0][3]).exists()
    assert original.read_text(encoding="utf-8") == "<p>公式</p>"


@pytest.mark.asyncio
async def test_batch_and_directory_auto_zip_preserve_chinese_structure(delivery):
    for name in ("甲/结果.txt", "乙/结果.txt"):
        file = delivery.root / name
        file.parent.mkdir()
        file.write_text(name, encoding="utf-8")
    result, captured = await capture_send(delivery, ["甲", "乙/结果.txt"])
    assert result["success"] and len(captured) == 1 and captured[0][1].endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(captured[0][2])) as archive:
        assert set(archive.namelist()) == {"甲/结果.txt", "乙/结果.txt"}
        assert archive.read("甲/结果.txt").decode() == "甲/结果.txt"


@pytest.mark.asyncio
async def test_individual_reports_partial_delivery_without_automatic_retry(delivery):
    for name in ("一.txt", "二.txt"):
        (delivery.root / name).write_text(name, encoding="utf-8")
    delivery.sender.side_effect = [
        {"status": "sent", "message_id": "one"},
        TimeoutError(),
    ]
    result = await delivery.service.send(
        delivery.actor,
        "bot:FriendMessage:user",
        paths=["一.txt", "二.txt"],
        package="individual",
    )
    assert not result["success"] and result["items"][0]["status"] == "sent"
    assert result["items"][1]["status"] == "unknown"
    assert result["items"][1]["may_have_sent"] is True
    assert delivery.sender.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["../其他群友.txt", "credentials.json", ".env", "secret.txt", "嵌套.zip"]
)
async def test_entire_batch_preflight_blocks_escapes_and_sensitive_content(
    delivery, bad
):
    (delivery.root / "正常.txt").write_text("normal", encoding="utf-8")
    if bad == "嵌套.zip":
        with zipfile.ZipFile(delivery.root / bad, "w") as archive:
            archive.writestr(".env", "real-host-credential")
    else:
        (delivery.root / bad).write_text("real-host-credential", encoding="utf-8")
    result = await delivery.service.send(
        delivery.actor, "bot:FriendMessage:user", paths=["正常.txt", bad]
    )
    assert not result["success"] and result["code"] == "file_access_denied"
    delivery.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_useful_config_source_and_non_robot_credentials_are_unchanged(delivery):
    expected = {
        ".env": b"TOKEN=business-token-123",
        "credentials.json": b'{"password":"example123","token":"business-token"}',
        "说明.txt": "密钥是接口参数，不是程序配置值".encode(),
        "数据库.db": b"ordinary-export",
    }
    for name, content in expected.items():
        (delivery.root / name).write_bytes(content)
    result, captured = await capture_send(delivery, list(expected))
    assert result["success"]
    with zipfile.ZipFile(io.BytesIO(captured[0][2])) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == expected


@pytest.mark.asyncio
async def test_hardlink_is_rejected(delivery, tmp_path):
    original = tmp_path / "original.txt"
    original.write_text("private", encoding="utf-8")
    os.link(original, delivery.root / "link.txt")
    result = await delivery.service.send(
        delivery.actor, "bot:FriendMessage:user", paths=["link.txt"]
    )
    assert not result["success"] and result["code"] == "file_access_denied"
    delivery.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_access_and_invalid_parameters_never_send(delivery):
    (delivery.root / "a.txt").write_text("a", encoding="utf-8")
    delivery.reader.authorize = lambda _: False
    result = await delivery.service.send(
        delivery.actor, "bot:FriendMessage:user", paths=["a.txt"]
    )
    assert not result["success"]
    delivery.sender.assert_not_awaited()
    delivery.reader.authorize = lambda _: True
    result = await delivery.service.send(
        delivery.actor,
        "bot:FriendMessage:user",
        paths=["a.txt"],
        package="zip",
        archive_name="../bad.zip",
    )
    assert not result["success"]
    delivery.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_tool_has_bound_destination_and_returns_validated_arguments(delivery):
    from astrbot_plugin_private_companion.task_file_delivery import FileDeliveryTool

    tool = FileDeliveryTool(delivery.service, delivery.actor, "bot:FriendMessage:user")
    properties = tool.parameters["properties"]
    assert "session" not in properties and "actor" not in properties
    result = json.loads(await tool.call(None, paths=["a.txt"], session="another"))
    assert not result["success"] and result["code"] == "invalid_arguments"
    delivery.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_sent_receipt_is_not_relabelled_as_possible_delivery(delivery):
    (delivery.root / "a.txt").write_text("a", encoding="utf-8")
    delivery.sender.return_value = {"status": "not_sent", "not_executed": True, "code": "platform_not_found"}
    result = await delivery.service.send(delivery.actor, "bot:FriendMessage:user", paths=["a.txt"])
    assert not result["success"]
    assert result["items"][0]["may_have_sent"] is False
    assert result["items"][0]["code"] == "platform_not_found"


@pytest.mark.asyncio
async def test_existing_zip_with_business_credentials_preserves_original_bytes(delivery):
    path = delivery.root / "业务资料.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(".env", "password=business-value")
        archive.writestr("说明.json", '{"token":"example-value"}')
    expected = path.read_bytes()
    result, captures = await capture_send(delivery, [path.name])
    assert result["success"] and captures[0][2] == expected


@pytest.mark.asyncio
async def test_preflight_resource_limit_never_sends_partial_batch(delivery):
    (delivery.root / "a.txt").write_bytes(b"abcd")
    delivery.service.max_bytes = 3
    result = await delivery.service.send(delivery.actor, "bot:FriendMessage:user", paths=["a.txt"])
    assert result["code"] == "file_preparation_failed" and result["not_executed"]
    delivery.sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_redirected_workspace_root_cannot_be_resolved_away(delivery, tmp_path):
    target = tmp_path / "另一工作区"
    target.mkdir()
    (target / "a.txt").write_text("peer-data", encoding="utf-8")
    redirected = tmp_path / "根目录链接"
    if os.name == "nt":
        result = await asyncio.to_thread(subprocess.run, ["cmd", "/c", "mklink", "/J", str(redirected), str(target)], capture_output=True)
        assert result.returncode == 0
    else:
        redirected.symlink_to(target, target_is_directory=True)
    delivery.reader.current_workspace = lambda _: redirected
    result = await delivery.service.send(delivery.actor, "bot:FriendMessage:user", paths=["a.txt"])
    assert result["code"] == "file_access_denied"
    delivery.sender.assert_not_awaited()
