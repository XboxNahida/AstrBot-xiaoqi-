"""中枢合并已替代旧事件时，新消息必须保留自己的回复机会。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrbot.core.agent.runners.tool_loop_agent_runner import FollowUpTicket
from astrbot.core.pipeline.process_stage import follow_up


@pytest.mark.asyncio
@pytest.mark.parametrize("merged_target", ["old-run", "other-run", None])
async def test_hub_merged_replacement_is_not_captured_by_superseded_runner(
    monkeypatch, merged_target,
):
    original = SimpleNamespace(
        message_obj=SimpleNamespace(message_id="old-run"),
        get_sender_id=lambda: "user-1", get_extra=lambda *_: False,
    )
    ticket = FollowUpTicket(seq=0, text="确认任务开始了吗")
    runner = SimpleNamespace(
        run_context=SimpleNamespace(context=SimpleNamespace(event=original)),
        follow_up=Mock(return_value=ticket),
    )
    event = SimpleNamespace(
        unified_msg_origin="bot:private:user-1", get_sender_id=lambda: "user-1",
        get_message_str=lambda: "确认任务开始了吗",
    )
    if merged_target is not None:
        event._private_companion_hub_batch_messages = [
            {"event_id": merged_target, "text": "旧消息"},
            {"event_id": "new-run", "text": "确认任务开始了吗"},
        ]
    monkeypatch.setattr(follow_up, "_ACTIVE_AGENT_RUNNERS", {event.unified_msg_origin: runner})
    monkeypatch.setattr(follow_up, "_FOLLOW_UP_ORDER_STATE", {})

    capture = follow_up.try_capture_follow_up(event)
    if merged_target == "old-run":
        assert capture is None
        runner.follow_up.assert_not_called()
    else:
        assert capture is not None
        assert capture.target_run_id == "old-run"
        runner.follow_up.assert_called_once()
        ticket.consumed = True
        ticket.resolved.set()
        await asyncio.wait_for(capture.monitor_task, timeout=1)
