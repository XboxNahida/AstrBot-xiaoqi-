import asyncio
import hashlib
import json

import pytest
from task_account_setup import AccountSetupCoordinator
from task_permissions import TaskActor, TaskPermissionStore


def account(actor):
    return (
        "abt_"
        + hashlib.sha256(
            "\0".join((actor.bot_id, actor.group_id, actor.user_id)).encode()
        ).hexdigest()[:16]
    )


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import task_account_setup as api

    monkeypatch.setattr(api, "_SETUP_LOCK", asyncio.Lock())
    tasks, broker = tmp_path / "tasks", tmp_path / "broker"
    tasks.mkdir()
    broker.mkdir()
    store = TaskPermissionStore(
        tmp_path / "permissions.db",
        allowed_roots=[tasks],
        allowed_capabilities={"read", "write", "shell", "install", "search"},
    )
    owner = TaskActor("bot", "group", "owner", True)
    calls = []
    fail = set()

    async def runner(mode, actor):
        calls.append((mode, actor.user_id))
        if (mode, actor.user_id) in fail:
            return 1
        name = account(actor)
        record = {
            "schema": 1,
            "bot_id": actor.bot_id,
            "group_id": actor.group_id,
            "user_id": actor.user_id,
            "user_sid": "S-1-5-21-1-2-3-1001",
            "workspace": str(coordinator.workspace(actor)),
            "credential": str(broker / (name + ".credential.dpapi")),
            "protected_roots": [str(coordinator.bot_root), str(broker)],
        }
        if mode == "Provision":
            (broker / (name + ".candidate.json")).write_text(json.dumps(record))
            (broker / (name + ".credential.dpapi")).write_bytes(b"fake")
        else:
            record.update(enabled=True, verification="passed")
            (broker / (name + ".ready.json")).write_text(json.dumps(record))
        return 0

    coordinator = AccountSetupCoordinator(
        store,
        runner=runner,
        task_root=tasks,
        broker_root=broker,
        bot_root=tmp_path / "bot",
        resolve_actor=lambda bot, group, user: TaskActor(
            bot, group, user, user == "owner"
        ),
    )
    return coordinator, store, owner, calls, fail, broker


@pytest.mark.asyncio
async def test_new_owner_provision_verify_and_ready_is_not_recreated(setup):
    coordinator, _, actor, calls, _, _ = setup
    assert (await coordinator.prepare(actor)).ready
    assert calls == [("Provision", "owner"), ("Verify", "owner")]
    assert (await coordinator.prepare(actor)).ready
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_member_needs_current_execution_capability(setup):
    coordinator, store, owner, calls, _, _ = setup
    member = TaskActor("bot", "group", "member", False)
    assert (await coordinator.prepare(member)).code == "permission_denied"
    store.grant(
        owner, "member", roots=[coordinator.workspace(member)], capabilities=["search"]
    )
    assert (await coordinator.prepare(member)).code == "permission_denied"
    store.grant(
        owner, "member", roots=[coordinator.workspace(member)], capabilities=["read"]
    )
    assert (await coordinator.prepare(member)).ready
    assert calls == [("Provision", "member"), ("Verify", "member")]


@pytest.mark.asyncio
async def test_new_scope_refreshes_all_previously_ready_accounts(setup):
    coordinator, _, owner, calls, _, _ = setup
    assert (await coordinator.prepare(owner)).ready
    second = TaskActor("other-bot", "", "owner", True)
    assert (await coordinator.prepare(second)).ready
    assert calls[-3:] == [
        ("Provision", "owner"),
        ("Verify", "owner"),
        ("Verify", "owner"),
    ]


@pytest.mark.asyncio
async def test_failed_verification_cannot_be_automatically_resumed(setup):
    coordinator, _, actor, calls, fail, broker = setup
    fail.add(("Verify", "owner"))
    result = await coordinator.prepare(actor)
    assert not result.ready
    assert not (broker / (account(actor) + ".ready.json")).exists()
    fail.clear()
    assert (await coordinator.prepare(actor)).code == "audit_required"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_live_ready_failure_logs_fixed_reason_without_reprovision(setup, monkeypatch, caplog):
    import task_account_setup as api

    coordinator, _, actor, calls, _, broker = setup
    assert (await coordinator.prepare(actor)).ready
    coordinator._production = True
    monkeypatch.setattr(api, "_host_acl_probe", lambda *args, **kw: {})

    def invalid(*args, **kw):
        raise PermissionError("Account or ACL deployment invariant changed")

    monkeypatch.setattr(api.WindowsDeploymentVerifier, "verify", invalid)
    with caplog.at_level("WARNING"):
        result = await coordinator.prepare(actor)
    assert not result.ready and result.code == "audit_required"
    assert "Account or ACL deployment invariant changed" in caplog.text
    assert account(actor) in caplog.text
    assert len(calls) == 2
    assert not (broker / (account(actor) + ".ready.json")).exists()


@pytest.mark.asyncio
async def test_refresh_failure_removes_previous_ready(setup):
    coordinator, _, owner, _, fail, broker = setup
    assert (await coordinator.prepare(owner)).ready
    fail.add(("Verify", "owner"))
    second = TaskActor("other", "", "another", True)
    result = await coordinator.prepare(second)
    assert result.ready
    assert result.failed_accounts == (account(owner),)
    assert not (broker / (account(owner) + ".ready.json")).exists()


@pytest.mark.asyncio
async def test_cancellation_closes_ready_and_never_retries_partial_account(setup):
    coordinator, _, actor, _, _, broker = setup
    entered = asyncio.Event()

    async def slow(mode, actor):
        entered.set()
        await asyncio.Event().wait()

    coordinator.runner = slow
    pending = asyncio.create_task(coordinator.prepare(actor))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not (broker / (account(actor) + ".ready.json")).exists()
    assert (await coordinator.prepare(actor)).code == "audit_required"


@pytest.mark.asyncio
async def test_coordinators_share_single_global_lock(setup):
    coordinator, _, actor, _, _, _ = setup
    original = coordinator.runner
    active = 0
    peak = 0

    async def measured(mode, actor):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        result = await original(mode, actor)
        active -= 1
        return result

    coordinator.runner = measured
    await asyncio.gather(coordinator.prepare(actor), coordinator.prepare(actor))
    assert peak == 1


@pytest.mark.asyncio
async def test_failed_provision_still_refreshes_old_ready(setup):
    coordinator, _, owner, calls, fail, broker = setup
    assert (await coordinator.prepare(owner)).ready
    second = TaskActor("other", "", "another", True)
    fail.add(("Provision", "another"))
    result = await coordinator.prepare(second)
    assert not result.ready
    assert calls[-2:] == [("Provision", "another"), ("Verify", "owner")]
    assert (broker / (account(owner) + ".ready.json")).exists()


@pytest.mark.asyncio
async def test_revoked_waiting_request_never_provisions(setup):
    import task_account_setup as api

    coordinator, store, owner, calls, _, _ = setup
    member = TaskActor("bot", "group", "member", False)
    grant = store.grant(
        owner, "member", roots=[coordinator.workspace(member)], capabilities=["read"]
    )
    async with api._SETUP_LOCK:
        pending = asyncio.create_task(coordinator.prepare(member))
        await asyncio.sleep(0)
        store.revoke(owner, grant.grant_id)
    assert (await pending).code == "permission_denied"
    assert not calls


@pytest.mark.asyncio
async def test_system_runner_cancellation_waits_for_started_handle_then_closes(
    monkeypatch,
):
    import threading

    import task_account_setup as api

    entered, release = threading.Event(), threading.Event()
    closed = []

    def delayed_process(*args):
        entered.set()
        release.wait(timeout=5)
        return type("Process", (), {"close": lambda self: closed.append(True)})()

    monkeypatch.setattr(api, "_SystemSetupProcess", delayed_process)
    task = asyncio.create_task(
        api._run_system_script("Provision", TaskActor("bot", "", "user", True))
    )
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]


def test_default_runner_rejects_custom_roots(setup):
    coordinator, store, *_ = setup
    with pytest.raises(ValueError, match="fixed"):
        AccountSetupCoordinator(store, task_root=coordinator.task_root)
