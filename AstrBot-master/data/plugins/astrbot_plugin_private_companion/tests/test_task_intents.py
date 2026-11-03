import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from task_intents import TaskIntentAdapter
from task_service import TaskScope, TaskService


class Event:
    def __init__(
        self,
        *,
        bot="bot",
        sender="sender",
        session="bot:GroupMessage:group",
        message="message",
    ):
        self.bot = bot
        self.sender = sender
        self.unified_msg_origin = session
        self.message_obj = SimpleNamespace(message_id=message)
        self.message_str = "帮我查询两个项目，并按需要重试自己的任务"
        self.extra = {"trusted_user_event": True, "authorized": True}

    def get_platform_id(self):
        return self.bot

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "group"


@pytest.fixture
def setup(tmp_path):
    service = TaskService(tmp_path / "tasks.sqlite")
    calls = []

    def authorize(request):
        calls.append(request)
        return True

    return service, TaskIntentAdapter(service, authorize), calls, tmp_path


def run(adapter, event, payload, workdir, **kwargs):
    options = {
        "trusted_user_event": True,
        "normally_awakened": True,
        "workdir": workdir,
    }
    options.update(kwargs)
    return asyncio.run(adapter.handle(event, payload, **options))


def test_chat_and_text_only_can_submit_without_trusting_model_identity(setup):
    service, adapter, calls, workdir = setup
    result = run(
        adapter,
        Event(),
        {
            "action": "submit",
            "goal": "查更新",
            "sender_id": "owner",
            "bot_id": "forged",
            "hub_action": "chat",
            "text_only": True,
        },
        workdir,
    )
    assert result["accepted"] and result["created"]
    task = service.get(
        result["task_id"], TaskScope("bot", "bot:GroupMessage:group", "sender")
    )
    assert task.goal == "查更新"
    assert calls[0].scope == task.scope
    assert calls[0].origin_message_id == "message"
    assert calls[0].original_text == Event().message_str
    assert not hasattr(calls[0].intent, "sender_id")


def test_original_user_can_revise_idle_task_without_creating_duplicate(setup):
    service, adapter, calls, workdir = setup
    created = run(adapter, Event(), {"action": "submit", "goal": "写草稿"}, workdir)
    result = run(adapter, Event(message="followup"), {
        "action": "revise", "task_id": created["task_id"], "goal": "草稿改成中文",
    }, workdir)
    assert result["accepted"]
    assert len(calls) == 2
    tasks = service.list(TaskScope("bot", "bot:GroupMessage:group", "sender"))
    assert len(tasks) == 1 and tasks[0].goal == "草稿改成中文"


def test_revision_requires_fresh_authorization_and_safe_status(setup):
    service, adapter, _, workdir = setup
    created = run(adapter, Event(), {"action": "submit", "goal": "写草稿"}, workdir)
    payload = {"action": "revise", "task_id": created["task_id"], "goal": "改稿"}
    denied = TaskIntentAdapter(service, lambda request: False)
    assert run(denied, Event(), payload, workdir)["code"] == "permission_denied"
    assert service.claim("worker")
    assert run(adapter, Event(), payload, workdir)["code"] == "invalid_transition"


@pytest.mark.parametrize(
    "flag, value",
    [
        ("trusted_user_event", False),
        ("trusted_user_event", 1),
        ("normally_awakened", False),
        ("normally_awakened", 1),
    ],
)
def test_only_trusted_normally_awakened_user_event_can_submit(setup, flag, value):
    service, adapter, calls, workdir = setup
    result = run(
        adapter, Event(), {"action": "submit", "goal": "执行"}, workdir, **{flag: value}
    )
    assert not result["accepted"]
    assert not calls
    assert not service.list(TaskScope("bot", "bot:GroupMessage:group", "sender"))


@pytest.mark.parametrize(
    "source", ["cron", "progress", "notification", "tool", "web_text"]
)
def test_synthetic_sources_cannot_become_authorization(setup, source):
    _, adapter, calls, workdir = setup
    assert not run(
        adapter, Event(), {"action": "submit", "goal": "执行"}, workdir, source=source
    )["accepted"]
    assert not calls


def test_hub_reply_and_restart_share_stable_fragment_not_goal_hash(setup):
    service, adapter, _, workdir = setup
    first = run(
        adapter,
        Event(),
        {"action": "submit", "goal": "初始目标"},
        workdir,
        source="hub",
    )
    restarted = TaskIntentAdapter(TaskService(service.path), lambda request: True)
    second = run(
        restarted, Event(), {"action": "submit", "goal": "修订后的同一片段"}, workdir
    )
    third = run(restarted, Event(), {"action": "submit", "goal": "第三次纠错"}, workdir)
    assert first["task_id"] == second["task_id"] == third["task_id"]
    assert first["created"] and not second["created"] and not third["created"]
    assert second["code"] == "already_exists"
    assert (
        service.get(
            first["task_id"], TaskScope("bot", "bot:GroupMessage:group", "sender")
        ).goal
        == "初始目标"
    )


def test_two_fragments_and_eight_fragment_limit_are_persistent(setup):
    service, adapter, _, workdir = setup
    ids = []
    for index in range(8):
        result = run(
            adapter,
            Event(),
            {"action": "submit", "fragment_key": f"part-{index}", "goal": "目标"},
            workdir,
        )
        assert result["accepted"]
        ids.append(result["task_id"])
    assert len(set(ids)) == 8
    restarted = TaskIntentAdapter(TaskService(service.path), lambda request: True)
    assert (
        run(
            restarted,
            Event(),
            {"action": "submit", "fragment_key": "ninth", "goal": "目标"},
            workdir,
        )["code"]
        == "fragment_limit"
    )
    assert (
        run(
            restarted,
            Event(),
            {"action": "submit", "fragment_key": "part-0", "goal": "修订"},
            workdir,
        )["task_id"]
        == ids[0]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "submit", "goal": ""},
        {"action": "submit", "goal": "x" * 4001},
        {"action": "submit", "goal": "有效", "fragment_key": "x" * 65},
        {"action": "submit", "goal": "有效", "fragment_key": ""},
        {"action": "grant", "goal": "越权"},
    ],
)
def test_invalid_or_oversized_model_fields_are_rejected(setup, payload):
    _, adapter, calls, workdir = setup
    assert not run(adapter, Event(), payload, workdir)["accepted"]
    assert not calls


def test_program_denial_and_revocation_cannot_be_bypassed(setup):
    service, _, _, workdir = setup
    allowed = True
    adapter = TaskIntentAdapter(service, lambda request: allowed)
    task = run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)
    allowed = False
    assert (
        run(
            adapter,
            Event(),
            {"action": "submit", "goal": "执行", "authorized": True},
            workdir,
        )["code"]
        == "permission_denied"
    )
    assert run(
        adapter,
        Event(message="pause"),
        {"action": "pause", "task_id": task["task_id"]},
        workdir,
    )["accepted"]
    assert (
        run(
            adapter,
            Event(message="resume"),
            {"action": "resume", "task_id": task["task_id"]},
            workdir,
        )["code"]
        == "permission_denied"
    )
    assert (
        run(adapter, Event(), {"action": "query", "task_id": task["task_id"]}, workdir)[
            "status"
        ]
        == "paused"
    )
    assert (
        run(
            adapter, Event(), {"action": "cancel", "task_id": task["task_id"]}, workdir
        )["status"]
        == "cancelled"
    )


@pytest.mark.parametrize(
    "identity", [{"bot": "other"}, {"sender": "other"}, {"session": "other"}]
)
def test_cross_identity_cannot_read_or_control_others_tasks(setup, identity):
    _, adapter, _, workdir = setup
    task = run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)
    for action in ("query", "cancel", "pause", "retry", "resume"):
        result = run(
            adapter,
            Event(**identity),
            {"action": action, "task_id": task["task_id"]},
            workdir,
        )
        assert result["code"] == "not_found"
    assert run(adapter, Event(**identity), {"action": "query"}, workdir)["tasks"] == []


def test_own_retry_and_resume_but_never_unknown_result(setup):
    service, adapter, _, workdir = setup
    task = run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)
    scope = TaskScope("bot", "bot:GroupMessage:group", "sender")
    assert run(
        adapter, Event(), {"action": "pause", "task_id": task["task_id"]}, workdir
    )["accepted"]
    assert run(
        adapter, Event(), {"action": "resume", "task_id": task["task_id"]}, workdir
    )["accepted"]
    job = service.claim("worker")
    service.fail(job, "protocol_error")
    assert run(
        adapter, Event(), {"action": "retry", "task_id": task["task_id"]}, workdir
    )["accepted"]
    job = service.claim("worker")
    service.fail(job, "unknown_result")
    for action in ("retry", "resume"):
        assert (
            run(
                adapter,
                Event(),
                {"action": action, "task_id": task["task_id"]},
                workdir,
            )["code"]
            == "invalid_transition"
        )
    assert service.get(task["task_id"], scope).status == "unknown_result"


def test_missing_identity_fails_closed_and_async_authorization_is_supported(setup):
    service, _, _, workdir = setup

    async def authorize(request):
        return request.scope.sender_id == "sender"

    adapter = TaskIntentAdapter(service, authorize)
    assert run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)[
        "accepted"
    ]
    for event in (
        Event(bot=""),
        Event(sender=""),
        Event(session=""),
        Event(message=""),
    ):
        assert (
            run(adapter, event, {"action": "submit", "goal": "执行"}, workdir)["code"]
            == "invalid_event"
        )


def test_authorization_exception_or_truthy_value_is_not_permission(setup):
    service, _, _, workdir = setup
    for authorize in (lambda request: "yes", lambda request: 1):
        adapter = TaskIntentAdapter(service, authorize)
        assert (
            run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)["code"]
            == "permission_denied"
        )

    def broken(request):
        raise RuntimeError("private authorization detail")

    adapter = TaskIntentAdapter(service, broken)
    result = run(adapter, Event(), {"action": "submit", "goal": "执行"}, workdir)
    assert result["code"] == "permission_denied"
    assert "private" not in str(result)


def test_parallel_adapters_cannot_submit_a_ninth_fragment(setup):
    service, adapter, _, workdir = setup

    def submit(index):
        separate = TaskIntentAdapter(TaskService(service.path), lambda request: True)
        return run(
            separate,
            Event(),
            {"action": "submit", "fragment_key": f"parallel-{index}", "goal": "执行"},
            workdir,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(16)))
    assert sum(result["created"] for result in results) == 8
    assert len(run(adapter, Event(), {"action": "query"}, workdir)["tasks"]) == 8


def test_preexisting_service_source_is_reported_as_existing(setup):
    service, adapter, _, workdir = setup
    task = service.submit(
        TaskScope("bot", "bot:GroupMessage:group", "sender"),
        "message",
        "main",
        "原目标",
        workdir,
    )
    result = run(adapter, Event(), {"action": "submit", "goal": "修订"}, workdir)
    assert result["task_id"] == task.task_id
    assert result["created"] is False
    assert result["code"] == "already_exists"


def test_host_metadata_is_present_in_initial_queue_write(setup):
    service, _, _, workdir = setup
    adapter = TaskIntentAdapter(
        service, lambda request: True,
        metadata_factory=lambda request: {"request": {"primary": "chosen"}},
    )
    result = run(adapter, Event(), {"goal": "查询"}, workdir)
    task = service.get(result["task_id"], TaskScope("bot", "bot:GroupMessage:group", "sender"))
    assert task.metadata["request"]["primary"] == "chosen"
