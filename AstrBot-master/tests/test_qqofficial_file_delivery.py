"""仅合成QQ API回执验证文件发送，不调用外部服务。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File
from astrbot.api.platform import MessageType
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
    QQOfficialPlatformAdapter,
)


@pytest.fixture(autouse=True)
def disable_real_metric_writes(monkeypatch):
    # 合成API验收不能启动统计数据库后台任务。
    monkeypatch.setattr("astrbot.core.platform.platform.Metric.upload", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", [MessageType.FRIEND_MESSAGE, MessageType.GROUP_MESSAGE]
)
async def test_file_delivery_returns_real_message_receipt_or_explicit_failure(kind):
    adapter = QQOfficialPlatformAdapter(
        {
            "id": "bot",
            "appid": "test",
            "secret": "unused",
            "enable_group_c2c": True,
            "enable_guild_direct_message": False,
        },
        {},
        asyncio.Queue(),
    )
    assert hasattr(adapter, "send_file_by_session"), "缺少可靠文件发送回执"
    adapter.client.api = SimpleNamespace(
        post_group_message=AsyncMock(return_value={"id": "sent-file"})
    )
    adapter.remember_session_scene("target", "group")
    adapter.remember_session_message_id("target", "incoming")
    session = MessageSesion(platform_name="bot", message_type=kind, session_id="target")
    chain = MessageChain(chain=[File(name="结果.zip", file="unused")])
    module = "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter.QQOfficialMessageEvent"
    with (
        patch(
            module + "._parse_to_qqofficial",
            AsyncMock(
                return_value=("", None, None, None, None, "file-path", "结果.zip")
            ),
        ),
        patch(
            module + ".upload_group_and_c2c_media",
            AsyncMock(return_value={"file_info": "uploaded"}),
        ) as upload,
        patch(
            module + ".post_c2c_message", AsyncMock(return_value={"id": "sent-file"})
        ),
    ):
        receipt = await adapter.send_file_by_session(session, chain)
        assert receipt == {"status": "sent", "message_id": "sent-file"}
        assert upload.await_args.args[2] == 4
        upload.return_value = None
        with pytest.raises(RuntimeError, match="file_upload"):
            await adapter.send_file_by_session(session, chain)


@pytest.mark.asyncio
async def test_missing_group_session_cannot_claim_file_delivery():
    adapter = QQOfficialPlatformAdapter(
        {
            "id": "bot",
            "appid": "test",
            "secret": "unused",
            "enable_group_c2c": True,
            "enable_guild_direct_message": False,
        },
        {},
        asyncio.Queue(),
    )
    assert hasattr(adapter, "send_file_by_session"), "缺少可靠文件发送回执"
    session = MessageSesion(
        platform_name="bot",
        message_type=MessageType.GROUP_MESSAGE,
        session_id="missing",
    )
    chain = MessageChain(chain=[File(name="结果.zip", file="unused")])
    module = "astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter.QQOfficialMessageEvent"
    with patch(
        module + "._parse_to_qqofficial",
        AsyncMock(return_value=("", None, None, None, None, "file-path", "结果.zip")),
    ):
        with pytest.raises(RuntimeError, match="file_session"):
            await adapter.send_file_by_session(session, chain)
