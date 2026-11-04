"""任务级模型参数及同模型有限恢复，不调用真实模型。"""

import sqlite3
from types import SimpleNamespace

import pytest

from task_agent_executor import TaskAgentExecutor
from task_service import TaskFailure, TaskScope, TaskService

try:
    from task_model_policy import request_policy
except ImportError:
    def request_policy(_):
        raise AssertionError("任务模型策略尚未实现")


def test_defaults_mean_three_total_requests():
    policy = request_policy({"primary": "gpt6", "fallback": "old-backup"})
    assert policy.provider_id == "gpt6"
    assert policy.timeout_seconds == 120
    assert policy.max_attempts == 3


@pytest.mark.parametrize("field,value", [
    ("request_timeout_seconds", True), ("request_timeout_seconds", 0),
    ("request_timeout_seconds", float("nan")),
    ("request_max_attempts", True), ("request_max_attempts", 0),
    ("request_max_attempts", 2.5),
])
def test_invalid_request_policy_is_rejected(field, value):
    with pytest.raises(ValueError):
        request_policy({"primary": "gpt6", field: value})


@pytest.mark.asyncio
async def test_same_model_three_attempts_survive_restart_and_pass_sdk_timeout(tmp_path):
    now = [1000.0]
    path = tmp_path / "tasks.sqlite3"
    service = TaskService(path, clock=lambda: now[0])
    scope = TaskScope("bot", "group", "owner")
    task = service.submit(scope, "msg", "main", "做任务", str(tmp_path), metadata={
        "request": {"primary": "gpt6", "fallback": "old-backup", "contexts": [],
                    "request_timeout_seconds": 240, "request_max_attempts": 3}
    })
    calls = []

    async def text_chat(**kwargs):
        calls.append(kwargs)
        raise TimeoutError("network fluctuation")

    resolved = []

    def resolve(name):
        resolved.append(name)
        return SimpleNamespace(text_chat=text_chat)

    for attempt in range(1, 4):
        service = TaskService(path, clock=lambda: now[0])
        job = service.claim("worker")
        assert job is not None
        executor = TaskAgentExecutor(service, resolve_provider=resolve,
                                    resolve_tools=lambda *_: {}, execute_tool=None,
                                    authorize=lambda *_: True)
        with pytest.raises(TaskFailure) as caught:
            await executor.execute(job)
        assert service.fail(job, caught.value.error_kind, retryable=caught.value.retryable)
        current = service.get(task.task_id, scope)
        assert current.status == ("waiting_retry" if attempt < 3 else "failed")
        now[0] += 100
    assert service.claim("worker") is None
    assert resolved == ["gpt6"] * 3
    assert all(call["timeout"] == 240 and call["request_max_retries"] == 0 for call in calls)


def test_one_attempt_does_not_retry(tmp_path):
    service = TaskService(tmp_path / "tasks.db")
    scope = TaskScope("b", "g", "u")
    task = service.submit(scope, "m", "main", "任务", str(tmp_path), metadata={
        "request": {"primary": "gpt6", "request_max_attempts": 1}
    })
    job = service.claim("worker")
    assert service.fail(job, "provider_unavailable", retryable=True)
    assert service.get(task.task_id, scope).status == "failed"


def test_request_failures_notify_only_first_and_exhaustion(tmp_path):
    now = [1000.0]
    service = TaskService(tmp_path / "tasks.db", clock=lambda: now[0])
    scope = TaskScope("b", "g", "u")
    task = service.submit(scope, "m", "main", "任务", str(tmp_path), metadata={
        "request": {"primary": "gpt6", "request_max_attempts": 3}
    })
    for _ in range(3):
        job = service.claim("worker")
        assert service.fail(job, "provider_unavailable", retryable=True)
        now[0] += 100
    with sqlite3.connect(service.path) as db:
        kinds = [r[0] for r in db.execute("SELECT kind FROM pt_outbox WHERE task_id=? ORDER BY notification_id", (task.task_id,))]
    assert kinds == ["accepted", "model_retry", "result"]


def test_periodic_progress_is_silent_during_model_reconnection(tmp_path):
    now = [1000.0]
    service = TaskService(tmp_path / "tasks.db", clock=lambda: now[0])
    scope = TaskScope("b", "g", "u")
    service.submit(scope, "m", "main", "任务", str(tmp_path), metadata={"request": {"primary": "gpt6"}})
    first = service.claim("w")
    service.fail(first, "provider_unavailable", retryable=True)
    now[0] += 16
    second = service.claim("w")
    assert not service.progress(second, "still running")


def test_successful_step_resets_request_budget_and_manual_retry_starts_new_cycle(tmp_path):
    now = [1000.0]
    service = TaskService(tmp_path / "tasks.db", clock=lambda: now[0])
    scope = TaskScope("b", "g", "u")
    task = service.submit(scope, "m", "main", "任务", str(tmp_path), metadata={
        "request": {"primary": "gpt6", "request_max_attempts": 3}
    })
    first = service.claim("w")
    service.fail(first, "provider_unavailable", retryable=True)
    now[0] += 100
    recovered = service.claim("w")
    assert service.record_step(recovered, recovered.step_key, {"verified": True}, next_step="next")
    current = service.get(task.task_id, scope)
    assert current.attempts == 0 and current.error_kind is None
    # 成功工具步骤之后，下一次模型请求使用自己的三次预算。
    assert service.begin_attempt(recovered, "gpt6")
    assert service.fail(recovered, "provider_unavailable", retryable=True)
    assert service.get(task.task_id, scope).status == "waiting_retry"
    for _ in range(2):
        now[0] += 100
        job = service.claim("w")
        assert job is not None
        service.fail(job, "provider_unavailable", retryable=True)
    assert service.get(task.task_id, scope).status == "failed"
    assert service.retry(task.task_id, scope)
    now[0] += 100
    job = service.claim("w")
    assert job is not None
    assert service.fail(job, "provider_unavailable", retryable=True)
    with sqlite3.connect(service.path) as db:
        notices = db.execute("SELECT fingerprint FROM pt_outbox WHERE task_id=? AND kind='model_retry'", (task.task_id,)).fetchall()
        steps = db.execute("SELECT count(*) FROM pt_steps WHERE task_id=?", (task.task_id,)).fetchone()[0]
    assert len(notices) == 2 and len(set(notices)) == 2
    assert steps == 1
