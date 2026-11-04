import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.interaction_api import InteractionAPI
from astrbot_plugin_private_companion.interaction_scripts import InteractionScripts
from astrbot_plugin_private_companion.task_service import TaskScope, TaskService


@pytest.fixture
def scripts(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "custom.py").write_text("print('custom')", encoding="utf-8")

    def checked(_actor, _cap, relative=".", **_):
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise PermissionError("path escaped")
        return path

    broker = SimpleNamespace(
        checked_path=checked,
        run_powershell=AsyncMock(
            return_value=SimpleNamespace(exit_code=0, stdout="ok", stderr="")
        ),
    )
    runtime = SimpleNamespace(
        service=TaskService(tmp_path / "tasks.db", clock=lambda: 1000),
        plugin=SimpleNamespace(context=SimpleNamespace()),
        interaction_allowed=lambda _: True,
        interaction_actor=lambda _: "actor",
        get_execution_broker=lambda _: broker,
        prepare_interaction_config=AsyncMock(return_value=root / "config.json"),
        _safe_text=lambda text, limit: str(text)[:limit],
        workspace=tmp_path,
    )
    api = InteractionAPI(runtime)
    api.base_url = "http://127.0.0.1:6196"
    channel = api.channels.open(
        TaskScope("bot", "bot:FriendMessage:owner", "owner"), "source", {}
    )
    runner = InteractionScripts(api)
    return runner, channel, broker


@pytest.mark.asyncio
async def test_schedule_is_durable_and_idempotent_without_running_early(scripts):
    runner, channel, broker = scripts
    first = await runner.schedule(channel, path="custom.py", key="morning", run_at=1060)
    assert (
        await runner.schedule(channel, path="custom.py", key="morning", run_at=1060)
    )["id"] == first["id"]
    assert not await runner.tick()
    broker.run_powershell.assert_not_awaited()
    runner.api.channels.clock = lambda: 1060
    assert await runner.tick()
    await asyncio.gather(*runner.running.values())
    assert runner.status(channel, first["id"])["status"] == "finished"
    assert "ASTRBOT_INTERACTION_CONFIG" in broker.run_powershell.await_args.args[1]
    assert channel["token"] not in broker.run_powershell.await_args.args[1]


@pytest.mark.asyncio
async def test_restart_marks_inflight_interrupted_and_does_not_repeat(scripts):
    runner, channel, _ = scripts
    record = await runner.schedule(channel, path="custom.py", key="once")
    with runner.api.channels.transaction() as db:
        db.execute("update ic_scripts set status='running' where id=?", (record["id"],))
    reopened = InteractionScripts(runner.api)
    assert reopened.status(channel, record["id"])["status"] == "interrupted"
    assert not await reopened.tick()


@pytest.mark.asyncio
async def test_revoked_grant_or_closed_channel_cannot_start(scripts):
    runner, channel, broker = scripts
    record = await runner.schedule(channel, path="custom.py", key="one")
    runner.api.runtime.interaction_allowed = lambda _: False
    await runner.tick()
    assert runner.status(channel, record["id"])["status"] == "permission_denied"
    broker.run_powershell.assert_not_awaited()


@pytest.mark.asyncio
async def test_can_stop_queued_script_and_reject_cross_channel_status(scripts):
    runner, channel, _ = scripts
    record = await runner.schedule(channel, path="custom.py", key="one", run_at=1060)
    result = await runner.cancel(channel, record["id"])
    assert result["status"] == "cancelled"
    with pytest.raises(ValueError):
        runner.status({**channel, "id": "other"}, record["id"])
    assert not await runner.tick()


@pytest.mark.asyncio
async def test_script_timeout_and_arguments_are_bounded_and_quoted(scripts):
    runner, channel, broker = scripts
    await runner.schedule(
        channel,
        path="custom.py",
        key="one",
        arguments=["a'b", "$(not-executed)"],
        timeout=300,
    )
    await runner.tick()
    await asyncio.gather(*runner.running.values())
    command = broker.run_powershell.await_args.args[1]
    assert "'a''b'" in command and "'$(not-executed)'" in command
    assert broker.run_powershell.await_args.kwargs["timeout"] == 300
    with pytest.raises(ValueError):
        await runner.schedule(channel, path="custom.py", key="bad", timeout=3601)
    with pytest.raises(PermissionError):
        await runner.schedule(channel, path="../outside.py", key="outside")


@pytest.mark.asyncio
async def test_list_scripts_discovers_existing_pending_without_duplicate(scripts):
    runner, channel, _ = scripts
    record = await runner.schedule(channel, path="custom.py", key="future", run_at=1060)
    rows = runner.list(channel)
    assert [row["id"] for row in rows] == [record["id"]]
    assert rows[0]["status"] == "pending"
    assert runner.list({**channel, "id": "other"}) == []


@pytest.mark.asyncio
async def test_expired_pending_has_terminal_receipt_instead_of_waiting_forever(scripts):
    runner, channel, broker = scripts
    record = await runner.schedule(channel, path="custom.py", key="future", run_at=1060)
    runner.api.channels.clock = lambda: channel["expires_at"] + 1
    assert await runner.tick()
    row = runner.status(channel, record["id"])
    assert row["status"] == "expired"
    assert row["error_code"] == "channel_expired"
    assert row["finished_at"] == channel["expires_at"] + 1
    broker.run_powershell.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_script_records_root_error_and_trusted_callback_once(scripts):
    runner, channel, broker = scripts
    callback = AsyncMock(return_value=True)
    runner.on_script_event = callback
    broker.run_powershell.side_effect = RuntimeError("missing_dependency " + channel["token"])
    record = await runner.schedule(channel, path="custom.py", key="failure")
    await runner.tick()
    await asyncio.gather(*runner.running.values())
    await runner.tick()
    await runner.tick()
    row = runner.status(channel, record["id"])
    assert row["status"] == "failed"
    assert "missing_dependency" in row["stderr"]
    assert channel["token"] not in row["stderr"]
    assert row["finished_at"] == 1000
    callback.assert_awaited_once()
    event = callback.await_args.args[0]
    assert event["event_id"] == f"script:{record['id']}:failed"
    assert event["channel"]["sender_id"] == channel["sender_id"]
    assert event["channel"]["bot_id"] == channel["bot_id"]
    assert channel["token"] not in str(event)
    assert broker.run_powershell.await_count == 1
    restarted = InteractionScripts(runner.api, on_script_event=callback)
    await restarted.tick()
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_restart_interruption_and_expiry_callback_keep_stable_event_identity(scripts):
    runner, channel, _ = scripts
    running = await runner.schedule(channel, path="custom.py", key="running")
    with runner.api.channels.transaction() as db:
        db.execute("UPDATE ic_scripts SET status='running' WHERE id=?", (running["id"],))
    callback = AsyncMock(return_value=True)
    runner = InteractionScripts(runner.api, on_script_event=callback)
    await runner.tick()
    assert callback.await_args.args[0]["status"] == "interrupted"
    assert runner.status(channel, running["id"])["error_code"] == "service_restarted"
    future = await runner.schedule(channel, path="custom.py", key="future", run_at=1060)
    runner.api.channels.clock = lambda: channel["expires_at"] + 1
    await runner.tick()
    assert callback.await_count == 2
    assert callback.await_args.args[0]["event_id"] == f"script:{future['id']}:expired"


@pytest.mark.asyncio
async def test_revoked_authorization_suppresses_failure_callback_and_no_script_replay(scripts):
    runner, channel, broker = scripts
    callback = AsyncMock(return_value=True)
    runner.on_script_event = callback
    broker.run_powershell.side_effect = TimeoutError("process timeout")
    record = await runner.schedule(channel, path="custom.py", key="timeout")
    await runner.tick()
    await asyncio.gather(*runner.running.values())
    runner.api.runtime.interaction_allowed = lambda _: False
    await runner.tick()
    assert runner.status(channel, record["id"])["error_code"] == "script_timeout"
    callback.assert_not_awaited()
    assert broker.run_powershell.await_count == 1


@pytest.mark.asyncio
async def test_callback_migration_does_not_resubmit_historical_failed_scripts(scripts):
    runner, channel, _ = scripts
    record = await runner.schedule(channel, path="custom.py", key="old")
    with runner.api.channels.transaction() as db:
        db.execute("UPDATE ic_scripts SET status='failed' WHERE id=?", (record["id"],))
    callback = AsyncMock(return_value=True)
    restarted = InteractionScripts(runner.api, on_script_event=callback)
    await restarted.tick()
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_finished_receipt_is_persisted_without_requesting_model_repair(scripts):
    runner, channel, _ = scripts
    runner.on_script_event = AsyncMock(return_value=True)
    record = await runner.schedule(channel, path="custom.py", key="success")
    await runner.tick()
    await asyncio.gather(*runner.running.values())
    await runner.tick()
    with runner.api.channels.transaction() as db:
        event = db.execute("SELECT * FROM ic_script_events WHERE script_id=?", (record["id"],)).fetchone()
    assert event["status"] == "finished"
    assert event["callback_status"] == "receipt"
    assert runner.status(channel, record["id"])["finished_at"] == 1000
    runner.on_script_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_retry_after_restart_reuses_event_id_without_running_script(scripts):
    runner, channel, broker = scripts
    callback = AsyncMock(side_effect=[RuntimeError("host submission unavailable"), True])
    runner.on_script_event = callback
    broker.run_powershell.side_effect = TimeoutError("timeout")
    record = await runner.schedule(channel, path="custom.py", key="once")
    await runner.tick()
    await asyncio.gather(*runner.running.values())
    await runner.tick()
    restarted = InteractionScripts(runner.api, on_script_event=callback)
    await restarted.tick()
    await restarted.tick()
    assert callback.await_count == 2
    assert callback.await_args_list[0].args[0]["event_id"] == callback.await_args_list[1].args[0]["event_id"]
    assert callback.await_args.args[0]["script_id"] == record["id"]
    assert broker.run_powershell.await_count == 1
