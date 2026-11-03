import importlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def page(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    package = "astrbot_plugin_private_companion"
    if package not in sys.modules:
        stub = types.ModuleType(package)
        stub.__path__ = [str(root)]
        monkeypatch.setitem(sys.modules, package, stub)
    module = importlib.import_module(f"{package}.page_api_tasks")
    service_module = importlib.import_module(f"{package}.task_service")
    permissions = importlib.import_module(f"{package}.task_permissions")
    policies = importlib.import_module(f"{package}.task_proactive_policy")
    runtime = SimpleNamespace(
        service=service_module.TaskService(tmp_path / "tasks.db"),
        permissions=permissions.TaskPermissionStore(
            tmp_path / "grants.db",
            allowed_roots=[tmp_path],
            allowed_capabilities={"read", "write"},
        ),
        policy=policies.ProactivePolicyStore(tmp_path / "policy.db"),
        authorize_job=lambda job: True,
        dashboard_actor=lambda bot, group, username: permissions.TaskActor(
            bot, group, "real-owner", True
        ),
    )

    class Page(module.TasksPageApiMixin):
        def _ok(self, data):
            return {"success": True, "data": data}

        def _error(self, message, *, status_code=400):
            return {"success": False, "error": message, "status_code": status_code}

    api = Page()
    api.plugin = SimpleNamespace(_persistent_task_runtime=runtime)
    request = SimpleNamespace(
        username="dashboard-admin", args={}, get_json=AsyncMock(return_value={})
    )
    monkeypatch.setattr(module, "request", request)
    scope = service_module.TaskScope("bot", "bot:GroupMessage:group", "sender")
    request.args.update(vars(scope))
    return api, request, runtime, scope, tmp_path


@pytest.mark.asyncio
async def test_all_management_reads_require_trusted_login(page):
    api, request, _, _, _ = page
    request.username = ""
    request.args["username"] = "admin"
    for handler in (
        api.get_tasks,
        api.get_task_detail,
        api.get_task_grants,
        api.get_task_proactive_policy,
    ):
        assert (await handler())["status_code"] == 401


@pytest.mark.asyncio
async def test_list_is_scoped_paginated_and_hides_runtime_secrets(page):
    api, request, runtime, scope, root = page
    for i in range(25):
        runtime.service.submit(
            scope,
            str(i),
            "main",
            f"goal-{i}",
            root,
            metadata={"runtime": {"system_prompt": "PRIVATE"}},
        )
    result = await api.get_tasks()
    assert len(result["data"]["tasks"]) == 20
    assert result["data"]["total"] == 25
    request.args["offset"] = "20"
    second = await api.get_tasks()
    assert len(second["data"]["tasks"]) == 5
    assert "PRIVATE" not in str(result)
    request.args["sender_id"] = "other"
    assert (await api.get_tasks())["data"]["tasks"] == []


@pytest.mark.asyncio
async def test_detail_scopes_and_safe_controls_recheck_execution_permission(page):
    api, request, runtime, scope, root = page
    task = runtime.service.submit(
        scope, "1", "main", "goal", root, metadata={"request": "PRIVATE"}
    )
    request.args["task_id"] = task.task_id
    assert "PRIVATE" not in str(await api.get_task_detail())
    request.get_json.return_value = {
        **vars(scope),
        "task_id": task.task_id,
        "action": "pause",
    }
    assert (await api.control_task())["success"]
    runtime.authorize_job = lambda job: False
    request.get_json.return_value["action"] = "resume"
    assert (await api.control_task())["status_code"] == 403
    request.get_json.return_value.update(action="cancel", sender_id="other")
    assert (await api.control_task())["status_code"] == 404
    assert runtime.service.get(task.task_id, scope).status == "paused"


@pytest.mark.asyncio
async def test_grants_ignore_payload_actor_and_require_server_mapping(page):
    api, request, runtime, _, root = page
    request.get_json.return_value = {
        "bot_id": "bot",
        "group_id": "group",
        "user_id": "friend",
        "capabilities": ["read"],
        "roots": [str(root)],
        "actor": "admin",
        "is_primary_user": True,
    }
    runtime.dashboard_actor = None
    assert (await api.grant_task_access())["status_code"] == 403
    permissions = importlib.import_module(
        "astrbot_plugin_private_companion.task_permissions"
    )
    runtime.dashboard_actor = lambda bot, group, username: permissions.TaskActor(
        bot, group, "real-owner", True
    )
    response = await api.grant_task_access()
    assert response["success"]
    assert response["data"]["grant"]["granted_by"] == "real-owner"
    request.args.update(bot_id="bot", group_id="group")
    grants = (await api.get_task_grants())["data"]["grants"]
    assert len(grants) == 1
    request.get_json.return_value["grant_id"] = grants[0]["grant_id"]
    assert (await api.revoke_task_access())["success"]


@pytest.mark.asyncio
async def test_policy_checks_real_cron_owner_and_updates_existing_schedule_only(page):
    api, request, runtime, scope, _ = page
    cron = SimpleNamespace(
        payload={
            "origin": "tool",
            "source": "reminder",
            "session": scope.session_key,
            "sender_id": scope.sender_id,
        },
        cron_expression="0 * * * *",
        timezone="Asia/Shanghai",
        run_once=False,
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(return_value=cron),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "config": {"max_deliveries": 2},
    }
    assert (await api.update_task_proactive_policy())["success"]
    runtime.cron_manager.update_job.assert_not_called()
    request.get_json.return_value["cron_expression"] = "*/30 * * * *"
    assert (await api.update_task_proactive_policy())["success"]
    runtime.cron_manager.update_job.assert_awaited_once_with(
        "existing", cron_expression="*/30 * * * *"
    )
    cron.payload["sender_id"] = "other"
    assert (await api.update_task_proactive_policy())["status_code"] == 403
    cron.payload["sender_id"] = scope.sender_id
    request.get_json.return_value["bot_id"] = "different"
    assert (await api.update_task_proactive_policy())["status_code"] == 403


@pytest.mark.asyncio
async def test_mutation_auth_is_not_replaceable_by_json_identity(page):
    api, request, _, scope, _ = page
    request.username = ""
    request.get_json.return_value = {
        **vars(scope),
        "username": "admin",
        "actor": "real-owner",
        "admin": True,
        "is_primary_user": True,
    }
    for handler in (
        api.control_task,
        api.grant_task_access,
        api.revoke_task_access,
        api.update_task_proactive_policy,
    ):
        assert (await handler())["status_code"] == 401


@pytest.mark.asyncio
async def test_grant_rejects_actor_from_wrong_bot_or_group(page):
    api, request, runtime, _, root = page
    permissions = importlib.import_module(
        "astrbot_plugin_private_companion.task_permissions"
    )
    request.get_json.return_value = {
        "bot_id": "bot",
        "group_id": "group",
        "user_id": "friend",
        "capabilities": ["read"],
        "roots": [str(root)],
    }
    runtime.dashboard_actor = lambda *args: permissions.TaskActor(
        "wrong-bot", "group", "real-owner", True
    )
    assert (await api.grant_task_access())["status_code"] == 403


@pytest.mark.asyncio
async def test_evidence_redacts_credentials_and_never_exposes_lease(page):
    api, request, runtime, scope, root = page
    runtime.service.submit(scope, "1", "main", "goal", root)
    job = runtime.service.claim("worker")
    runtime.service.record_step(
        job,
        "main",
        {
            "summary": "done",
            "token": "PRIVATE",
            "nested": {"system_prompt": "PRIVATE", "info": "api_key=PRIVATE"},
        },
    )
    request.args["task_id"] = job.task_id
    data = (await api.get_task_detail())["data"]["task"]
    assert "PRIVATE" not in str(data)
    assert "lease" not in str(data)
    assert data["steps"]["main"]["summary"] == "done"


@pytest.mark.asyncio
async def test_invalid_cron_does_not_write_policy_or_reschedule(page):
    api, request, runtime, scope, _ = page
    cron = SimpleNamespace(
        payload={
            "origin": "tool",
            "source": "reminder",
            "session": scope.session_key,
            "sender_id": scope.sender_id,
        },
        cron_expression="0 * * * *",
        run_once=False,
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(return_value=cron),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "cron_expression": "99 * * * *",
        "config": {"max_deliveries": 1},
    }
    assert (await api.update_task_proactive_policy())["status_code"] == 400
    assert runtime.policy.get(scope, "existing") is None
    runtime.cron_manager.update_job.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_failure_restores_policy_and_prior_schedule(page):
    api, request, runtime, scope, _ = page
    cron = SimpleNamespace(
        payload={
            "origin": "tool",
            "source": "reminder",
            "session": scope.session_key,
            "sender_id": scope.sender_id,
        },
        cron_expression="0 * * * *",
        run_once=False,
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(side_effect=[ValueError("fail"), cron]),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "cron_expression": "*/30 * * * *",
        "config": {"max_deliveries": 1},
    }
    assert (await api.update_task_proactive_policy())["status_code"] == 400
    assert runtime.policy.get(scope, "existing") is None
    assert runtime.cron_manager.update_job.await_args.kwargs == {
        "cron_expression": "0 * * * *"
    }


@pytest.mark.asyncio
async def test_task_evidence_has_shared_response_size_budget(page):
    api, request, runtime, scope, root = page
    runtime.service.submit(scope, "budget", "main", "goal", root)
    job = runtime.service.claim("worker")
    assert runtime.service.record_step(job, "main", {"files": ["x" * 10000] * 30})
    request.args["task_id"] = job.task_id
    assert len(json.dumps(await api.get_task_detail())) < 16000


@pytest.mark.asyncio
async def test_revise_uses_service_state_and_authenticated_admin(page):
    api, request, runtime, scope, root = page
    job = runtime.service.submit(scope, "revise", "main", "old", root)
    request.get_json.return_value = {
        **vars(scope),
        "task_id": job.task_id,
        "action": "revise",
        "goal": "new goal",
    }
    assert (await api.control_task())["success"]
    assert runtime.service.get(job.task_id, scope).goal == "new goal"
    runtime.service.claim("worker")
    assert (await api.control_task())["status_code"] == 409
    request.username = ""
    assert (await api.control_task())["status_code"] == 401


@pytest.mark.asyncio
async def test_reconcile_requires_evidence_strict_boolean_and_cannot_retry_unknown(
    page,
):
    api, request, runtime, scope, root = page
    job = runtime.service.submit(scope, "reconcile", "main", "goal", root)
    runtime.service.claim("worker")
    assert runtime.service.pause(job.task_id, scope)
    request.get_json.return_value = {
        **vars(scope),
        "task_id": job.task_id,
        "action": "retry",
    }
    assert (await api.control_task())["status_code"] == 409
    request.get_json.return_value.update(
        action="reconcile",
        outcome="completed",
        evidence={"receipt": "verified"},
        worker_stopped="true",
    )
    assert (await api.control_task())["status_code"] == 400
    request.get_json.return_value["worker_stopped"] = True
    request.get_json.return_value["evidence"] = {}
    assert (await api.control_task())["status_code"] == 400
    request.get_json.return_value["evidence"] = {"receipt": "verified"}
    request.get_json.return_value["worker_stopped"] = False
    assert (await api.control_task())["status_code"] == 409
    request.get_json.return_value["worker_stopped"] = True
    assert (await api.control_task())["success"]
    assert runtime.service.get(job.task_id, scope).status == "succeeded"


@pytest.mark.asyncio
async def test_policy_explicit_kind_persists_source_without_guessing_note(page):
    api, request, runtime, scope, _ = page
    cron = SimpleNamespace(
        payload={
            "origin": "tool",
            "session": scope.session_key,
            "sender_id": scope.sender_id,
            "note": "Search news",
            "other": 42,
        },
        cron_expression="0 * * * *",
        run_once=False,
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(return_value=cron),
    )
    request.args["rule_id"] = "existing"
    assert (await api.get_task_proactive_policy())["data"]["task_kind"] is None
    request.get_json.return_value = {**vars(scope), "rule_id": "existing", "config": {}}
    assert (await api.update_task_proactive_policy())["status_code"] == 400
    assert runtime.policy.get(scope, "existing") is None
    request.get_json.return_value["task_kind"] = "monitor"
    assert (await api.update_task_proactive_policy())["success"]
    payload = runtime.cron_manager.update_job.await_args.kwargs["payload"]
    assert payload == {**cron.payload, "source": "monitor"}
    assert "source" not in cron.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "api", "plugin"])
async def test_policy_never_adopts_non_tool_cron(page, origin):
    api, request, runtime, scope, _ = page
    cron = SimpleNamespace(
        payload={
            "origin": origin,
            "session": scope.session_key,
            "sender_id": scope.sender_id,
            "source": "reminder",
        },
        cron_expression="0 * * * *",
        run_once=False,
    )
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(return_value=cron),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "task_kind": "monitor",
        "config": {},
    }
    assert (await api.update_task_proactive_policy())["status_code"] == 403
    assert runtime.policy.get(scope, "existing") is None
    runtime.cron_manager.update_job.assert_not_called()


@pytest.mark.asyncio
async def test_source_and_schedule_failure_restore_prior_policy_and_payload(page):
    api, request, runtime, scope, _ = page
    policies = importlib.import_module(
        "astrbot_plugin_private_companion.task_proactive_policy"
    )
    runtime.policy.save(scope, "existing", policies.RuleConfig(max_deliveries=7))
    payload = {
        "origin": "tool",
        "session": scope.session_key,
        "sender_id": scope.sender_id,
        "source": "reminder",
        "note": "old",
    }
    cron = SimpleNamespace(payload=payload, cron_expression="0 * * * *", run_once=False)
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(side_effect=[RuntimeError("schedule failure"), cron]),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "task_kind": "monitor",
        "cron_expression": "*/30 * * * *",
        "config": {"max_deliveries": 1},
    }
    assert (await api.update_task_proactive_policy())["status_code"] == 500
    assert runtime.policy.get(scope, "existing")["config"]["max_deliveries"] == 7
    assert runtime.cron_manager.update_job.await_args.kwargs == {
        "cron_expression": "0 * * * *",
        "payload": payload,
    }


@pytest.mark.asyncio
async def test_source_only_failure_removes_new_policy_and_restores_payload(page):
    api, request, runtime, scope, _ = page
    payload = {
        "origin": "tool",
        "session": scope.session_key,
        "sender_id": scope.sender_id,
        "note": "unclassified",
    }
    cron = SimpleNamespace(payload=payload, cron_expression="0 * * * *", run_once=False)
    runtime.cron_manager = SimpleNamespace(
        db=SimpleNamespace(get_cron_job=AsyncMock(return_value=cron)),
        update_job=AsyncMock(side_effect=[RuntimeError("source failure"), cron]),
    )
    request.get_json.return_value = {
        **vars(scope),
        "rule_id": "existing",
        "task_kind": "reminder",
        "config": {},
    }
    assert (await api.update_task_proactive_policy())["status_code"] == 500
    assert runtime.policy.get(scope, "existing") is None
    assert runtime.cron_manager.update_job.await_args.kwargs == {"payload": payload}


def test_tasks_panel_controls_and_explicit_kind_dom_smoke():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    panel = (
        Path(__file__).resolve().parents[1]
        / "pages/companion-panel/js/panels/tasks-panel.js"
    )
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.textContent = ''; this.listeners = {}; }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}
global.document = {createElement: tag => new Element(tag)};
global.window = {};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const root = new Element('root');
const all = () => { const out=[]; function walk(n) {out.push(n); n.children.forEach(walk);} walk(root); return out; };
const find = text => all().find(n => n.textContent === text);
const input = title => find(title).children[0];
const posts=[];
const task = {task_id:'id',goal:'原目标',status:'unknown_result',attempts:1,steps:{}};
window.PrivateCompanionTasksPanel.mount(root, {showToast: message => {if (message.includes('失败')) throw Error(message);}, fetchJson: async (url, options) => {
 if(options) {posts.push(JSON.parse(options.body)); return {success:true,data:{}};}
 if(url.startsWith('/tasks/detail?')) return {success:true,data:{task}};
 if(url.startsWith('/tasks/proactive-policy?')) return {success:true,data:{task_kind:'monitor',cron_expression:'0 * * * *',run_once:false,defaults:{timezone:'Asia/Shanghai',windows:[],min_interval_seconds:0,enabled:true,only_updates:true,stopped:false}}};
 return {success:true,data:{tasks:[task],total:1}};
}});
(async () => {
 input('机器人实例').value='bot'; input('会话标识').value='bot:GroupMessage:g'; input('真实发起者账号').value='sender';
 await find('查询任务').listeners.click(); await find('原目标').listeners.click();
 assert.equal(find('重试'), undefined);
 input('已核实执行进程已停止').checked=true;
 input('核实证据（JSON对象）').value='{"receipt":"real-receipt"}';
 input('核实结果').value='completed';
 await find('提交核实结果').listeners.click();
 assert.equal(posts.at(-1).action,'reconcile'); assert.equal(posts.at(-1).worker_stopped,true);
 input('定时任务编号').value='existing'; await find('读取规则').listeners.click();
 assert.equal(input('任务类型').value,'monitor');
 await find('保存推送规则').listeners.click(); assert.equal(posts.at(-1).task_kind,'monitor');
 task.status='queued'; await find('原目标').listeners.click();
 input('修订目标').value='新目标'; await find('保存目标修订').listeners.click();
 assert.equal(posts.at(-1).action,'revise'); assert.equal(posts.at(-1).goal,'新目标');
 console.log('DOM smoke passed');
})().catch(error => {console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        [node, "-e", script, str(panel)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stdout + result.stderr
