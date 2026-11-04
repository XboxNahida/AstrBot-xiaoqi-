from dataclasses import replace

import pytest

from astrbot_plugin_private_companion.interaction_channels import InteractionChannels
from astrbot_plugin_private_companion.task_service import TaskScope


@pytest.fixture
def channels(tmp_path):
    return InteractionChannels(tmp_path / "tasks.db", clock=lambda: 1000)


@pytest.fixture
def scope():
    return TaskScope("bot", "bot:GroupMessage:g", "owner")


def opened(channels, scope):
    return channels.open(
        scope,
        "source:main",
        {"chat_context": {"system_prompt": "小七"}},
        ttl_seconds=3600,
    )


def test_channel_reopen_keeps_identity_token_and_state(channels, scope):
    first = opened(channels, scope)
    assert opened(channels, scope)["id"] == first["id"]
    assert opened(channels, scope)["token"] == first["token"]
    channels.set_state(first["id"], 0, {"进度": "等材料"})
    rebooted = InteractionChannels(channels.path, clock=lambda: 1000)
    assert rebooted.authenticate(first["token"])["state"] == {"进度": "等材料"}
    assert rebooted.get(replace(scope, sender_id="other"), first["id"]) is None
    assert rebooted.authenticate("invalid") is None


def test_collection_uses_real_scope_cursor_and_time_window(channels, scope):
    channel = opened(channels, scope)
    assert channels.observe(scope, "m1", "第一句", ["Plain"], 1001) == 1
    assert channels.observe(scope, "m1", "重复", [], 1001) == 0
    assert (
        channels.observe(replace(scope, sender_id="other"), "m2", "其他人", [], 1002)
        == 0
    )
    channels.observe(scope, "m3", "第二句", ["Image"], 1003)
    result = channels.events(channel["id"], after_seq=0, since=1002, until=1004)
    assert [e["text"] for e in result["events"]] == ["第二句"]
    assert result["total_messages"] == 2
    assert result["messages_after_cursor"] == 2
    assert result["window_count"] == 1
    assert result["events"][0]["seq"] == 2
    assert result["last_message"]["text"] == "第二句"
    assert result["next_cursor"] == 2


def test_old_script_reply_is_rejected_after_new_message(channels, scope):
    c = opened(channels, scope)
    reply = channels.request_reply(c["id"], "r1", "叫用户起床", based_on_seq=0)
    assert (
        channels.request_reply(c["id"], "r1", "叫用户起床", based_on_seq=0)["id"]
        == reply["id"]
    )
    claimed = channels.claim_reply()
    channels.observe(scope, "m1", "醒了", [], 1001)
    assert not channels.begin_reply(claimed)
    assert channels.reply_status(c["id"], reply["id"])["status"] == "superseded"
    with pytest.raises(ValueError, match="stale_cursor"):
        channels.request_reply(c["id"], "r2", "不应再叫", based_on_seq=0)


def test_script_can_cancel_pending_then_resume_and_send_again(channels, scope):
    c = opened(channels, scope)
    first = channels.request_reply(c["id"], "r1", "待发送", based_on_seq=0)
    channels.control(c["id"], "stop", pause=True)
    assert channels.reply_status(c["id"], first["id"])["status"] == "superseded"
    with pytest.raises(ValueError):
        channels.request_reply(c["id"], "r2", "暂停时拒绝", based_on_seq=0)
    channels.observe(scope, "m1", "暂停时也能采集", [], 1001)
    assert channels.events(c["id"])["total_messages"] == 1
    channels.control(c["id"], "resume")
    channels.request_reply(c["id"], "r3", "继续", based_on_seq=1)
    claimed = channels.claim_reply()
    assert channels.begin_reply(claimed)
    channels.finish_reply(claimed, "sent")
    assert channels.reply_status(c["id"], claimed["id"])["status"] == "sent"


def test_unknown_delivery_not_retried_and_close_revokes_token(channels, scope):
    c = opened(channels, scope)
    channels.request_reply(c["id"], "r1", "只交平台一次", based_on_seq=0)
    claimed = channels.claim_reply()
    assert channels.begin_reply(claimed)
    channels.clock = lambda: 1200
    assert channels.claim_reply() is None
    assert channels.reply_status(c["id"], claimed["id"])["status"] == "unknown"
    channels.control(c["id"], "close")
    assert channels.authenticate(c["token"]) is None


def test_state_compare_and_swap_and_pagination(channels, scope):
    c = opened(channels, scope)
    channels.set_state(c["id"], 0, {"count": 1})
    with pytest.raises(ValueError, match="state_conflict"):
        channels.set_state(c["id"], 0, {"count": 2})
    for n in range(4):
        channels.observe(scope, f"m{n}", str(n), [], 1000 + n)
    page = channels.events(c["id"], limit=2)
    assert page["has_more"]
    assert page["next_cursor"] == 2
    assert [e["seq"] for e in channels.events(c["id"], after_seq=2)["events"]] == [3, 4]


def test_expiry_refuses_reply_and_event_collection(channels, scope):
    c = opened(channels, scope)
    channels.clock = lambda: 5000
    assert channels.authenticate(c["token"]) is None
    assert channels.observe(scope, "m1", "过期", [], 5000) == 0
    with pytest.raises(ValueError):
        channels.request_reply(c["id"], "r1", "过期不能发", based_on_seq=0)


@pytest.mark.parametrize("bad", [0, -1, True, float("inf"), float("nan")])
def test_invalid_channel_lifetime(channels, scope, bad):
    with pytest.raises(ValueError):
        channels.open(scope, "source", {}, ttl_seconds=bad)
