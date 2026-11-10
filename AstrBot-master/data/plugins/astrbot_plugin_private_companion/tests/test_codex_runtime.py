import asyncio
import hashlib
import importlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from task_service import TaskScope, TaskService


class Rpc:
    def __init__(self, index):
        self.index = index
        self.calls = []
        self.closed = False
        self.turn = None

    async def start(self):
        pass

    async def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": f"thread-{self.index}"}}
        if method == "thread/read":
            return {"thread": {"turns": [{"id": self.turn, "status": "completed"}]}}
        if method == "turn/start":
            self.turn = f"turn-{len(self.calls)}"
            return {"turn": {"id": self.turn, "status": "inProgress"}}
        return {}

    async def emit(self, method, params):
        await self.on_notification(method, params)

    async def close(self):
        self.closed = True


class Gateway:
    def __init__(self):
        self.grants = []
        self.revoked = []

    async def start(self):
        return "http://127.0.0.1:12345"

    def issue(self, **kwargs):
        self.grants.append(kwargs)
        return f"temporary-{len(self.grants)}"

    def revoke(self, token):
        self.revoked.append(token)

    async def close(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("error_info", [
    {"responseStreamDisconnected": {"httpStatusCode": 502}},
    {"responseTooManyFailedAttempts": {"httpStatusCode": 429}},
])
async def test_provider_turn_failure_preserves_current_cause_not_old_command(env, error_info):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        await rpc.emit("item/completed", {
            "threadId": created["thread_id"], "turnId": created["turn_id"],
            "item": {"id": "old", "type": "commandExecution", "exitCode": 1,
                     "aggregatedOutput": "older command syntax error"},
        })
        assert service.get(job.task_id, scope).status == "running"
        error = {"message": "network failed fake-private-key", "codexErrorInfo": error_info}
        await rpc.emit("turn/completed", {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "failed", "error": error},
        })
        failed = service.get(job.task_id, scope)
        assert failed.error_kind == "provider_unavailable"
        cause = failed.metadata["codex_terminal_error"]
        assert cause["turn_id"] == created["turn_id"]
        assert cause["error"]["codexErrorInfo"] == error_info
        assert "network failed" in cause["error"]["message"]
        assert "fake-private-key" not in json.dumps(cause)
        assert failed.metadata["execution_state"]["phase"] == "model_error"
        assert rpc.closed and not runtime.active
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_stop_interrupt_timeout_is_controlled_stop_not_execution_failure(env, cancel):
    runtime, _, service, scope, task, calls, gateway, _ = env
    job = task()
    try:
        await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        original = rpc.request

        async def request(method, params=None):
            if method == "turn/interrupt":
                raise TimeoutError("interrupt acknowledgement lost")
            return await original(method, params)

        rpc.request = request
        stopped = await runtime.stop(scope, task_id=job.task_id, cancel=cancel)
        assert stopped["status"] == ("cancelled" if cancel else "paused")
        assert service.get(job.task_id, scope).error_kind is None
        assert rpc.closed and gateway.revoked and not runtime.active
        with service._transaction() as db:
            assert not db.execute("SELECT 1 FROM pt_outbox WHERE kind='result' AND json_extract(payload,'$.error_kind') IS NOT NULL").fetchall()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_user_stop_does_not_emit_late_model_retry_notice(env):
    runtime, _, service, scope, task, _, gateway, _ = env
    job = task()
    try:
        await runtime.create(job, model_id="catalog")
        await runtime.stop(scope, task_id=job.task_id)
        await gateway.grants[0]["on_event"]({"type": "request_failed", "attempt": 1})
        with service._transaction() as db:
            assert not db.execute("SELECT 1 FROM pt_outbox WHERE kind='model_retry'").fetchall()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_explicit_resume_clears_old_terminal_cause(env):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        await calls[0][0].emit("turn/completed", {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "failed",
                     "error": {"message": "old failure", "codexErrorInfo": {"responseTooManyFailedAttempts": {"httpStatusCode": 429}}}},
        })
        assert "codex_terminal_error" in service.get(job.task_id, scope).metadata
        await runtime.resume(scope, task_id=job.task_id, text="用户明确要求继续")
        assert service.get(job.task_id, scope).status == "running"
        assert "codex_terminal_error" not in service.get(job.task_id, scope).metadata
    finally:
        await runtime.close()


@pytest.fixture
def env(tmp_path):
    service = TaskService(tmp_path / "tasks.db")
    scope = TaskScope("bot", "session", "user")
    provider = SimpleNamespace(
        provider_config={
            "enable": True,
            "model": "gpt-6-sol",
            "api_base": "https://fake.invalid/v1",
            "custom_headers": {"X-Test": "host"},
            "proxy": "http://127.0.0.1:80",
        },
        get_model=lambda: "gpt-6-sol",
        get_current_key=lambda: "fake-private-key",
    )
    calls = []
    host = SimpleNamespace(
        service=service,
        plugin=SimpleNamespace(
            context=SimpleNamespace(get_provider_by_id=lambda _: provider)
        ),
        authorize_job=lambda _: True,
        restore_event=lambda job: job,
        actor=lambda _: "actor",
        workspace_prompt=lambda _: "workspace",
        _safe_text=lambda text, limit=16000: str(text)[:limit],
    )

    async def ensure(job):
        return None

    host.ensure_workspace = ensure

    async def connect(**kwargs):
        rpc = Rpc(len(calls))
        calls.append((rpc, kwargs))
        return rpc

    module = importlib.import_module("codex_runtime")
    gateway = Gateway()
    runtime = module.CodexRuntime(host, connect_factory=connect, gateway=gateway)

    def task(fragment="first", **metadata):
        return service.submit(
            scope,
            "m",
            fragment,
            "goal",
            tmp_path,
            metadata={"engine": "codex", **metadata},
        )

    return runtime, host, service, scope, task, calls, gateway, provider


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["succeeded", "waiting_user", None])
async def test_final_media_is_projected_before_public_evidence(env, declared):
    runtime, _, service, scope, task, calls, _, _ = env
    image = "data:image/png;base64,QUJDREVGRw=="
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        text = "ordinary result " + image
        if declared:
            text = json.dumps({"task_status": declared, "text": text})
        await calls[0][0].emit(
            "item/completed",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "item": {"id": "final", "type": "agentMessage", "text": text},
            },
        )
        assert image not in runtime.active[job.task_id]["final"]
        assert "ordinary result" in runtime.active[job.task_id]["final"]
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "completed"},
            },
        )
        persisted = service.get(job.task_id, scope)
        assert persisted.status == (declared or "unknown_result")
        assert image not in json.dumps(persisted.result)
        assert image not in json.dumps(runtime.read(scope, task_id=job.task_id))
        if declared:
            assert "ordinary result" in persisted.result["text"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["read", "create"])
async def test_existing_result_media_projected_by_all_public_receipts(env, access):
    runtime, _, service, scope, task, calls, _, _ = env
    image = "data:image/png;base64,TEVHQUNZ"
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        await runtime.stop(scope, task_id=job.task_id)
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "interrupted"},
            },
        )
        legacy = {
            "request": {"api_key": "ordinary example", "image": image},
            "headers": ["column A"],
            "access_token": "ordinary example token",
            "nested_json": json.dumps({"image": image, "text": "keep business"}),
        }
        with service._transaction() as db:
            db.execute(
                "UPDATE pt_tasks SET status='waiting_user',result=? WHERE task_id=?",
                (json.dumps(legacy), job.task_id),
            )
        if access == "read":
            public = runtime.read(scope, task_id=job.task_id)
        elif access == "create":
            public = await runtime.create(job, model_id="catalog")
        assert image not in json.dumps(public)
        assert public["result"]["request"]["api_key"] == "ordinary example"
        assert public["result"]["headers"] == ["column A"]
        assert public["result"]["access_token"] == "ordinary example token"
        assert json.loads(public["result"]["nested_json"])["text"] == "keep business"
        assert service.get(job.task_id, scope).result == legacy
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["resume", "recover"])
async def test_resumed_final_media_is_projected_in_immediate_receipt(env, access):
    runtime, _, service, scope, task, calls, _, _ = env
    image = "data:image/png;base64,UkVTVU1F"
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        await runtime.stop(scope, task_id=job.task_id)
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "interrupted"},
            },
        )
        if access == "recover":
            with service._transaction() as db:
                db.execute(
                    "UPDATE pt_tasks SET status='waiting_user' WHERE task_id=?",
                    (job.task_id,),
                )
        original = runtime._turn

        async def complete_during_turn(state, inputs):
            await original(state, inputs)
            binding = runtime.read(scope, task_id=job.task_id)
            rpc = calls[-1][0]
            await rpc.emit(
                "item/completed",
                {
                    "threadId": binding["thread_id"],
                    "turnId": binding["turn_id"],
                    "item": {
                        "id": "final",
                        "type": "agentMessage",
                        "text": json.dumps(
                            {
                                "task_status": "succeeded",
                                "text": "resume evidence " + image,
                            }
                        ),
                    },
                },
            )
            await rpc.emit(
                "turn/completed",
                {
                    "threadId": binding["thread_id"],
                    "turn": {"id": binding["turn_id"], "status": "completed"},
                },
            )

        runtime._turn = complete_during_turn
        if access == "resume":
            public = await runtime.resume(scope, task_id=job.task_id)
        else:
            public = await runtime.recover(
                scope,
                task_id=job.task_id,
                expected_revision=service.get(job.task_id, scope).revision,
                guidance="verify evidence",
            )
        assert public["status"] == "succeeded"
        assert "resume evidence" in public["result"]["text"]
        assert image not in json.dumps(public)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_media_reaches_model_but_is_omitted_from_public_receipt(env):
    runtime, host, _, scope, task, calls, _, _ = env
    image = "data:image/png;base64,VE9PTA=="
    host.resolve_tools = lambda *_: {"business": SimpleNamespace(name="business")}
    executions = []

    async def execute(*_):
        executions.append(1)
        return {"request": {"image": image}, "api_key": "ordinary example"}

    host.execute_tool = execute
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        request = {
            "threadId": created["thread_id"],
            "turnId": created["turn_id"],
            "callId": "business-image",
            "tool": "astrbot_tool",
            "arguments": {"name": "business", "arguments": {}},
        }
        receipt = await calls[0][0].on_request("item/tool/call", request)
        assert receipt == await calls[0][0].on_request("item/tool/call", request)
        assert executions == [1]
        model_data = json.loads(receipt["contentItems"][0]["text"])
        assert model_data["request"]["image"] == image
        assert model_data["api_key"] == "ordinary example"
        public = runtime.read(scope, task_id=job.task_id)
        assert image not in json.dumps(public)
        assert "ordinary example" in json.dumps(public)
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "generation", "closed"])
async def test_resume_connection_state_is_successful_generation_fact(env, outcome):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    closers = []
    try:
        created = await runtime.create(job, model_id="catalog")
        await runtime.stop(scope, task_id=job.task_id)
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "interrupted"},
            },
        )
        original = runtime.connect_factory

        async def connect(**kwargs):
            rpc = await original(**kwargs)
            request = rpc.request

            async def resume(method, params=None):
                if method == "thread/resume":
                    if outcome == "failure":
                        raise ConnectionError("synthetic resume failure")
                    if outcome == "generation":
                        runtime.store.next_generation(scope, job.task_id)
                    if outcome == "closed":
                        closers.append(asyncio.create_task(runtime.close()))
                        await asyncio.sleep(0)
                return await request(method, params)

            rpc.request = resume
            return rpc

        runtime.connect_factory = connect
        if outcome == "success":
            result = await runtime.resume(scope, task_id=job.task_id)
            assert (
                result["status"] == "running"
                and result["connection_state"] == "connected"
            )
            assert (
                service.get(job.task_id, scope).metadata["codex"]["connection_state"]
                == "connected"
            )
        else:
            with pytest.raises((RuntimeError, ConnectionError)):
                await runtime.resume(scope, task_id=job.task_id)
            state = runtime.store.get(scope, task_id=job.task_id)["connection_state"]
            assert state == (
                "connecting" if outcome == "generation" else "disconnected"
            )
    finally:
        await asyncio.gather(*closers)
        await runtime.close()


@pytest.mark.asyncio
async def test_protocol_input_and_nested_turn_reasoning_never_public(env):
    runtime, _, _, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        await calls[0][0].emit(
            "item/completed",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "item": {
                    "id": "user",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "full private input"}],
                },
            },
        )
        await runtime.stop(scope, task_id=job.task_id)
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {
                    "id": created["turn_id"],
                    "status": "interrupted",
                    "items": [
                        {
                            "id": "r",
                            "type": "reasoning",
                            "summary": ["private thinking"],
                            "content": ["private thought"],
                        },
                        {
                            "id": "cmd",
                            "type": "commandExecution",
                            "aggregatedOutput": "file checked",
                        },
                    ],
                },
            },
        )
        public = runtime.read(scope, task_id=job.task_id)
        assert "private" not in json.dumps(public)
        assert "file checked" in json.dumps(public)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_business_tool_result_keeps_fields_and_exact_secret_redaction(env):
    runtime, host, _, scope, task, calls, _, _ = env
    host.resolve_tools = lambda *_: {"business": SimpleNamespace(name="business")}
    original = {
        "status": "success",
        "request": {"product": "ordinary request"},
        "headers": ["column A"],
        "api_key": "example key",
        "access_token": "example token",
        "typed": {"type": "userMessage", "content": "ordinary training sample"},
        "echo": "fake-private-key temporary-1",
    }
    executions = []

    async def execute(*_):
        executions.append(1)
        return original

    host.execute_tool = execute
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        request = {
            "threadId": created["thread_id"],
            "turnId": created["turn_id"],
            "callId": "business",
            "tool": "astrbot_tool",
            "arguments": {"name": "business", "arguments": {}},
        }
        receipt = await calls[0][0].on_request("item/tool/call", request)
        assert receipt == await calls[0][0].on_request("item/tool/call", request)
        assert len(executions) == 1
        result = json.loads(receipt["contentItems"][0]["text"])
        assert all(
            result[key] == value for key, value in original.items() if key != "echo"
        )
        assert (
            "fake-private-key" not in result["echo"]
            and "temporary-1" not in result["echo"]
        )
        events = runtime.read(scope, task_id=job.task_id)["events"]["items"]
        public = next(
            event["payload"]["receipt"]
            for event in events
            if event["method"] == "host_tool/completed"
        )
        assert public == result
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_public_usage_notification_keeps_reported_counts_only(env):
    runtime, _, _, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        usage = {
            "last": {
                "cachedInputTokens": 7,
                "cacheWriteInputTokens": 3,
                "inputTokens": 20,
                "outputTokens": 8,
                "reasoningOutputTokens": 4,
                "totalTokens": 28,
            },
            "total": {"inputTokens": 20},
            "modelContextWindow": None,
            "request": "private input",
        }
        await calls[0][0].emit(
            "thread/tokenUsage/updated",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "tokenUsage": usage,
                "input": "private input",
            },
        )
        events = runtime.read(scope, task_id=job.task_id)["events"]["items"]
        event = next(e for e in events if e["method"] == "thread/tokenUsage/updated")
        public = event["payload"]["tokenUsage"]
        assert public["last"] == usage["last"]
        assert public["total"] == {"inputTokens": 20}
        assert public["modelContextWindow"] is None
        assert "private input" not in repr(event)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_related_parent_and_persistent_guidance_are_actual_scoped_facts(env):
    runtime, _, service, scope, task, calls, _, _ = env
    parent = task("parent", request={"system_prompt": "private old prompt"})
    claimed = service.claim_external(parent.task_id, scope)
    service.finish(claimed, {"text": "old completion evidence"})
    job = task(
        "child",
        parent_task_id=parent.task_id,
        task_relation="revise",
        request={
            "recovery_guidance": "核对后只修正第二处",
            "system_prompt": "private old prompt",
        },
    )
    try:
        await runtime.create(job, model_id="catalog")
        inputs = next(p["input"] for m, p in calls[0][0].calls if m == "turn/start")
        assert job.task_id in repr(inputs) and parent.task_id in repr(inputs)
        assert "revise" in repr(inputs) and "old completion evidence" in repr(inputs)
        assert "核对后只修正第二处" in repr(inputs)
        assert "private old prompt" not in repr(inputs)
        await runtime.stop(scope, task_id=job.task_id)
        created = runtime.read(scope, task_id=job.task_id)
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "interrupted"},
            },
        )
        await runtime.resume(scope, task_id=job.task_id)
        resumed = next(p["input"] for m, p in calls[1][0].calls if m == "turn/start")
        assert "核对后只修正第二处" in repr(resumed)
        assert "不重复" in repr(resumed)
        instructions = next(
            p["baseInstructions"] for m, p in calls[0][0].calls if m == "thread/start"
        )
        assert "子进程" in instructions and "持久任务" in instructions
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_create_idempotent_independent_scoped_and_secret_free(env):
    runtime, _, service, scope, task, calls, gateway, _ = env
    a, b = task(), task("second")
    try:
        first = await runtime.create(a, model_id="catalog")
        again = await runtime.create(a, model_id="catalog")
        second = await runtime.create(b, model_id="catalog")
        assert first["thread_id"] == again["thread_id"] != second["thread_id"]
        assert len(calls) == 2
        assert len([x for x in calls[0][0].calls if x[0] == "turn/start"]) == 1
        assert first["reasoning_effort"] == "medium"
        assert runtime.list(scope)["total"] == 2
        with pytest.raises(LookupError):
            runtime.read(replace(scope, sender_id="other"), task_id=a.task_id)
        assert "fake-private-key" not in repr(calls)
        assert "fake-private-key" not in repr(runtime.read(scope, task_id=a.task_id))
        assert gateway.grants[0]["headers"] == {"X-Test": "host"}
        assert service.get(a.task_id, scope).status == "running"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_first_input_real_current_images_only(env):
    runtime, _, _, _, task, calls, _, _ = env
    job = task(
        request={
            "current_input": {
                "content": [
                    {"type": "text", "text": "actual"},
                    {"type": "image_url", "image_url": {"url": "pc-task-image://a"}},
                ]
            },
            "media": {"pc-task-image://a": "data:image/png;base64,AAA"},
            "contexts": [{"role": "user", "content": "old"}],
        }
    )
    try:
        await runtime.create(job, model_id="catalog")
        inputs = next(p["input"] for m, p in calls[0][0].calls if m == "turn/start")
        assert {"type": "image", "url": "data:image/png;base64,AAA"} in inputs
        assert "actual" in repr(inputs) and "old" not in repr(inputs)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_actual_model_rejects_misleading_catalog(env):
    runtime, _, _, _, task, calls, _, provider = env
    provider.get_model = lambda: "gemini-3"
    assert (await runtime.create(task(), model_id="gpt-fake"))[
        "error"
    ] == "unsupported_model"
    assert not calls
    await runtime.close()


@pytest.mark.asyncio
async def test_tools_deduplicated_errors_continue_and_completion_releases(env):
    runtime, host, service, scope, task, calls, gateway, _ = env
    executions = []
    host.resolve_tools = lambda job, names: {
        "media": SimpleNamespace(name="media", description="test", parameters={})
    }

    async def execute(job, tool, args):
        executions.append(args)
        return {
            "status": "error",
            "content": "exit 1 token budget password business fake-private-key",
        }

    host.execute_tool = execute
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        params = {
            "threadId": created["thread_id"],
            "turnId": created["turn_id"],
            "callId": "call1",
            "tool": "astrbot_tool",
            "arguments": {"name": "media", "arguments": {"x": 1}},
        }
        receipt = await rpc.on_request("item/tool/call", params)
        assert receipt == await rpc.on_request("item/tool/call", params)
        assert len(executions) == 1
        assert "token budget password business" in repr(receipt)
        assert "fake-private-key" not in repr(receipt)
        assert service.get(job.task_id, scope).status == "running"
        with pytest.raises(PermissionError):
            await rpc.on_request("item/tool/call", {**params, "threadId": "foreign"})
        await rpc.emit(
            "item/completed",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "item": {
                    "type": "agentMessage",
                    "id": "msg",
                    "text": json.dumps({"task_status": "succeeded", "text": "done"}),
                },
            },
        )
        await rpc.emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "completed"},
            },
        )
        assert service.get(job.task_id, scope).status == "succeeded"
        assert rpc.closed and gateway.revoked
        assert not runtime.active
        assert "done" in repr(runtime.read(scope, task_id=job.task_id))
        assert service.get(job.task_id, scope).result["receipts"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_native_tool_nonzero_not_task_failure_and_reasoning_private(env):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        params = {
            "threadId": created["thread_id"],
            "turnId": created["turn_id"],
            "item": {
                "id": "cmd",
                "type": "commandExecution",
                "exitCode": 1,
                "aggregatedOutput": "failure",
            },
        }
        await rpc.emit("item/started", params)
        assert (
            service.get(job.task_id, scope).metadata["execution_state"]["phase"]
            == "tool_running"
        )
        await rpc.emit("item/completed", params)
        assert service.get(job.task_id, scope).status == "running"
        await rpc.emit(
            "item/completed",
            {**params, "item": {"type": "reasoning", "text": "PRIVATE_REASONING"}},
        )
        assert "PRIVATE_REASONING" not in repr(runtime.read(scope, task_id=job.task_id))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_guide_effort_next_turn_stop_and_explicit_resume(env):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        changed = await runtime.guide(
            scope, task_id=job.task_id, text="adjust", reasoning_effort="high"
        )
        assert changed["applies_to"] == "next_turn"
        assert rpc.calls[-1] == (
            "turn/steer",
            {
                "threadId": created["thread_id"],
                "expectedTurnId": created["turn_id"],
                "input": [{"type": "text", "text": "adjust"}],
            },
        )
        await runtime.stop(scope, task_id=job.task_id)
        assert service.get(job.task_id, scope).status == "running"
        await rpc.emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "interrupted"},
            },
        )
        assert service.get(job.task_id, scope).status == "paused"
        assert service.get(job.task_id, scope).lease_token is None
        resumed = await runtime.resume(scope, task_id=job.task_id)
        assert resumed["thread_id"] == created["thread_id"]
        assert any(
            m == "thread/resume" and p["threadId"] == created["thread_id"]
            for m, p in calls[1][0].calls
        )
        turn = next(p for m, p in calls[1][0].calls if m == "turn/start")
        assert turn["effort"] == "high"
        assert "goal" not in repr(turn["input"])
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_steer_race_reads_actual_before_new_turn(env):
    from codex_rpc import CodexRpcError

    runtime, _, _, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        original = rpc.request

        async def request(method, params=None):
            if method == "turn/steer":
                rpc.calls.append((method, params))
                raise CodexRpcError(
                    {"code": -32000, "message": "expected turn mismatch"}
                )
            return await original(method, params)

        rpc.request = request
        await runtime.guide(scope, task_id=job.task_id, text="continue")
        assert sum(m == "turn/steer" for m, p in rpc.calls) == 1
        assert any(m == "thread/read" for m, p in rpc.calls)
        assert calls[1][0].calls[-1][0] == "turn/start"
        assert (
            runtime.read(scope, task_id=job.task_id)["thread_id"]
            == created["thread_id"]
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stale_generation_heartbeat_and_close_unknown(env):
    runtime, _, service, scope, task, calls, gateway, _ = env
    job = task()
    created = await runtime.create(job, model_id="catalog")
    old_state = runtime.active[job.task_id]
    before = service.get(job.task_id, scope).metadata.get("execution_state")
    assert service.heartbeat(old_state["job"])
    assert service.get(job.task_id, scope).metadata.get("execution_state") == before
    await runtime.close()
    assert service.get(job.task_id, scope).status == "unknown_result"
    assert service.get(job.task_id, scope).lease_token is None
    assert calls[0][0].closed and gateway.revoked
    await runtime._notification(
        old_state,
        "turn/completed",
        {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "completed"},
        },
    )
    assert service.get(job.task_id, scope).status == "unknown_result"


@pytest.mark.asyncio
@pytest.mark.parametrize("response_lost", [False, True])
async def test_terminal_races_turn_start_response(env, response_lost):
    runtime, _, service, scope, task, calls, _, _ = env
    factory = runtime.connect_factory

    async def connect(**kwargs):
        rpc = await factory(**kwargs)
        original = rpc.request

        async def request(method, params=None):
            result = await original(method, params)
            if method == "turn/start":
                await rpc.emit(
                    "turn/started", {"threadId": "thread-0", "turn": result["turn"]}
                )
                await rpc.emit(
                    "item/completed",
                    {
                        "threadId": "thread-0",
                        "turnId": result["turn"]["id"],
                        "item": {
                            "type": "agentMessage",
                            "text": json.dumps(
                                {"task_status": "succeeded", "text": "done"}
                            ),
                        },
                    },
                )
                await rpc.emit(
                    "turn/completed",
                    {
                        "threadId": "thread-0",
                        "turn": {"id": result["turn"]["id"], "status": "completed"},
                    },
                )
                if response_lost:
                    raise ConnectionError("terminal closed pending response")
            return result

        rpc.request = request
        return rpc

    runtime.connect_factory = connect
    job = task()
    created = await runtime.create(job, model_id="catalog")
    assert created["turn_id"] is None
    assert service.get(job.task_id, scope).status == "succeeded"
    assert calls[0][0].closed
    await runtime.close()


@pytest.mark.asyncio
async def test_cross_user_controls_and_model_changes_isolated(env):
    runtime, _, _, scope, task, calls, _, _ = env
    a, b = task(), task("other")
    try:
        await runtime.create(a, model_id="catalog")
        await runtime.create(b, model_id="catalog")
        for method, kw in [
            (runtime.guide, {"text": "evil"}),
            (runtime.stop, {}),
            (runtime.resume, {}),
        ]:
            with pytest.raises(LookupError):
                await method(replace(scope, sender_id="other"), task_id=a.task_id, **kw)
        await runtime.guide(scope, task_id=a.task_id, text="", reasoning_effort="low")
        assert runtime.read(scope, task_id=a.task_id)["reasoning_effort"] == "low"
        assert runtime.read(scope, task_id=b.task_id)["reasoning_effort"] == "medium"
        assert calls[0][0].calls[-1][0] == "turn/start"
    finally:
        await runtime.close()


def test_dynamic_tools_follow_fixed_01670_discriminator(env):
    runtime = env[0]
    assert all(tool.get("type") == "function" for tool in runtime._dynamic_tools())


@pytest.mark.asyncio
async def test_gateway_retry_once_and_no_heartbeat_progress(env):
    runtime, _, service, scope, task, _, gateway, _ = env
    job = task()
    try:
        await runtime.create(job, model_id="catalog")
        observer = gateway.grants[0]["on_event"]
        await observer(
            {
                "type": "request_started",
                "model": "gpt-6-sol",
                "attempt": 1,
                "elapsed": 0,
            }
        )
        await observer(
            {"type": "request_failed", "model": "gpt-6-sol", "attempt": 1, "elapsed": 1}
        )
        await observer(
            {"type": "request_failed", "model": "gpt-6-sol", "attempt": 2, "elapsed": 2}
        )
        with service._transaction() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM pt_outbox WHERE kind='model_retry'"
                ).fetchone()[0]
                == 1
            )
        assert service.get(job.task_id, scope).attempts == 1
        assert (
            "last_progress_at"
            not in service.get(job.task_id, scope).metadata["execution_state"]
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_launch_environment_and_cancelled_start_reaps_handle(
    env, tmp_path, monkeypatch
):
    runtime, host, _, _, task, _, _, _ = env
    module = importlib.import_module("codex_runtime")
    install = tmp_path / "installation"
    install.mkdir()
    exe = install / "codex.exe"
    exe.write_bytes(b"fake binary")
    (install / "installation.json").write_text(
        json.dumps(
            {
                "executable": str(exe),
                "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(
        module,
        "Path",
        lambda value: (
            install if str(value) == "C:/ProgramData/AstrBotCodex" else Path(value)
        ),
    )
    started, release = threading.Event(), threading.Event()
    seen = []

    class Channel:
        killed = False
        closed = False

        def kill_tree(self):
            self.killed = True

        async def close(self):
            self.closed = True

    channel = Channel()

    def launch(identity, argv, root, environment):
        seen.append((identity, argv, root, environment))
        started.set()
        release.wait(3)
        return channel

    host.get_execution_broker = lambda actor: SimpleNamespace(
        verifier=SimpleNamespace(verify=lambda actor, root: "verified-ordinary"),
        backend=SimpleNamespace(start_duplex=launch),
    )
    pending = asyncio.create_task(
        runtime._connect(
            task(), "actor", "gpt-6-sol", "temporary", "http://127.0.0.1:12345"
        )
    )
    await asyncio.to_thread(started.wait, 3)
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert channel.killed and channel.closed
    assert seen[0][0] == "verified-ordinary"
    assert "fake-private-key" not in repr(seen)
    assert seen[0][1][1:4] == ["app-server", "--listen", "stdio://"]
    child_env = seen[0][3]
    assert child_env["CODEX_HOME"].endswith(".codex")
    assert "PowerShell" in child_env["PATH"] and ".runtime" in child_env["PATH"]
    assert set(child_env) == {
        "SystemRoot",
        "WINDIR",
        "PATH",
        "USERPROFILE",
        "TEMP",
        "TMP",
        "CODEX_HOME",
        "ASTRBOT_CODEX_TOKEN",
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_task_model_policy_limits_gateway(env):
    runtime, _, _, _, task, _, gateway, _ = env
    try:
        await runtime.create(
            task(request={"request_timeout_seconds": 17, "request_max_attempts": 2}),
            model_id="catalog",
        )
        assert gateway.grants[0]["idle_timeout"] == 17
        assert gateway.grants[0]["max_attempts"] == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_stop_keeps_inflight_receipt_then_cancels_and_old_generation_ignored(env):
    runtime, host, service, scope, task, calls, _, _ = env
    entered, finish = asyncio.Event(), asyncio.Event()
    host.resolve_tools = lambda job, names: {"media": SimpleNamespace(name="media")}

    async def execute(job, tool, args):
        entered.set()
        await finish.wait()
        return {"status": "success", "content": "actual receipt"}

    host.execute_tool = execute
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        pending = asyncio.create_task(
            rpc.on_request(
                "item/tool/call",
                {
                    "threadId": created["thread_id"],
                    "turnId": created["turn_id"],
                    "callId": "inflight",
                    "tool": "astrbot_tool",
                    "arguments": {"name": "media", "arguments": {}},
                },
            )
        )
        await entered.wait()
        await runtime.stop(scope, task_id=job.task_id, cancel=True)
        terminal = asyncio.create_task(
            rpc.emit(
                "turn/completed",
                {
                    "threadId": created["thread_id"],
                    "turn": {"id": created["turn_id"], "status": "interrupted"},
                },
            )
        )
        await asyncio.sleep(0)
        assert not terminal.done()
        assert service.get(job.task_id, scope).status == "running"
        finish.set()
        assert "actual receipt" in repr(await pending)
        await terminal
        assert service.get(job.task_id, scope).status == "cancelled"
        assert "actual receipt" in repr(runtime.read(scope, task_id=job.task_id))
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_new_generation_ignores_delayed_old_terminal(env):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        first = await runtime.create(job, model_id="catalog")
        old = runtime.active[job.task_id]
        await calls[0][0].emit(
            "turn/completed",
            {
                "threadId": first["thread_id"],
                "turn": {"id": first["turn_id"], "status": "completed"},
            },
        )
        second = await runtime.resume(scope, task_id=job.task_id, text="next")
        await runtime._notification(
            old,
            "turn/completed",
            {
                "threadId": first["thread_id"],
                "turn": {"id": first["turn_id"], "status": "failed"},
            },
        )
        assert service.get(job.task_id, scope).status == "running"
        assert runtime.read(scope, task_id=job.task_id)["turn_id"] == second["turn_id"]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_rpc_disconnect_stops_job_unknown(env):
    runtime, _, service, scope, task, calls, gateway, _ = env
    job = task()
    await runtime.create(job, model_id="catalog")
    calls[0][0]._closed = True
    for _ in range(60):
        if not runtime.active:
            break
        await asyncio.sleep(0.1)
    assert not runtime.active
    assert service.get(job.task_id, scope).status == "unknown_result"
    assert gateway.revoked and calls[0][0].closed
    await runtime.close()


@pytest.mark.asyncio
async def test_close_during_connect_waits_then_reaps_without_starting_turn(env):
    runtime, _, service, scope, task, calls, gateway, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    original = runtime.connect_factory

    async def connect(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    runtime.connect_factory = connect
    job = task()
    creating = asyncio.create_task(runtime.create(job, model_id="catalog"))
    await entered.wait()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError):
        await creating
    await closing
    assert calls[0][0].closed and gateway.revoked
    assert not any(method == "turn/start" for method, params in calls[0][0].calls)
    assert service.get(job.task_id, scope).status == "unknown_result"


@pytest.mark.asyncio
async def test_model_waiting_user_completed_turn_is_not_task_success(env):
    runtime, _, service, scope, task, calls, _, _ = env
    job = task()
    try:
        created = await runtime.create(job, model_id="catalog")
        rpc = calls[0][0]
        turn_input = next(p for method, p in rpc.calls if method == "turn/start")
        assert "task_status" in turn_input["outputSchema"]["required"]
        await rpc.emit(
            "item/completed",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps(
                        {"task_status": "waiting_user", "text": "请补充实际测量值"}
                    ),
                },
            },
        )
        await rpc.emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "completed"},
            },
        )
        current = service.get(job.task_id, scope)
        assert current.status == "waiting_user"
        assert current.result["text"] == "请补充实际测量值"
        assert rpc.closed and current.lease_token is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_close_waits_for_terminal_cleanup_already_in_progress(env):
    runtime, _, _, _, task, calls, _, _ = env
    created = await runtime.create(task(), model_id="catalog")
    rpc = calls[0][0]
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        rpc.closed = True

    rpc.close = close
    terminal = asyncio.create_task(
        rpc.emit(
            "turn/completed",
            {
                "threadId": created["thread_id"],
                "turn": {"id": created["turn_id"], "status": "completed"},
            },
        )
    )
    await entered.wait()
    closing = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await terminal
    await closing
    assert rpc.closed


@pytest.mark.asyncio
async def test_expired_lease_close_still_marks_unknown_and_releases_exact_token(env):
    runtime, _, service, scope, task, _, _, _ = env
    now = [service.clock()]
    service.clock = lambda: now[0]
    job = task()
    await runtime.create(job, model_id="catalog")
    now[0] += service.lease_seconds + 1
    await runtime.close()
    current = service.get(job.task_id, scope)
    assert current.status == "unknown_result" and current.lease_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared", ["succeeded", "waiting_user", "failed", "paused", "stop"]
)
async def test_expired_lease_terminal_falls_back_to_unknown(env, declared):
    runtime, _, service, scope, task, calls, _, _ = env
    now = [service.clock()]
    service.clock = lambda: now[0]
    job = task()
    created = await runtime.create(job, model_id="catalog")
    rpc = calls[0][0]
    if declared == "stop":
        await runtime.stop(scope, task_id=job.task_id)
    else:
        await rpc.emit(
            "item/completed",
            {
                "threadId": created["thread_id"],
                "turnId": created["turn_id"],
                "item": {
                    "type": "agentMessage",
                    "text": json.dumps({"task_status": declared, "text": "fact"}),
                },
            },
        )
    now[0] += service.lease_seconds + 1
    await rpc.emit(
        "turn/completed",
        {
            "threadId": created["thread_id"],
            "turn": {
                "id": created["turn_id"],
                "status": "interrupted" if declared == "stop" else "completed",
            },
        },
    )
    current = service.get(job.task_id, scope)
    assert current.status == "unknown_result"
    assert current.lease_token is None and rpc.closed and not runtime.active
    await runtime.close()


@pytest.mark.asyncio
async def test_guide_reloads_generation_after_concurrent_resume(env):
    runtime, host, _, scope, task, calls, _, provider = env
    job = task()
    created = await runtime.create(job, model_id="catalog")
    await runtime.stop(scope, task_id=job.task_id)
    await calls[0][0].emit(
        "turn/completed",
        {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "interrupted"},
        },
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def ensure(job):
        entered.set()
        await release.wait()

    host.ensure_workspace = ensure
    next_provider = SimpleNamespace(
        provider_config={**provider.provider_config, "model": "gpt-6.1-sol"},
        get_model=lambda: "gpt-6.1-sol",
        get_current_key=provider.get_current_key,
    )
    host.plugin.context.get_provider_by_id = lambda provider_id: (
        next_provider if provider_id == "next-provider" else provider
    )
    resuming = asyncio.create_task(runtime.resume(scope, task_id=job.task_id))
    await entered.wait()
    guiding = asyncio.create_task(
        runtime.guide(
            scope,
            task_id=job.task_id,
            text="",
            model_id="next-provider",
            reasoning_effort="high",
        )
    )
    await asyncio.sleep(0)
    release.set()
    await resuming
    changed = await guiding
    assert changed["applies_to"] == "next_turn"
    assert changed["reasoning_effort"] == "high"
    assert changed["model_id"] == "next-provider"
    assert changed["model"] == "gpt-6.1-sol"
    assert provider.get_model() == "gpt-6-sol"
    await runtime.close()


@pytest.mark.asyncio
async def test_first_input_includes_real_request_attachment_paths(env):
    runtime, _, _, _, task, calls, _, _ = env
    try:
        await runtime.create(
            task(
                request={
                    "attachments": [
                        {"path": "C:/task/input/实际附件.docx", "name": "实际附件.docx"}
                    ],
                    "system_prompt": "OLD_NATIVE_PROMPT",
                    "contexts": [{"content": "OLD_HISTORY"}],
                }
            ),
            model_id="catalog",
        )
        inputs = next(
            params["input"]
            for method, params in calls[0][0].calls
            if method == "turn/start"
        )
        assert "C:/task/input/实际附件.docx" in repr(inputs)
        assert "OLD_NATIVE_PROMPT" not in repr(inputs) and "OLD_HISTORY" not in repr(
            inputs
        )
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_expired_old_terminal_cannot_replace_new_lease(env):
    runtime, _, service, scope, task, calls, _, _ = env
    now = [service.clock()]
    service.clock = lambda: now[0]
    job = task()
    created = await runtime.create(job, model_id="catalog")
    old_job = runtime.active[job.task_id]["job"]
    now[0] += service.lease_seconds + 1
    assert service.recover_expired() == 1
    assert service.ack_control(old_job)
    new_job = service.claim_external(job.task_id, scope)
    await calls[0][0].emit(
        "turn/completed",
        {
            "threadId": created["thread_id"],
            "turn": {"id": created["turn_id"], "status": "failed"},
        },
    )
    current = service.get(job.task_id, scope)
    assert current.status == "running" and current.lease_token == new_job.lease_token
    service.fail(new_job, "unknown_result")
    await runtime.close()
