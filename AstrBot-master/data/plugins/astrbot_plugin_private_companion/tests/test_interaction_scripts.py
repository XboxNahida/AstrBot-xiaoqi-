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
