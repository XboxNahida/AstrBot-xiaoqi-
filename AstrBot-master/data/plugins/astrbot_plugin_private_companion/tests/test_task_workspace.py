import asyncio
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from task_execution import ExecutionResult, actor_workspace
from task_permissions import TaskActor, TaskPermissionStore


def account(actor):
    return "abt_" + hashlib.sha256(
        "\0".join((actor.bot_id, actor.group_id, actor.user_id)).encode()
    ).hexdigest()[:16]


@pytest.fixture
def directory(tmp_path):
    api = importlib.import_module("task_workspace")
    root, records = tmp_path / "tasks", tmp_path / "broker"
    records.mkdir()
    permissions = TaskPermissionStore(
        tmp_path / "permissions.db", allowed_roots=[root],
        allowed_capabilities={"read", "write", "shell", "network", "install"},
    )
    actors = [
        TaskActor("bot", "g1", "owner", True),
        TaskActor("bot", "g1", "alice", False),
        TaskActor("bot", "g1", "bob", False),
        TaskActor("bot", "g2", "carol", False),
        TaskActor("bot", "", "owner", True),
        TaskActor("bot", "", "bob", False),
        TaskActor("other-bot", "g1", "alice", False),
    ]
    brokers, grants = {}, {}
    for actor in actors:
        workspace = actor_workspace(root, actor)
        workspace.mkdir(parents=True)
        (workspace / "中文文件.txt").write_text("真实内容", encoding="utf-8")
        if not actor.is_primary_user and actor.group_id:
            owner = TaskActor(actor.bot_id, actor.group_id, "owner", True)
            grants[actor] = permissions.grant(
                owner, actor.user_id, capabilities=["read"], roots=[workspace],
            )
        (records / (account(actor) + ".ready.json")).write_text(json.dumps({
            "bot_id": actor.bot_id, "group_id": actor.group_id,
            "user_id": actor.user_id, "enabled": True,
            "verification": "passed", "workspace": str(workspace),
        }), encoding="utf-8")
        def checked(who, cap, relative=".", *, _verify=True, expected=actor):
            path = actor_workspace(root, who) / relative
            if who != expected or not permissions.check(who, cap, path):
                raise PermissionError("revoked")
            if ".." in Path(relative).parts or Path(relative).is_absolute():
                raise PermissionError("unsafe")
            if path.is_symlink():
                raise PermissionError("reparse")
            return path
        brokers[actor] = SimpleNamespace(
            permissions=permissions, checked_path=checked,
            _run_payload=AsyncMock(return_value=ExecutionResult(0, "真实内容")),
        )
    service = api.TaskWorkspaceDirectory(
        task_root=root, records_root=records,
        resolve_actor=lambda b, g, u: next(a for a in actors if (a.bot_id, a.group_id, a.user_id) == (b, g, u)),
        broker_for_actor=brokers.__getitem__,
    )
    return service, actors, brokers, permissions, grants, records, root


def test_catalog_group_read_scope_and_own_write_capabilities(directory):
    service, actors, _, _, _, _, _ = directory
    result = asyncio.run(service.call(actors[1], action="info"))
    assert result["success"]
    assert {x["workspace_id"] for x in result["workspaces"]} == {account(a) for a in actors[:3]}
    assert result["current_workspace"]["workspace_id"] == account(actors[1])
    assert result["current_workspace"]["capabilities"] == ["read"]
    assert all(not item["writable"] for item in result["workspaces"])
    assert "token" not in json.dumps(result)


def test_primary_private_can_discover_group_workspaces_but_no_other_private(directory):
    service, actors, _, _, _, _, _ = directory
    result = asyncio.run(service.call(actors[4], action="info"))
    assert {x["workspace_id"] for x in result["workspaces"]} == {account(a) for a in actors[:5]}
    assert result["current_workspace"]["writable"]


def test_cross_member_read_uses_only_target_fixed_read_payload(directory):
    service, actors, brokers, _, _, _, _ = directory
    result = asyncio.run(service.call(
        actors[1], action="read", workspace_id=account(actors[2]), path="中文文件.txt",
    ))
    assert result["success"] and result["stdout"] == "真实内容"
    target_call = brokers[actors[2]]._run_payload.call_args
    assert target_call.args[0] == actors[2]
    assert target_call.kwargs["capability"] == "read"
    assert "ReadAllText" in target_call.args[1]
    brokers[actors[1]]._run_payload.assert_not_awaited()


@pytest.mark.parametrize("target_index", [3, 5, 6])
def test_member_cannot_select_other_group_private_or_bot(directory, target_index):
    service, actors, brokers, _, _, _, _ = directory
    with pytest.raises(PermissionError):
        asyncio.run(service.call(actors[1], action="read", workspace_id=account(actors[target_index]), path="中文文件.txt"))
    brokers[actors[target_index]]._run_payload.assert_not_awaited()


@pytest.mark.parametrize("relative", [".interaction-example.json", ".runtime/python.exe", ".venv/pyvenv.cfg", ".tmp/log", ".env", "sub/.interaction-example.json"])
def test_internal_material_is_never_shared(directory, relative):
    service, actors, brokers, _, _, _, _ = directory
    with pytest.raises(PermissionError):
        asyncio.run(service.call(actors[1], action="read", workspace_id=account(actors[2]), path=relative))
    brokers[actors[2]]._run_payload.assert_not_awaited()


def test_revocation_during_read_discards_result(directory):
    service, actors, brokers, store, grants, _, _ = directory
    async def revoke(*args, **kwargs):
        store.revoke(actors[0], grants[actors[1]].grant_id)
        return ExecutionResult(0, "must not escape")
    brokers[actors[2]]._run_payload.side_effect = revoke
    with pytest.raises(PermissionError):
        asyncio.run(service.call(actors[1], action="read", workspace_id=account(actors[2]), path="中文文件.txt"))


def test_catalog_rejects_forged_manifest_and_excludes_revoked_peer(directory):
    service, actors, _, store, grants, records, _ = directory
    store.revoke(actors[0], grants[actors[2]].grant_id)
    (records / ("abt_" + "0" * 16 + ".ready.json")).write_text(json.dumps({
        "bot_id": "bot", "group_id": "g1", "user_id": "bob", "enabled": True,
        "verification": "passed", "workspace": "C:/Windows",
    }), encoding="utf-8")
    result = asyncio.run(service.call(actors[1], action="info"))
    assert {x["workspace_id"] for x in result["workspaces"]} == {account(a) for a in actors[:2]}


def test_list_is_bounded_and_does_not_recurse_through_internal_paths(directory):
    service, actors, brokers, _, _, _, _ = directory
    brokers[actors[2]]._run_payload.return_value = ExecutionResult(0, "[]")
    asyncio.run(service.call(actors[1], action="list", workspace_id=account(actors[2])))
    code = brokers[actors[2]]._run_payload.call_args.args[1]
    assert "-Recurse" not in code and "ReparsePoint" in code and "500" in code
    assert "StartsWith('.')" in code


def test_failed_listing_is_recoverable_but_has_real_failure_evidence(directory):
    service, actors, brokers, _, _, _, _ = directory
    brokers[actors[1]]._run_payload.return_value = ExecutionResult(1, "", "not found")
    result = asyncio.run(service.call(actors[1], action="list", path="不存在"))
    assert result["success"] is False and result["recoverable"] is True
    assert result["exit_code"] == 1 and result["stderr"] == "not found"
    assert result["hint"]


def test_large_listing_preserves_whole_names_and_exposes_next_offset(directory):
    service, actors, brokers, _, _, _, _ = directory
    files = [{"name": str(i) + "文" * 170 + ".txt", "directory": False} for i in range(120)]
    brokers[actors[1]]._run_payload.return_value = ExecutionResult(0, json.dumps(files, ensure_ascii=False))
    result = asyncio.run(service.call(actors[1], action="list"))
    entries = json.loads(result["stdout"])
    assert entries and len(entries) < 120
    assert entries == files[:len(entries)]
    assert result["next_offset"] == len(entries)
