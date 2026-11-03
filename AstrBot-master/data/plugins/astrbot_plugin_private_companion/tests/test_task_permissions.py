import importlib
import os
import sqlite3
import subprocess
import sys
import time

import pytest


def test_permission_module_is_available():
    assert importlib.util.find_spec("task_permissions") is not None


@pytest.fixture
def permissions(tmp_path):
    api = importlib.import_module("task_permissions")
    root = tmp_path / "workspace"
    root.mkdir()
    store = api.TaskPermissionStore(
        tmp_path / "permissions.sqlite3",
        allowed_roots=[root],
        allowed_capabilities={"read", "write", "shell", "install", "network"},
    )
    owner = api.TaskActor("bot-1", "group-1", "owner", True)
    member = api.TaskActor("bot-1", "group-1", "member", False)
    return api, store, root, owner, member


def test_grant_is_persistent_and_bound_to_real_user(permissions):
    api, store, root, owner, member = permissions
    grant = store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    assert grant.granted_by == owner.user_id
    assert grant.expires_at is None
    assert store.check(member, "read", root / "file.txt")
    assert not store.check(member, "write", root)
    assert not store.check(
        api.TaskActor("bot-1", "group-1", "same-name", False), "read", root
    )
    reopened = api.TaskPermissionStore(
        store.db_path, allowed_roots=[root], allowed_capabilities={"read"}
    )
    assert reopened.check(member, "read", root)
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "bot,group", [("bot-2", "group-1"), ("bot-1", "group-2"), ("bot-1", "")]
)
def test_grants_never_cross_bot_group_or_private_chat(permissions, bot, group):
    api, store, root, owner, member = permissions
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    outsider = api.TaskActor(bot, group, member.user_id, False)
    assert not store.check(outsider, "read", root)
    assert store.list(outsider) == []


def test_ungranted_member_and_bot_administrator_have_no_implicit_access(permissions):
    api, store, root, _, member = permissions
    assert not store.check(member, "read", root)
    admin = api.TaskActor("bot-1", "group-1", "bot-admin", False)
    assert not store.check(admin, "shell", root)
    assert not store.check(admin, "bot_admin", root)


def test_primary_user_is_still_limited_by_host_policy(permissions, tmp_path):
    _, store, root, owner, _ = permissions
    assert store.check(owner, "shell", root)
    assert not store.check(owner, "read", tmp_path / "secret")
    assert not store.check(owner, "bot_admin", root)
    assert not store.check(owner, "unknown", root)


def test_host_policy_defaults_to_deny(permissions, tmp_path):
    api, _, root, owner, _ = permissions
    closed = api.TaskPermissionStore(tmp_path / "closed.sqlite3")
    assert not closed.check(owner, "read", root)
    with pytest.raises(PermissionError):
        closed.grant(owner, "member", capabilities={"read"}, roots=[root])


def test_member_cannot_delegate_or_revoke_even_with_execution_grant(permissions):
    _, store, root, owner, member = permissions
    grant = store.grant(owner, member.user_id, capabilities={"shell"}, roots=[root])
    with pytest.raises(PermissionError):
        store.grant(member, "other", capabilities={"shell"}, roots=[root])
    with pytest.raises(PermissionError):
        store.revoke(member, grant.grant_id)
    assert store.check(member, "shell", root)


def test_grant_cannot_exceed_host_roots_or_capabilities(permissions, tmp_path):
    _, store, root, owner, _ = permissions
    for capabilities, roots in [
        ({"bot_admin"}, [root]),
        ({"unknown"}, [root]),
        ({"read"}, [tmp_path]),
    ]:
        with pytest.raises(PermissionError):
            store.grant(owner, "member", capabilities=capabilities, roots=roots)
    assert store.list(owner) == []


def test_revocation_is_immediately_visible_to_other_store(permissions):
    api, store, root, owner, member = permissions
    grant = store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    reader = api.TaskPermissionStore(
        store.db_path, allowed_roots=[root], allowed_capabilities={"read"}
    )
    assert reader.check(member, "read", root)
    assert store.revoke(owner, grant.grant_id)
    assert not reader.check(member, "read", root)
    assert not store.revoke(owner, grant.grant_id)
    assert store.list(owner)[0].revoked_at is not None


def test_wrong_scope_primary_user_cannot_revoke_grant(permissions):
    api, store, root, owner, member = permissions
    grant = store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    for bot, group in [("other", "group-1"), ("bot-1", "other")]:
        assert not store.revoke(
            api.TaskActor(bot, group, "owner", True), grant.grant_id
        )
    assert store.check(member, "read", root)


def test_expiry_is_checked_each_time_including_exact_boundary(permissions, monkeypatch):
    _, store, root, owner, member = permissions
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    store.grant(
        owner, member.user_id, capabilities={"read"}, roots=[root], expires_at=now + 10
    )
    assert store.check(member, "read", root)
    monkeypatch.setattr(time, "time", lambda: now + 10)
    assert not store.check(member, "read", root)


@pytest.mark.parametrize("expiry", [0, float("nan"), float("inf"), True, "tomorrow"])
def test_invalid_or_elapsed_expiry_is_rejected(permissions, expiry):
    _, store, root, owner, _ = permissions
    with pytest.raises(ValueError):
        store.grant(
            owner, "member", capabilities={"read"}, roots=[root], expires_at=expiry
        )


def test_resolved_paths_cannot_escape_root_or_prefix_sibling(permissions, tmp_path):
    _, store, root, owner, member = permissions
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    assert store.check(member, "read", root / "sub" / ".." / "ok.txt")
    assert not store.check(member, "read", root / ".." / "outside.txt")
    assert not store.check(member, "read", tmp_path / "workspace-other" / "file.txt")
    assert not store.check(member, "read", "relative.txt")


def test_symlink_escape_is_denied(permissions, tmp_path):
    _, store, root, owner, member = permissions
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    assert not store.check(member, "read", link / "secret.txt")
    with pytest.raises(PermissionError):
        store.grant(owner, "other", capabilities={"read"}, roots=[link])


def test_distinct_grants_cannot_mix_capability_and_path(permissions):
    _, store, root, owner, member = permissions
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[first])
    store.grant(owner, member.user_id, capabilities={"write"}, roots=[second])
    assert store.check(member, "read", first)
    assert not store.check(member, "read", second)
    assert not store.check(member, "write", first)


def test_list_is_scoped_to_user_unless_primary(permissions):
    _, store, root, owner, member = permissions
    mine = store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    store.grant(owner, "other", capabilities={"write"}, roots=[root])
    assert len(store.list(owner)) == 2
    assert [item.grant_id for item in store.list(member)] == [mine.grant_id]
    assert store.list(member, user_id="other") == []


def test_host_policy_change_restricts_existing_grants(permissions):
    api, store, root, owner, member = permissions
    store.grant(owner, member.user_id, capabilities={"read", "shell"}, roots=[root])
    reader = api.TaskPermissionStore(
        store.db_path, allowed_roots=[root / "narrow"], allowed_capabilities={"read"}
    )
    assert reader.check(member, "read", root / "narrow" / "file.txt")
    assert not reader.check(member, "shell", root / "narrow")
    assert not reader.check(member, "read", root / "file.txt")


def test_actor_requires_explicit_boolean_trust_flag(permissions):
    api, _, _, _, _ = permissions
    for flag in ["false", "true", 1, None]:
        with pytest.raises(ValueError):
            api.TaskActor("bot-1", "group-1", "member", flag)


def test_private_chat_cannot_create_group_grants(permissions):
    api, store, root, _, _ = permissions
    with pytest.raises(PermissionError):
        store.grant(
            api.TaskActor("bot-1", "", "owner", True),
            "member",
            capabilities={"read"},
            roots=[root],
        )


def test_revocation_from_another_process_is_observed(permissions):
    _, store, root, owner, member = permissions
    grant = store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    assert store.check(member, "read", root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from task_permissions import TaskActor, TaskPermissionStore; "
                "store = TaskPermissionStore(sys.argv[1]); "
                "assert store.revoke(TaskActor('bot-1', 'group-1', 'owner', True), sys.argv[2])"
            ),
            str(store.db_path),
            grant.grant_id,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not store.check(member, "read", root)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_windows_junction_escape_is_denied(permissions, tmp_path):
    _, store, root, owner, member = permissions
    outside = tmp_path / "junction-outside"
    outside.mkdir()
    junction = root / "junction"
    result = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, repr(result.stderr)
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    try:
        assert not store.check(member, "read", junction / "secret.txt")
        with pytest.raises(PermissionError):
            store.grant(owner, "other", capabilities={"read"}, roots=[junction])
    finally:
        junction.rmdir()


def test_corrupt_database_fails_closed(permissions):
    _, store, root, owner, member = permissions
    store.grant(owner, member.user_id, capabilities={"read"}, roots=[root])
    store.db_path.write_bytes(b"invalid sqlite database")
    assert not store.check(member, "read", root)


def test_bot_admin_cannot_be_included_in_host_execution_policy(permissions, tmp_path):
    api, _, root, _, _ = permissions
    with pytest.raises(ValueError):
        api.TaskPermissionStore(
            tmp_path / "invalid.sqlite3",
            allowed_roots=[root],
            allowed_capabilities={"bot_admin"},
        )


@pytest.mark.parametrize("capability", [None, [], {}, 1])
def test_malformed_capability_is_denied(permissions, capability):
    _, store, root, owner, _ = permissions
    assert not store.check(owner, capability, root)
