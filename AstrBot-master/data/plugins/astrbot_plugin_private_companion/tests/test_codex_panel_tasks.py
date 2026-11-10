"""Exercise authenticated Codex controls using the real service and fake RPC."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.tests import (
    test_codex_dispatch as dispatch_helpers,
)
from astrbot_plugin_private_companion.tests import test_page_api_tasks as page_helpers
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

runtime = dispatch_helpers.runtime
routed = dispatch_helpers.routed
page = page_helpers.page


@pytest.mark.parametrize("page_name", ["companion-panel", "陪伴面板"])
def test_codex_tasks_dom_paging_text_and_explicit_resume(page_name):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node unavailable")
    panel = (
        Path(__file__).resolve().parents[1]
        / "pages"
        / page_name
        / "js/panels/tasks-panel.js"
    )
    script = r"""
const assert=require('node:assert/strict');
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.value='';this.textContent='';this.listeners={}}
 set innerHTML(value){throw Error('unsafe HTML rendering')}
 append(...n){this.children.push(...n)} prepend(...n){this.children.unshift(...n)}
 replaceChildren(...n){this.children=n} addEventListener(k,f){this.listeners[k]=f}
}
global.document={createElement:tag=>new Element(tag)};global.window={};
eval(require('node:fs').readFileSync(process.argv[1],'utf8'));
const root=new Element('root'),all=()=>{const a=[];function walk(n){a.push(n);n.children.forEach(walk)}walk(root);return a};
const find=(tag,text)=>all().find(n=>n.tag===tag&&n.textContent===text);
const task={task_id:'id',goal:'codex',status:'unknown_result',engine:'codex',codex:{thread_id:'thread-real',model:'gpt-real',reasoning_effort:'high'},execution:{phase_label:'结果待核实'},created_at:1,updated_at:2,steps:{}};
const posts=[],reads=[];
window.PrivateCompanionTasksPanel.mount(root,{showToast:()=>{},fetchJson:async(url,opts)=>{
 if(opts){posts.push(JSON.parse(opts.body));return {success:true,data:{task}}}
 reads.push(url);
 if(url.startsWith('/tasks/detail'))return {success:true,data:{task,codex_events:{items:[{created_at:1,method:'item/completed',payload:{text:url.includes('after=7')?'second':'<img src=x onerror=alert(1)>'}}],next_after:url.includes('after=7')?null:7}}};
 return {success:true,data:{tasks:[task],total:1}};
}});
(async()=>{
 await find('button','查询任务').listeners.click();await find('button','codex').listeners.click();
 assert(all().some(n=>n.textContent.includes('thread-real')&&n.textContent.includes('gpt-real')));
 assert(all().some(n=>n.tag==='pre'&&n.textContent.includes('<img src=x')));
 await find('button','继续读取事件').listeners.click();
 assert(reads.some(u=>u.includes('after=7')));assert(all().some(n=>n.textContent.includes('second')));
 await find('button','核实原进度并继续').listeners.click();
 assert.equal(posts.at(-1).action,'resume');
 assert.equal(find('button','保存目标修订'),undefined);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, str(panel)],
        capture_output=True,
        text=True,
        encoding="utf8",
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_codex_page_factual_detail_and_real_event_pagination(page, routed):
    api, request, _, _, _ = page
    rt, calls, _ = routed
    api.plugin._persistent_task_runtime = rt
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    request.args = vars(scope).copy()
    try:
        created = await rt.submit(e, {"goal": "Codex page"})
        for index in range(3):
            await calls[0].emit(
                "item/completed",
                {
                    "threadId": created["thread_id"],
                    "turnId": created["turn_id"],
                    "item": {
                        "id": str(index),
                        "type": "commandExecution",
                        "exitCode": 0,
                        "aggregatedOutput": "<img src=x onerror=alert(1)> data:image/png;base64,AAA",
                    },
                },
            )
        task = (await api.get_tasks())["data"]["tasks"][0]
        assert (
            task["engine"] == "codex"
            and task["codex"]["thread_id"] == created["thread_id"]
        )
        assert task["codex"]["reasoning_effort"] == "medium"
        assert task["created_at"] and task["updated_at"] and task["execution"]["phase"]
        request.args.update(task_id=created["task_id"], limit="1", after="0")
        first = (await api.get_task_detail())["data"]["codex_events"]
        assert len(first["items"]) == 1 and first["next_after"]
        request.args["after"] = str(first["next_after"])
        second = (await api.get_task_detail())["data"]["codex_events"]
        assert second["items"][0]["id"] > first["items"][0]["id"]
        assert "base64,AAA" not in repr(first) + repr(second)
        request.args["sender_id"] = "foreign"
        assert (await api.get_task_detail())["status_code"] == 404
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["pause", "cancel", "resume", "retry"])
async def test_codex_page_controls_use_host_lifecycle(page, routed, action):
    api, request, _, _, _ = page
    rt, calls, _ = routed
    api.plugin._persistent_task_runtime = rt
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    try:
        created = await rt.submit(e, {"goal": "Codex page control"})
        if action in {"resume", "retry"}:
            await rt.codex.close()
            rt.codex._closed = False
            assert rt.service.get(created["task_id"], scope).status == "unknown_result"
        original = rt.control_codex_task
        rt.control_codex_task = AsyncMock(wraps=original)
        request.get_json.return_value = {
            **vars(scope),
            "task_id": created["task_id"],
            "action": action,
        }
        result = await api.control_task()
        assert result["success"], result
        assert rt.control_codex_task.await_count == 1
        passed_job = rt.control_codex_task.call_args.args[0]
        assert passed_job.scope == scope
        if action in {"pause", "cancel"}:
            assert any(m == "turn/interrupt" for m, _ in calls[0].calls)
            assert result["data"]["task"]["status"] == "running"
        else:
            assert any(m == "thread/resume" for m, _ in calls[-1].calls)
            assert result["data"]["task"]["status"] == "running"
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,status", [("pause", "paused"), ("cancel", "cancelled")]
)
async def test_codex_page_unbound_failure_closes_without_start(
    page, routed, action, status
):
    api, request, _, _, _ = page
    rt, calls, providers = routed
    api.plugin._persistent_task_runtime = rt
    providers["catalog"].provider_config.pop("api_base")
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    try:
        created = await rt.submit(e, {"goal": "unbound failure"})
        assert created["status"] == "unknown_result"
        request.get_json.return_value = {
            **vars(scope),
            "task_id": created["task_id"],
            "action": action,
        }
        result = await api.control_task()
        assert result["success"] and result["data"]["task"]["status"] == status
        assert not calls
    finally:
        await rt.codex.close()
