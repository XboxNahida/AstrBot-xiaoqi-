import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from task_service import TaskFailure, TaskScope, TaskService

try:
    from task_agent_executor import TaskAgentExecutor
except ImportError:
    TaskAgentExecutor = None


class Response:
    def __init__(self, text="", calls=(), role="assistant"):
        self.completion_text = text
        self.role = role
        self.calls = calls

    def to_openai_tool_calls(self):
        return [
            {
                "id": str(i),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(self.calls)
        ]


class Provider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def text_chat(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def text_chat_stream(self, **kwargs):
        yield await self.text_chat(**kwargs)


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_pages_exclude_their_own_reads_and_keep_original_arguments(self):
        job = self.submit(mode="reasoning", required_tool_names=[], dynamic_tool_catalog=True)
        self.primary = Provider(
            Response(calls=[("read", {"path": "ORIGINAL_ARGUMENT_ONLY"})]),
            Response(calls=[("read", {"path": "second"})]),
            Response(calls=[("pc_task_read_steps", {"offset": 0, "max_chars": 6000})]),
            Response(calls=[("pc_task_read_steps", {"offset": 1, "max_chars": 6000})]),
            Response("done"),
        )
        result = await self.executor().execute(job)
        self.assertEqual("done", result["text"])
        pages = []
        for request in self.primary.requests[3:]:
            latest = request["contexts"][-1]
            pages.append(json.loads(latest["content"])["content"])
        self.assertEqual([2, 2], [page["total_steps"] for page in pages])
        self.assertTrue(pages[0]["has_more"])
        self.assertFalse(pages[1]["has_more"])
        self.assertIn("ORIGINAL_ARGUMENT_ONLY", pages[0]["text"])
        self.assertIn("tool_call", pages[0]["text"])

    async def test_legacy_step_arguments_are_recovered_from_the_bound_tool_call(self):
        job = self.submit(mode="reasoning", required_tool_names=[], dynamic_tool_catalog=True)
        contexts = [{"role": "user", "content": "goal"},
                    {"role": "assistant", "tool_calls": [{"id": "legacy-call", "type": "function", "function": {"name": "read", "arguments": '{"path":"legacy-path"}'}}]},
                    {"role": "tool", "tool_call_id": "legacy-call", "content": '{"status":"success","content":"ok"}'}]
        self.service.record_step(job, job.step_key, {"kind": "tool", "number": 1, "tool": "read", "contexts": contexts, "receipts": [], "pending_calls": [], "outcome": {"content": "ok"}}, next_step="tool:2")
        self.primary = Provider(Response(calls=[("pc_task_read_steps", {"offset": 0, "max_chars": 6000})]), Response("done"))
        await self.executor().execute(self.service.get(job.task_id, job.scope))
        page = json.loads(self.primary.requests[-1]["contexts"][-1]["content"])["content"]
        self.assertIn("legacy-path", page["text"])
        self.assertIn("legacy-call", page["text"])

    async def test_model_window_summary_is_durable_and_keeps_full_tool_steps(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        rounds = [{"role": "user", "content": "goal attachment-ref"}]
        for index in range(6):
            rounds.extend([
                {"role": "assistant", "tool_calls": [{"id": str(index), "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": str(index), "content": "observation" * 250},
            ])
        self.service.record_step(job, job.step_key, {"kind": "tool", "number": 6, "contexts": rounds, "receipts": [], "pending_calls": [], "outcome": {"content": "original"}}, next_step="tool:7")
        job = self.service.get(job.task_id, job.scope)
        self.primary = Provider(Response("Verified observations; no operation should be replayed."), Response("done"))
        self.primary.provider_config = {"max_context_tokens": 11000}
        result = await self.executor().execute(job)
        self.assertEqual("done", result["text"])
        self.assertEqual(2, len(self.primary.requests))
        self.assertIn("摘要", self.primary.requests[0]["system_prompt"])
        self.assertLess(len(str(self.primary.requests[-1]["contexts"])), len(str(rounds)))
        state = self.service.get(job.task_id, job.scope)
        summaries = [s for s in state.steps.values() if s.get("kind") == "context_summary"]
        self.assertEqual(1, len(summaries))
        self.assertEqual(rounds, next(s for s in state.steps.values() if s.get("kind") == "tool")["contexts"])
        self.assertIn("goal attachment-ref", str(summaries[0]["contexts"]))
        self.assertEqual(self.primary.requests[0]["prompt_cache_key"], self.primary.requests[1]["prompt_cache_key"])

    async def test_summary_failure_waits_for_user_and_preserves_original_checkpoint(self):
        job = self.submit(mode="reasoning", required_tool_names=[], contexts=[{"role": "user", "content": "x" * 30000}])
        self.primary = Provider(RuntimeError("must not send oversized input"))
        self.primary.provider_config = {"fallback_max_context_tokens": 8192}
        state = await self.run_failure(self.executor(), job, "context_limit")
        self.assertEqual("waiting_user", state.status)
        self.assertEqual("x" * 30000, state.metadata["request"]["contexts"][0]["content"])

    async def test_own_history_tool_is_bounded_and_does_not_call_external_adapter(self):
        job = self.submit(mode="reasoning", required_tool_names=[], dynamic_tool_catalog=True)
        self.primary = Provider(Response(calls=[("pc_task_read_steps", {"offset": 0, "max_chars": 1000})]), Response("done"))
        result = await self.executor().execute(job)
        self.assertEqual("done", result["text"])
        self.assertEqual([], self.executions)
        tool_reply = self.primary.requests[-1]["contexts"][-1]
        self.assertLessEqual(len(tool_reply["content"]), 1800)
        self.assertIn("total_steps", tool_reply["content"])

    async def test_summary_outage_keeps_tool_checkpoint_and_waits_for_recovery(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        rounds = [{"role": "user", "content": "original goal"}]
        for index in range(6):
            rounds.extend([
                {"role": "assistant", "tool_calls": [{"id": str(index)}]},
                {"role": "tool", "tool_call_id": str(index), "content": "observation" * 250},
            ])
        self.service.record_step(job, job.step_key, {"kind": "tool", "number": 6, "contexts": rounds, "receipts": [], "pending_calls": []}, next_step="tool:7")
        self.primary = Provider(RuntimeError("summary unavailable"))
        self.primary.provider_config = {"max_context_tokens": 11000}
        state = await self.run_failure(self.executor(), self.service.get(job.task_id, job.scope), "context_compaction_failed")
        self.assertEqual("waiting_user", state.status)
        self.assertEqual(1, len(self.primary.requests))
        self.assertEqual(rounds, next(iter(state.steps.values()))["contexts"])
        self.assertFalse(any(s.get("kind") == "context_summary" for s in state.steps.values()))

    async def test_summary_checkpoint_resumes_without_resummarizing_or_replaying(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        compact = [{"role": "user", "content": "goal retained"}, {"role": "user", "content": "summary retained", "task_context_summary": True}]
        self.assertTrue(self.service.record_context_checkpoint(job, {"kind": "context_summary", "number": 4, "contexts": compact, "receipts": [], "pending_calls": [], "summary": "summary retained"}))
        self.service.fail(job, "provider_unavailable", retryable=True)
        self.now += 16
        resumed = self.service.claim("resume")
        self.primary = Provider(Response("done"))
        result = await self.executor().execute(resumed)
        self.assertEqual("done", result["text"])
        self.assertEqual(1, len(self.primary.requests))
        self.assertIn("summary retained", str(self.primary.requests[0]["contexts"]))
        self.assertTrue(all("task_context_summary" not in m for m in self.primary.requests[0]["contexts"]))
        self.assertEqual([], self.executions)

    async def test_summary_checkpoint_does_not_reset_current_attempt_budget(self):
        job = self.submit(mode="reasoning", required_tool_names=[], request_max_attempts=2)
        self.assertTrue(self.service.record_context_checkpoint(job, {"kind": "context_summary", "number": 0, "contexts": [], "summary": "first"}))
        self.assertTrue(self.service.begin_attempt(job, "primary", max_attempts=2))
        self.assertTrue(self.service.record_context_checkpoint(job, {"kind": "context_summary", "number": 0, "contexts": [], "summary": "second"}))
        self.assertFalse(self.service.begin_attempt(job, "primary", max_attempts=2))
        self.assertEqual(2, self.service.get(job.task_id, job.scope).attempts)

    async def test_history_page_cannot_read_foreign_task_and_bounds_escaped_content(self):
        job = self.submit(mode="reasoning", required_tool_names=[], dynamic_tool_catalog=True)
        self.service.submit(TaskScope("otherbot", "othergroup", "otherowner"), "other", "other", "FOREIGN_PRIVATE_CONTENT", self.temp.name)
        self.service.record_step(job, job.step_key, {"kind": "tool", "number": 1, "contexts": [], "outcome": {"content": '\n\\"' * 10000}}, next_step="tool:2")
        self.primary = Provider(Response(calls=[("pc_task_read_steps", {"max_chars": 1000})]), Response("done"))
        await self.executor().execute(self.service.get(job.task_id, job.scope))
        reply = json.loads(self.primary.requests[-1]["contexts"][-1]["content"])
        self.assertLessEqual(len(json.dumps(reply["content"], ensure_ascii=False)), 1000)
        self.assertNotIn("FOREIGN_PRIVATE_CONTENT", str(reply))
        self.assertTrue(reply["content"]["has_more"])

    async def test_huge_tool_reply_uses_preview_but_full_original_remains_readable(self):
        job = self.submit(mode="reasoning", required_tool_names=[], dynamic_tool_catalog=True)
        original = "large-result-details" * 10000
        self.primary = Provider(Response(calls=[("read", {})]), Response("done"))

        async def execute(*args):
            return {"status": "success", "content": original, "receipt": {"observed": True}}

        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual("done", result["text"])
        sent = self.primary.requests[-1]["contexts"][-1]["content"]
        self.assertLess(len(sent), 5000)
        self.assertIn("pc_task_read_steps", sent)
        saved = [s for s in self.service.get(job.task_id, job.scope).steps.values() if s.get("kind") == "tool"]
        self.assertEqual(original, saved[0]["outcome"]["content"])

    async def test_activity_exposes_live_tool_and_retains_issue_after_model_continues(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        self.primary = Provider(Response(calls=[("read", {})]), Response("done"))

        async def execute(job, tool, args):
            current = self.service.get(job.task_id, job.scope)
            self.assertEqual(current.metadata["execution_state"]["phase"], "tool_running")
            self.assertEqual(current.metadata["execution_state"]["name"], "read")
            return {"status": "error", "content": "recoverable tool error", "receipt": {"tool": "read"}}

        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual(result["text"], "done")
        state = self.service.get(job.task_id, job.scope).metadata["execution_state"]
        self.assertEqual(state["phase"], "model_response")
        self.assertEqual(state["last_tool_issue"]["status"], "error")
        self.assertEqual(state["last_tool_issue"]["name"], "read")

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000.0
        self.scope = TaskScope("bot", "group", "owner")
        self.service = TaskService(
            Path(self.temp.name) / "tasks.db", clock=lambda: self.now
        )
        self.tools = {
            "read": SimpleNamespace(name="read"),
            "write": SimpleNamespace(name="write"),
        }
        self.executions = []
        self.permissions = []
        self.primary = Provider()
        self.fallback = Provider()

    def submit(self, **overrides):
        request = {
            "prompt": "inspect",
            "system_prompt": "persona",
            "contexts": [],
            "tool_names": ["read", "write"],
            "primary": "primary",
            "fallback": "fallback",
            "required_tool_names": ["read"],
        }
        request.update(overrides)
        self.task = self.service.submit(
            self.scope,
            "message",
            "fragment",
            "inspect",
            self.temp.name,
            metadata={"request": request},
        )
        return self.service.claim("test")

    async def execute_tool(self, job, tool, args):
        self.executions.append((job.scope, tool.name, args))
        return {
            "status": "success",
            "content": "actual result",
            "receipt": {"operation_id": f"receipt-{len(self.executions)}"},
        }

    def authorize(self, job, tool, args):
        self.permissions.append((job.scope, tool.name, args))
        return True

    def executor(self, **overrides):
        self.assertIsNotNone(TaskAgentExecutor, "独立持久任务执行适配器尚未实现")
        kwargs = {
            "resolve_provider": lambda name: {
                "primary": self.primary,
                "fallback": self.fallback,
            }[name],
            "resolve_tools": lambda job, names: self.tools,
            "execute_tool": self.execute_tool,
            "authorize": self.authorize,
        }
        kwargs.update(overrides)
        return TaskAgentExecutor(self.service, **kwargs)

    async def run_failure(self, executor, job, kind):
        with self.assertRaises(TaskFailure) as caught:
            await executor.execute(job)
        self.assertEqual(kind, caught.exception.error_kind)
        self.service.fail(job, kind, retryable=caught.exception.retryable)
        return self.service.get(job.task_id, job.scope)

    async def test_success_requires_actual_receipt_and_disables_provider_retries(self):
        job = self.submit()
        self.primary = Provider(
            Response(calls=[("read", {"path": "x"})]), Response("done")
        )
        result = await self.executor().execute(job)
        self.assertEqual("done", result["text"])
        self.assertEqual(["read"], result["successful_tool_names"])
        self.assertEqual("auto", self.primary.requests[0]["tool_choice"])
        self.assertEqual("auto", self.primary.requests[1]["tool_choice"])
        self.assertTrue(
            all(r["request_max_retries"] == 1 and r["timeout"] == 60 for r in self.primary.requests)
        )
        self.assertEqual(self.scope, self.executions[0][0])
        self.assertEqual(1, len(self.permissions))
        self.assertEqual(1, self.service.get(job.task_id, job.scope).attempts)

    async def test_restored_pending_tool_does_not_add_fourth_model_request(self):
        job = self.submit(request_max_attempts=3)
        calls = Response(calls=[("read", {"path": "x"})]).to_openai_tool_calls()
        self.assertTrue(self.service.record_step(job, job.step_key, {
            "kind": "tool", "number": 1,
            "contexts": [{"role": "assistant", "content": "", "tool_calls": calls}],
            "receipts": [], "pending_calls": calls,
        }, next_step="tool:2"))
        self.service.fail(job, "provider_unavailable", retryable=True)
        self.primary = Provider(*(TimeoutError("synthetic") for _ in range(4)))
        for attempt in range(1, 4):
            self.now += 100
            resumed = self.service.claim("resume")
            self.assertIsNotNone(resumed)
            current = await self.run_failure(self.executor(), resumed, "provider_unavailable")
            self.assertEqual("waiting_retry" if attempt < 3 else "failed", current.status)
        self.assertEqual(3, len(self.primary.requests))
        self.assertEqual(1, len(self.executions))

    async def test_current_multimodal_question_survives_tools_and_resume(self):
        key = "pc-task-image://current"
        image = "data:image/png;base64,c3ludGhldGlj"
        job = self.submit(
            current_input={"message_id": "new", "content": [
                {"type": "text", "text": "本轮H(z)，不是FFT"},
                {"type": "image_url", "image_url": {"url": key}},
            ]},
            media={key: image},
            contexts=[{"role": "user", "content": "上一题FFT"}],
        )
        self.primary = Provider(Response(calls=[("read", {"path": "x"})]), RuntimeError("interrupted"))
        saved = await self.run_failure(self.executor(), job, "provider_unavailable")
        first = self.primary.requests[0]["contexts"][-1]["content"]
        self.assertIsInstance(first, list)
        self.assertIn("本轮H(z)", str(first))
        self.assertIn(image, str(first))
        self.assertIn(key, str(saved.steps))
        self.assertNotIn(image, str(saved.steps))
        self.now += 1000
        resumed = self.service.claim("resume")
        self.primary = Provider(Response("done"))
        result = await self.executor().execute(resumed)
        self.assertEqual("done", result["text"])
        self.assertIn(image, str(self.primary.requests[0]["contexts"]))
        self.assertEqual(1, len(self.executions))

    async def test_text_only_task_provider_receives_image_notice_and_can_inspect(self):
        name = "pc_inspect_images"
        self.tools[name] = SimpleNamespace(name=name)
        job = self.submit(tool_names=[name], required_tool_names=[name],
            current_input={"message_id": "new", "content": [
                {"type": "text", "text": "本轮新题"},
                {"type": "image_url", "image_url": {"url": "pc-task-image://new"}},
            ]}, media={"pc-task-image://new": "data:image/png;base64,YQ=="})
        self.primary = Provider(Response(calls=[(name, {"question": "核对本轮题干"})]), Response("done"))
        self.primary.provider_config = {"modalities": ["text", "tool_use"]}
        result = await self.executor().execute(job)
        sent = self.primary.requests[0]["contexts"][-1]["content"]
        self.assertNotIn("image_url", str(sent))
        self.assertIn("pc_inspect_images", str(sent))
        self.assertIn("本轮新题", str(sent))
        self.assertEqual([name], result["successful_tool_names"])

    async def test_readonly_workspace_path_failure_can_be_corrected_without_fake_receipt(self):
        name = "pc_task_workspace"
        self.tools = {name: SimpleNamespace(name=name)}
        job = self.submit(tool_names=[name], required_tool_names=[name])
        self.primary = Provider(
            Response(calls=[(name, {"action": "read", "path": "wrong.txt"})]),
            Response(calls=[(name, {"action": "list", "path": "."})]),
            Response("done"),
        )
        calls = []
        async def read(job, tool, args):
            calls.append(args)
            return {"status": "error" if len(calls) == 1 else "success", "content": "correct the path" if len(calls) == 1 else "actual listing", "receipt": {"operation_id": str(len(calls))}, "recoverable": True}
        result = await self.executor(execute_tool=read).execute(job)
        self.assertEqual("done", result["text"])
        self.assertEqual(2, len(calls))
        self.assertEqual(1, len(result["receipts"]))
        self.assertEqual("2", result["receipts"][0]["receipt"]["operation_id"])
        self.assertIn("correct the path", str(self.primary.requests[1]["contexts"]))

    async def test_readonly_workspace_corrections_continue_until_model_reports_blocked(self):
        name = "pc_task_workspace"
        self.tools = {name: SimpleNamespace(name=name)}
        job = self.submit(tool_names=[name], required_tool_names=[name], completion_contract="status_v1")
        self.primary = Provider(
            *(Response(calls=[(name, {"action": "list"})]) for _ in range(5)),
            Response('{"task_status":"blocked","text":"核对五次失败，当前路径不可用"}'),
        )
        calls = []
        async def fail(*args):
            calls.append(args)
            return {"status": "error", "content": "bad path", "receipt": {"operation_id": "error"}, "recoverable": True}
        state = await self.run_failure(self.executor(execute_tool=fail), job, "task_incomplete")
        self.assertEqual("failed", state.status)
        self.assertEqual(5, len(calls))
        self.assertEqual(6, len(self.primary.requests))
        self.assertTrue(all(r["tool_choice"] == "auto" for r in self.primary.requests))
        report = next(step for step in state.steps.values() if step["kind"] == "completion_report")
        self.assertEqual([], report["receipts"])

    async def test_required_tool_group_accepts_alternative_with_real_receipt(self):
        job = self.submit(required_tool_names=[], required_tool_groups=[["read", "write"]])
        self.primary = Provider(Response(calls=[("write", {})]), Response("done"))
        result = await self.executor().execute(job)
        self.assertEqual(["write"], result["successful_tool_names"])
        self.assertEqual("auto", self.primary.requests[0]["tool_choice"])
        self.assertEqual("auto", self.primary.requests[1]["tool_choice"])

    async def test_missing_interaction_id_can_be_corrected_without_fake_success(self):
        name = "pc_interaction_channel"
        self.tools = {name: SimpleNamespace(name=name)}
        job = self.submit(tool_names=[name], required_tool_names=[name])
        self.primary = Provider(
            Response(calls=[(name, {"action": "read"})]),
            Response(calls=[(name, {"action": "list"})]),
            Response("done"),
        )
        calls = []
        async def execute(job, tool, args):
            calls.append(args)
            return {"status": "error" if len(calls) == 1 else "success", "content": "channel_id_required: list or open first" if len(calls) == 1 else "channels: []", "receipt": {"operation_id": str(len(calls))}, "recoverable": len(calls) == 1}
        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual(2, len(calls))
        self.assertEqual(["pc_interaction_channel"], result["successful_tool_names"])
        self.assertEqual(1, len(result["receipts"]))
        self.assertEqual("2", result["receipts"][0]["receipt"]["operation_id"])

    async def test_query_then_blocked_is_not_completed_and_preserves_reason(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response(json.dumps({"task_status": "blocked", "text": "只查到了旧提醒，脚本尚未配置"})),
        )
        state = await self.run_failure(self.executor(), job, "task_incomplete")
        self.assertEqual("failed", state.status)
        reports = [x for x in state.steps.values() if x.get("kind") == "completion_report"]
        self.assertEqual("只查到了旧提醒，脚本尚未配置", reports[0]["text"])
        self.assertEqual(1, len(reports[0]["receipts"]))
        self.assertIn("task_status", self.primary.requests[-1]["system_prompt"])

    async def test_structured_completed_returns_chat_text_with_receipts(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response('```json\n{"task_status":"completed","text":"实际结果"}\n```'),
        )
        result = await self.executor().execute(job)
        self.assertEqual("实际结果", result["text"])
        self.assertEqual(["read"], result["successful_tool_names"])

    async def test_structured_needs_input_waits_for_user_without_auto_retry(self):
        job = self.submit(completion_contract="status_v1", mode="reasoning", required_tool_names=[])
        self.primary = Provider(Response('{"task_status":"needs_input","text":"请明确时间"}'))
        state = await self.run_failure(self.executor(), job, "missing_input")
        self.assertEqual("waiting_user", state.status)

    async def test_contract_rejects_unstructured_completion(self):
        job = self.submit(completion_contract="status_v1", mode="reasoning", required_tool_names=[])
        self.primary = Provider(Response("查到了，但还没改"))
        await self.run_failure(self.executor(), job, "protocol_error")

    async def test_model_can_complete_with_tool_outside_suggested_group(self):
        job = self.submit(required_tool_names=[], required_tool_groups=[["write"]], completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response('{"task_status":"completed","text":"查询已满足任务目标"}'),
        )
        result = await self.executor().execute(job)
        self.assertEqual(["read"], result["successful_tool_names"])
        self.assertEqual("tool_receipts", result["verification"])

    async def test_missing_suggested_capability_is_reported_to_model_without_exposure(self):
        job = self.submit(required_tool_names=[], required_tool_groups=[["not_allowed"]], completion_contract="status_v1")
        self.primary = Provider(Response('{"task_status":"blocked","text":"当前工具不能完成操作"}'))
        await self.run_failure(self.executor(), job, "task_incomplete")
        self.assertEqual(1, len(self.primary.requests))
        self.assertEqual({"read", "write"}, {tool.name for tool in self.primary.requests[0]["func_tool"].tools})
        self.assertEqual([], self.executions)

    async def test_invalid_group_schema_is_rejected(self):
        job = self.submit(required_tool_groups=["write"])
        await self.run_failure(self.executor(), job, "missing_input")

    async def test_host_denial_before_execution_preserves_permission_failure(self):
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {})]))

        async def denied(*args):
            raise TaskFailure("permission_denied")

        await self.run_failure(
            self.executor(execute_tool=denied), job, "permission_denied"
        )

    async def test_retry_after_tool_success_resumes_without_repeating_tool(self):
        job = self.submit()
        self.primary = Provider(
            Response(calls=[("read", {})]), RuntimeError("SECRET model key")
        )
        state = await self.run_failure(self.executor(), job, "provider_unavailable")
        self.assertEqual("waiting_retry", state.status)
        self.now += 16
        self.service = TaskService(self.service.path, clock=lambda: self.now)
        resumed = self.service.claim("after-restart")
        self.primary = Provider(Response("verified"))
        result = await self.executor().execute(resumed)
        self.assertEqual("verified", result["text"])
        self.assertEqual(1, len(self.executions))
        self.assertEqual("tool", self.primary.requests[0]["contexts"][-1]["role"])
        self.assertIn(
            "actual result", self.primary.requests[0]["contexts"][-1]["content"]
        )
        with closing(sqlite3.connect(self.service.path)) as db:
            self.assertNotIn(
                "SECRET", repr(db.execute("SELECT * FROM pt_tasks").fetchall())
            )
            self.assertNotIn(
                "SECRET", repr(db.execute("SELECT * FROM pt_outbox").fetchall())
            )

    async def test_structured_model_completion_does_not_force_suggested_tool(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(Response('{"task_status":"completed","text":"现有信息足以回答"}'))
        result = await self.executor().execute(job)
        self.assertEqual("model_report_only", result["verification"])
        self.assertEqual([], result["receipts"])
        self.assertEqual("auto", self.primary.requests[0]["tool_choice"])
        self.assertEqual([], self.executions)

    async def test_no_available_tool_never_falls_back_to_host_tools(self):
        job = self.submit(completion_contract="status_v1")
        self.tools = {}
        self.primary = Provider(Response('{"task_status":"blocked","text":"当前没有可用工具"}'))
        state = await self.run_failure(self.executor(), job, "task_incomplete")
        self.assertEqual("failed", state.status)
        self.assertEqual([], self.primary.requests[0]["func_tool"].tools)
        self.assertEqual("auto", self.primary.requests[0]["tool_choice"])
        self.assertEqual([], self.executions)
        self.assertEqual([], self.permissions)

    async def test_revoked_between_calls_stops_before_second_tool(self):
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {}), ("write", {})]))

        def authorize(job, tool, args):
            return not self.executions

        state = await self.run_failure(
            self.executor(authorize=authorize), job, "permission_denied"
        )
        self.assertEqual("waiting_user", state.status)
        self.assertEqual(1, len(self.executions))

    async def test_cancel_during_provider_discards_late_response(self):
        job = self.submit()

        async def chat(**kwargs):
            self.service.cancel(job.task_id, job.scope)
            return Response(calls=[("read", {})])

        self.primary.text_chat = chat
        state = await self.run_failure(self.executor(), job, "lease_lost")
        self.assertEqual("cancelled", state.status)
        self.assertEqual([], self.executions)

    async def test_cancel_during_tool_cannot_commit_late_receipt(self):
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {})]))

        async def execute(job, tool, args):
            self.service.cancel(job.task_id, job.scope)
            return await self.execute_tool(job, tool, args)

        state = await self.run_failure(
            self.executor(execute_tool=execute), job, "lease_lost"
        )
        self.assertEqual("cancelled", state.status)
        self.assertEqual({}, state.steps)

    async def test_tool_exception_is_returned_to_model_without_blind_retry_or_secret(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("write", {})]),
            Response('{"task_status":"blocked","text":"操作结果不确定，需要核实"}'),
        )

        async def execute(job, tool, args):
            self.executions.append((job.scope, tool.name, args))
            raise RuntimeError("SECRET tool token")

        state = await self.run_failure(
            self.executor(execute_tool=execute), job, "task_incomplete"
        )
        self.assertEqual("failed", state.status)
        self.assertEqual(1, len(self.executions))
        self.assertIn("unknown_result", str(self.primary.requests[1]["contexts"]))
        self.assertNotIn("SECRET", str(self.primary.requests))
        self.assertNotIn("SECRET", str(state.steps))
        self.assertIsNone(self.service.claim("again"))

    async def test_provider_outage_stops_after_three_requests_and_can_be_retried(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        self.primary = Provider(*(RuntimeError("down") for _ in range(3)), Response("recovered"))
        for delay in (15, 60):
            state = await self.run_failure(self.executor(), job, "provider_unavailable")
            self.assertEqual("waiting_retry", state.status)
            self.assertIsNone(self.service.claim("too-early"))
            self.now += delay
            job = self.service.claim("next")
            self.assertIsNotNone(job)
        state = await self.run_failure(self.executor(), job, "provider_unavailable")
        self.assertEqual("failed", state.status)
        self.now += 60
        self.assertIsNone(self.service.claim("no-fourth-request"))
        self.assertTrue(self.service.retry(job.task_id, job.scope))
        job = self.service.claim("user-retry")
        result = await self.executor().execute(job)
        self.assertEqual("recovered", result["text"])
        self.assertEqual(4, len(self.primary.requests))
        self.assertEqual(0, len(self.fallback.requests))

    async def test_only_explicit_reasoning_mode_allows_completion_without_tools(self):
        job = self.submit(required_tool_names=[])
        self.primary = Provider(Response("done"))
        await self.run_failure(self.executor(), job, "missing_evidence")

    async def test_explicit_reasoning_mode_succeeds(self):
        job = self.submit(mode="reasoning", required_tool_names=[], tool_names=[])
        self.primary = Provider(Response("reasoned answer"))
        result = await self.executor().execute(job)
        self.assertEqual("reasoning", result["mode"])
        self.assertEqual([], self.primary.requests[0]["func_tool"].tools)

    async def test_unknown_tool_and_model_claimed_identity_cannot_authorize(self):
        job = self.submit()
        self.primary = Provider(
            Response(calls=[("shell", {"sender_id": "admin", "authorized": True})])
        )
        state = await self.run_failure(self.executor(), job, "permission_denied")
        self.assertEqual("waiting_user", state.status)
        self.assertEqual([], self.executions)

    async def test_model_can_finish_after_more_than_thirty_successful_steps(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            *(Response(calls=[("read", {"index": index})]) for index in range(31)),
            Response('{"task_status":"completed","text":"全部步骤完成"}'),
        )
        self.primary.provider_config = {"max_context_tokens": 65536}
        result = await self.executor().execute(job)
        self.assertEqual(31, len(self.executions))
        self.assertEqual(31, len(result["receipts"]))
        self.assertEqual(31, len(self.service.get(job.task_id, job.scope).steps))

    async def test_non_success_or_missing_receipt_is_unknown_result(self):
        for receipt in (
            {"status": "unknown", "content": "maybe"},
            {"status": "success", "content": "done"},
        ):
            with self.subTest(receipt=receipt):
                self.setUp()
                job = self.submit()
                self.primary = Provider(Response(calls=[("read", {})]))

                async def execute(job, tool, args):
                    return receipt

                state = await self.run_failure(
                    self.executor(execute_tool=execute), job, "unknown_result"
                )
                self.assertEqual("unknown_result", state.status)

    async def test_current_prompt_is_preserved_with_prior_context(self):
        job = self.submit(contexts=[{"role": "user", "content": "previous request"}])
        self.primary = Provider(Response(calls=[("read", {})]), Response("done"))
        await self.executor().execute(job)
        request = self.primary.requests[0]
        messages = request["contexts"] + [{"content": request["prompt"]}]
        self.assertIn("inspect", [m["content"] for m in messages])

    async def test_protocol_correction_informs_model_without_raw_error(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(Response('{"task_status":"SECRET invalid status","text":"invalid"}'))
        await self.run_failure(self.executor(), job, "protocol_error")
        self.now += 16
        job = self.service.claim("retry")
        self.primary = Provider(Response('{"task_status":"completed","text":"已纠正协议"}'))
        await self.executor().execute(job)
        self.assertIn(
            "protocol_error", self.primary.requests[0]["system_prompt"]
        )
        self.assertNotIn("SECRET", str(self.primary.requests))

    async def test_schema_error_is_corrected_by_model_before_tool_execution(self):
        self.tools["read"].parameters = {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        }
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {"path": 123})]),
            Response(calls=[("read", {"path": "fixed"})]),
            Response('{"task_status":"completed","text":"参数已修正并执行"}'),
        )
        result = await self.executor().execute(job)
        self.assertEqual([{"path": "fixed"}], [item[2] for item in self.executions])
        self.assertEqual(1, len(self.permissions))
        self.assertEqual(1, len(result["receipts"]))
        self.assertIn("tool_argument_error", str(self.primary.requests[1]["contexts"]))
        self.assertIn("not_executed", str(self.primary.requests[1]["contexts"]))

    async def test_pending_second_tool_resumes_without_repeating_first(self):
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {}), ("write", {})]))
        state = await self.run_failure(
            self.executor(authorize=lambda job, tool, args: tool.name == "read"),
            job,
            "permission_denied",
        )
        self.assertEqual("waiting_user", state.status)
        self.assertTrue(self.service.resume(job.task_id, job.scope))
        job = self.service.claim("resume")
        self.primary = Provider(Response("done"))
        result = await self.executor().execute(job)
        self.assertEqual(["read", "write"], [x[1] for x in self.executions])
        self.assertEqual(["read", "write"], result["successful_tool_names"])
        self.assertEqual(1, self.service.get(job.task_id, job.scope).attempts)

    async def test_partial_batch_parameter_correction_does_not_repeat_success(self):
        self.tools["write"].parameters = {"type": "object", "required": ["path"]}
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {}), ("write", {})]),
            Response(calls=[("write", {"path": "fixed"})]),
            Response('{"task_status":"completed","text":"保留查询结果后修正写入"}'),
        )
        result = await self.executor().execute(job)
        self.assertEqual(["read", "write"], [x[1] for x in self.executions])
        self.assertEqual(["read", "write"], result["successful_tool_names"])
        self.assertEqual(2, len(result["receipts"]))
        self.assertIn("tool_argument_error", str(self.primary.requests[1]["contexts"]))

    async def test_general_failure_returns_to_model_for_repair(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("write", {"path": "wrong"})]),
            Response(calls=[("read", {"path": "actual"})]),
            Response('{"task_status":"completed","text":"已修正方案并核验"}'),
        )
        async def execute(job, tool, args):
            self.executions.append((job.scope, tool.name, args))
            return {"status": "error" if tool.name == "write" else "success",
                    "content": "script_starts_after_channel_expiry" if tool.name == "write" else "actual state",
                    "receipt": {"operation_id": tool.name}}
        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual(["read"], result["successful_tool_names"])
        self.assertEqual(1, len(result["receipts"]))
        self.assertIn("script_starts_after_channel_expiry", str(self.primary.requests[1]["contexts"]))
        self.assertEqual("running", self.service.get(job.task_id, job.scope).status)

    async def test_model_can_choose_alternative_instead_of_required_tool(self):
        job = self.submit(completion_contract="status_v1", required_tool_names=["read"])
        self.primary = Provider(
            Response(calls=[("write", {})]),
            Response('{"task_status":"completed","text":"使用替代工具完成并核验"}'),
        )
        result = await self.executor().execute(job)
        self.assertEqual(["write"], result["successful_tool_names"])
        self.assertTrue(all(r["tool_choice"] == "auto" for r in self.primary.requests))

    async def test_host_dynamic_catalog_refreshes_before_each_model_request(self):
        job = self.submit(
            completion_contract="status_v1", dynamic_tool_catalog=True,
            tool_names=["read"],
        )
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response(calls=[("write", {})]),
            Response('{"task_status":"completed","text":"按当前目录完成"}'),
        )
        catalog_requests = []

        def resolve(job, names):
            catalog_requests.append(names)
            return self.tools if self.executions else {"read": self.tools["read"]}

        result = await self.executor(resolve_tools=resolve).execute(job)
        self.assertTrue(all(names is None for names in catalog_requests))
        self.assertGreaterEqual(len(catalog_requests), 3)
        self.assertEqual(
            ["pc_task_read_steps", "read"], [tool.name for tool in self.primary.requests[0]["func_tool"].tools]
        )
        self.assertEqual(
            {"pc_task_read_steps", "read", "write"},
            {tool.name for tool in self.primary.requests[1]["func_tool"].tools},
        )
        self.assertEqual(["read", "write"], [item[1] for item in self.permissions])
        self.assertEqual(["read", "write"], result["successful_tool_names"])

    async def test_injected_executor_without_host_flag_keeps_names_allowlist(self):
        job = self.submit(tool_names=["read"])
        self.primary = Provider(Response(calls=[("write", {})]))
        catalog_requests = []

        def resolve(job, names):
            catalog_requests.append(names)
            return self.tools

        await self.run_failure(
            self.executor(resolve_tools=resolve), job, "permission_denied"
        )
        self.assertTrue(all(names == ("read",) for names in catalog_requests))
        self.assertEqual(
            ["read"], [tool.name for tool in self.primary.requests[0]["func_tool"].tools]
        )
        self.assertEqual([], self.executions)
        self.assertEqual([], self.permissions)

    async def test_model_can_report_blocked_after_ordinary_error(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response('{"task_status":"blocked","text":"核对失败原因后决定暂缓"}'),
        )
        async def fail(*args):
            return {"status": "error", "content": "known failure", "receipt": {"observed": True}}
        state = await self.run_failure(self.executor(execute_tool=fail), job, "task_incomplete")
        reports = [s for s in state.steps.values() if s.get("kind") == "completion_report"]
        self.assertEqual("核对失败原因后决定暂缓", reports[0]["text"])
        self.assertEqual([], reports[0]["receipts"])

    async def test_failure_replans_before_remaining_batch_operations(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("write", {"path": "bad"}), ("write", {"path": "stale"})]),
            Response(calls=[("read", {})]),
            Response('{"task_status":"completed","text":"已核对当前结果"}'),
        )
        async def execute(job, tool, args):
            self.executions.append((job.scope, tool.name, args))
            return {"status": "error" if tool.name == "write" else "success",
                    "content": "invalid schedule" if tool.name == "write" else "verified",
                    "receipt": {"operation_id": str(len(self.executions))}}
        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual(["write", "read"], [x[1] for x in self.executions])
        self.assertIn("not_executed", str(self.primary.requests[1]["contexts"]))
        self.assertEqual(1, len(result["receipts"]))

    async def test_error_checkpoint_survives_model_outage_without_replaying_tool(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(Response(calls=[("write", {})]), RuntimeError("backend unavailable"))
        async def execute(job, tool, args):
            self.executions.append((job.scope, tool.name, args))
            return {"status": "error" if tool.name == "write" else "success", "content": "known failure", "receipt": {"observed": True}}
        await self.run_failure(self.executor(execute_tool=execute), job, "provider_unavailable")
        self.now += 16
        resumed = self.service.claim("resume-model")
        self.primary = Provider(
            Response(calls=[("read", {})]),
            Response('{"task_status":"completed","text":"恢复后完成核验"}'),
        )
        result = await self.executor(execute_tool=execute).execute(resumed)
        self.assertEqual(["write", "read"], [x[1] for x in self.executions])
        self.assertEqual(["read"], result["successful_tool_names"])
        self.assertIn("known failure", str(self.primary.requests[0]["contexts"]))

    async def test_long_task_keeps_running_and_compacts_complete_rounds(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            *(Response(calls=[("read", {"index": i})]) for i in range(70)),
            Response('{"task_status":"completed","text":"长任务完成"}'),
        )
        original_chat = self.primary.text_chat

        async def with_summaries(**kwargs):
            if "请生成当前后台任务" in kwargs.get("system_prompt", ""):
                self.primary.requests.append(kwargs)
                return Response("Earlier read operations returned actual results; retain their receipts in durable steps.")
            return await original_chat(**kwargs)

        self.primary.text_chat = with_summaries
        result = await self.executor().execute(job)
        self.assertEqual(70, len(result["receipts"]))
        self.assertEqual(70, len(self.executions))
        final_context = self.primary.requests[-1]["contexts"]
        self.assertIn("inspect", [m.get("content") for m in final_context])
        ids = set()
        for message in final_context:
            if message["role"] == "assistant":
                ids.update(call["id"] for call in message.get("tool_calls", []))
            elif message["role"] == "tool":
                self.assertIn(message["tool_call_id"], ids)

    async def test_bad_arguments_can_be_corrected_without_scheduler_failure(self):
        self.tools["read"].parameters = {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("read", {"path": 123})]),
            Response(calls=[("read", {"path": "valid"})]),
            Response('{"task_status":"completed","text":"已纠正参数"}'),
        )
        result = await self.executor().execute(job)
        self.assertEqual(1, len(self.executions))
        self.assertEqual(1, len(result["receipts"]))
        self.assertIn("tool_argument_error", str(self.primary.requests[1]["contexts"]))

    async def test_uncertain_tool_result_is_returned_for_model_verification(self):
        job = self.submit(completion_contract="status_v1")
        self.primary = Provider(
            Response(calls=[("write", {})]),
            Response(calls=[("read", {})]),
            Response('{"task_status":"completed","text":"查询确认操作已生效"}'),
        )
        async def execute(job, tool, args):
            self.executions.append((job.scope, tool.name, args))
            if tool.name == "write":
                raise TaskFailure("unknown_result")
            return {"status": "success", "content": "confirmed actual state", "receipt": {"observation_id": "verified"}}
        result = await self.executor(execute_tool=execute).execute(job)
        self.assertEqual(["write", "read"], [x[1] for x in self.executions])
        self.assertEqual(["read"], result["successful_tool_names"])
        self.assertIn("unknown_result", str(self.primary.requests[1]["contexts"]))

    async def test_oversized_context_rejected_before_provider_or_tool(self):
        job = self.submit(contexts=[{"role": "user", "content": "x" * 131072}])
        self.primary.provider_config = {"max_context_tokens": 8192}
        await self.run_failure(self.executor(), job, "context_limit")
        self.assertEqual([], self.primary.requests)
        self.assertEqual([], self.executions)

    async def test_malformed_batch_is_rejected_before_any_side_effect(self):
        job = self.submit()
        response = Response(calls=[("read", {}), ("write", {})])
        calls = response.to_openai_tool_calls()
        del calls[1]["id"]
        response.to_openai_tool_calls = lambda: calls
        self.primary = Provider(response)
        state = await self.run_failure(self.executor(), job, "protocol_error")
        self.assertEqual("waiting_retry", state.status)
        self.assertEqual([], self.executions)

    async def test_custom_extra_body_output_allowance_is_reserved(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        self.primary.provider_config = {"max_context_tokens": 8192, "custom_extra_body": {"max_completion_tokens": 8192}}
        state = await self.run_failure(self.executor(), job, "context_limit")
        self.assertEqual("waiting_user", state.status)
        self.assertEqual([], self.primary.requests)


if __name__ == "__main__":
    unittest.main()
