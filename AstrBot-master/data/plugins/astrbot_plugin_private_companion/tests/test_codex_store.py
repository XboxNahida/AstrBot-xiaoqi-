import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from task_service import TaskScope, TaskService


@pytest.fixture
def env(tmp_path):
    clock = [1000.0]
    service = TaskService(tmp_path / "tasks.db", clock=lambda: clock[0])
    scope = TaskScope("bot", "group", "user")
    task = service.submit(
        scope,
        "m",
        "f",
        "same title",
        tmp_path,
        metadata={"engine": "codex", "attachments": ["original"]},
    )
    return service, scope, task, clock


@pytest.mark.parametrize("status", ["queued", "waiting_retry"])
def test_native_never_claims_codex(env, status):
    service, scope, task, _ = env
    with service._transaction() as db:
        db.execute("UPDATE pt_tasks SET status=?", (status,))
    assert service.claim("native") is None


@pytest.mark.parametrize(
    "status",
    ["queued", "paused", "unknown_result", "failed", "waiting_user", "succeeded"],
)
def test_external_claim_fences_and_preserves_checkpoints(env, status):
    service, scope, task, clock = env
    with service._transaction() as db:
        db.execute("UPDATE pt_tasks SET status=?,result=?", (status, '{"old":true}'))
        db.execute(
            "INSERT INTO pt_steps VALUES(?,?,?,?)", (task.task_id, "old", "{}", 10)
        )
    assert (
        service.claim_external(task.task_id, replace(scope, sender_id="other")) is None
    )
    job = service.claim_external(task.task_id, scope)
    assert job.status == "running" and job.revision == task.revision + 1
    assert job.result is None and job.attempts == 0 and "old" in job.steps
    assert service.claim_external(task.task_id, scope) is None
    with service._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM pt_attempts").fetchone()[0] == 0
    assert service.record_activity(job, "model_response", "codex")
    previous = service.get(task.task_id, scope).metadata["execution_state"]
    clock[0] += 1
    assert service.heartbeat(job)
    assert service.get(task.task_id, scope).metadata["execution_state"] == previous
    clock[0] += 100
    assert service.recover_expired() == 1
    assert not service.finish(job, {"ok": True})
    assert service.claim_external(task.task_id, scope) is None
    assert service.ack_control(job)
    new = service.claim_external(task.task_id, scope)
    assert new.lease_token != job.lease_token
    assert service.cancel(task.task_id, scope)
    assert not service.heartbeat(new)
    assert service.ack_control(new)
    assert service.claim_external(task.task_id, scope) is None


def store(env):
    module = importlib.import_module("codex_store")
    return module.CodexStore(env[0])


def test_public_event_projection_keeps_business_fields_and_removes_nested_private_items(
    env,
):
    _, scope, task, _ = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    business = {
        "request": {"product": "ordinary"},
        "headers": ["column"],
        "api_key": "example",
        "access_token": "example credential",
    }
    payload = {
        "receipt": {
            **business,
            "nested": {"type": "reasoning", "content": ["ordinary dataset"]},
        },
        "turn": {
            "items": [
                {"type": "userMessage", "content": ["full private input"]},
                {"type": "reasoning", "content": ["private reasoning"]},
                {"type": "commandExecution", "output": "checked"},
            ]
        },
        "tokenUsage": {"last": {"reasoningOutputTokens": 9}},
    }
    s.append_event(
        scope, task.task_id, generation=0, method="host_tool/completed", payload=payload
    )
    public = s.events(scope, task.task_id)["items"][0]["payload"]
    assert all(public["receipt"][key] == value for key, value in business.items())
    assert public["receipt"]["nested"]["type"] == "reasoning"
    assert "private" not in json.dumps(public)
    assert (
        "checked" in json.dumps(public)
        and public["tokenUsage"]["last"]["reasoningOutputTokens"] == 9
    )


@pytest.mark.parametrize("legacy_preview", [False, True])
def test_existing_events_are_projected_before_public_pagination(env, legacy_preview):
    service, scope, task, _ = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    payload = {
        "turn": {
            "items": [
                {"type": "userMessage", "content": "old full input"},
                {"type": "reasoning", "summary": "old private reasoning"},
            ]
        }
    }
    if legacy_preview:
        payload = {"preview": json.dumps(payload)[:-5], "truncated": True}
    with service._transaction() as db:
        for _ in range(2):
            db.execute(
                "INSERT INTO pc_codex_events(task_id,generation,method,payload,created_at) VALUES(?,0,?,?,0)",
                (task.task_id, "turn/completed", json.dumps(payload)),
            )
    first = s.events(scope, task.task_id, limit=1)
    assert "old full input" not in json.dumps(
        first
    ) and "old private reasoning" not in json.dumps(first)
    second = s.events(scope, task.task_id, after=first["next_after"], limit=1)
    assert second["items"][0]["id"] > first["items"][0]["id"]
    assert second["next_after"] is None


def test_projection_precedes_oversize_preview_and_handles_json_tool_result(env):
    service, scope, task, _ = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    payload = {
        "receipt": {
            "content": json.dumps(
                {
                    "events": {
                        "items": [
                            {
                                "method": "turn/completed",
                                "payload": {
                                    "turn": {
                                        "items": [
                                            {
                                                "type": "reasoning",
                                                "content": "hidden thought",
                                            }
                                        ]
                                    }
                                },
                            }
                        ]
                    },
                    "request": "business request",
                }
            )
        },
        "many": ["x" * 10000] * 30,
    }
    s.append_event(
        scope, task.task_id, generation=0, method="host_tool/completed", payload=payload
    )
    with service._transaction() as db:
        encoded = db.execute(
            "SELECT payload FROM pc_codex_events WHERE task_id=?", (task.task_id,)
        ).fetchone()[0]
    assert "hidden thought" not in encoded
    assert "business request" in encoded
    public = s.events(scope, task.task_id)
    assert "hidden thought" not in json.dumps(public)


def test_external_lease_does_not_consume_native_capacity(env):
    service, scope, task, _ = env
    assert service.claim_external(task.task_id, scope)
    native = service.submit(scope, "native", "f", "native", service.path.parent)
    assert service.claim("native").task_id == native.task_id


@pytest.mark.parametrize(
    "metadata,status",
    [
        ({"engine": "native"}, "queued"),
        ({"engine": "codex", "history_deleted": True}, "queued"),
        ({"engine": "codex"}, "cancelled"),
        ({"engine": "codex"}, "waiting_retry"),
    ],
)
def test_external_rejects_ineligible(env, metadata, status):
    service, scope, task, _ = env
    with service._transaction() as db:
        db.execute(
            "UPDATE pt_tasks SET metadata=?,status=?", (json.dumps(metadata), status)
        )
    assert service.claim_external(task.task_id, scope) is None


def test_concurrent_claim_and_thread_uniqueness(env):
    service, scope, task, _ = env
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(lambda _: service.claim_external(task.task_id, scope), range(4))
        )
    assert sum(job is not None for job in claims) == 1
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    other = service.submit(
        scope,
        "m2",
        "f",
        "same title",
        service.path.parent,
        metadata={"engine": "codex"},
    )
    s.bind(other, model_id="p", model="gpt")
    assert s.attach(scope, task.task_id, thread_id="unique", generation=0)
    assert not s.attach(scope, other.task_id, thread_id="unique", generation=0)
    assert s.next_generation(scope, task.task_id) == 1
    assert not s.attach(scope, task.task_id, thread_id="unique", generation=0)
    assert not s.disconnected(scope, task.task_id, generation=0)
    assert s.get(scope, task_id=task.task_id)["connection_state"] == "connecting"
    with pytest.raises(ValueError):
        s.append_event(
            scope, task.task_id, generation=1, method="heartbeat", payload={}
        )
    assert s.append_event(
        scope,
        task.task_id,
        generation=1,
        method="tool/result",
        payload={"success": False},
    )
    assert service.get(task.task_id, scope).status == "running"


def test_usage_and_business_fields_preserved_but_auth_envelopes_omitted(env):
    _, scope, task, _ = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    payload = {
        "usage": {"input_tokens": 20, "cached_tokens": 10},
        "request_id": "r1",
        "token": "business-token",
        "secret_recipe": "ordinary data",
        "password_policy": "minimum length",
        "authorization": "Bearer private",
        "headers": {"Authorization": "private"},
        "system_prompt": "private",
        "baseInstructions": "private",
        "request_body": {"input": "private"},
    }
    s.append_event(
        scope, task.task_id, generation=0, method="response/completed", payload=payload
    )
    observed = s.events(scope, task.task_id)["items"][0]["payload"]
    assert observed == {
        key: payload[key]
        for key in ("usage", "request_id", "token", "secret_recipe", "password_policy")
    }


def test_binding_identity_generation_and_snapshots(env):
    service, scope, task, clock = env
    s = store(env)
    first = s.bind(
        task, model_id="provider/model", model="gpt", reasoning_effort="medium"
    )
    assert first["generation"] == 0 and first["connection_state"] == "disconnected"
    assert s.attach(scope, task.task_id, thread_id="thread", generation=0)
    assert s.bind(task, model_id="other", model="other")["model"] == "gpt"
    assert not s.attach(scope, task.task_id, thread_id="different", generation=0)
    assert s.get(scope, task_id=task.task_id, thread_id="wrong") is None
    for field in ("bot_id", "session_key", "sender_id"):
        wrong = replace(scope, **{field: "other"})
        assert s.get(wrong, thread_id="thread") is None
        assert s.list(wrong)["total"] == 0
        with pytest.raises(LookupError):
            s.events(wrong, task.task_id)
        assert not s.update(wrong, task.task_id, generation=0, model="bad")
    assert s.next_generation(scope, task.task_id) == 1
    assert not s.update(scope, task.task_id, generation=0, model="bad")
    assert (
        s.append_event(scope, task.task_id, generation=0, method="old", payload={})
        is None
    )
    assert s.update(scope, task.task_id, generation=1, model_id="new", turn_id="turn")
    assert s.disconnected(scope, task.task_id, generation=1)
    current = service.get(task.task_id, scope)
    assert current.metadata["codex"]["thread_id"] == "thread"
    assert current.metadata["request"]["primary"] == "new"
    assert current.metadata["attachments"] == ["original"]
    assert current.status == "queued"
    assert s.get(scope, task_id=task.task_id)["sender_id"] == scope.sender_id


def test_events_bounded_valid_json_real_time_and_pagination(env):
    service, scope, task, clock = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    ids = []
    for i in range(4):
        clock[0] += 3
        ids.append(
            s.append_event(
                scope,
                task.task_id,
                generation=0,
                method="tool/result",
                payload={
                    "text": "中" * 50000,
                    "nested": [[[[[[[1]]]]]]],
                    "api_key": "secret",
                    "system_prompt": "private",
                },
            )
        )
    after = 0
    seen = []
    while True:
        page = s.events(scope, task.task_id, after=after, limit=50)
        assert len(json.dumps(page, ensure_ascii=False)) <= 20000
        seen += [x["id"] for x in page["items"]]
        for item in page["items"]:
            assert len(json.dumps(item["payload"], ensure_ascii=False)) <= 16000
            assert "secret" not in json.dumps(item) and "private" not in json.dumps(
                item
            )
        if page["next_after"] is None:
            break
        assert page["next_after"] > after
        after = page["next_after"]
    assert seen == ids
    assert s.get(scope, task_id=task.task_id)["last_event_at"] == clock[0]
    assert service.get(task.task_id, scope).status == "queued"
    assert s.events(scope, task.task_id)["items"][0]["created_at"] == 1003


def test_validation_and_same_title_restart(env):
    service, scope, task, _ = env
    s = store(env)
    s.bind(task, model_id="p", model="gpt")
    other = service.submit(
        scope,
        "m2",
        "f",
        "same title",
        service.path.parent,
        metadata={"engine": "codex"},
    )
    s.bind(other, model_id="p", model="gpt")
    assert store(env).list(scope, limit=1)["next_offset"] == 1
    assert store(env).list(scope, offset=1)["total"] == 2
    for kwargs in (
        {"generation": True, "model": "x"},
        {"generation": 0, "thread_id": "x"},
    ):
        with pytest.raises(ValueError):
            s.update(scope, task.task_id, **kwargs)
    with pytest.raises(ValueError):
        s.list(scope, limit=True)
    native = service.submit(scope, "native", "f", "native", service.path.parent)
    with pytest.raises(ValueError):
        s.bind(native, model_id="p", model="gpt")
