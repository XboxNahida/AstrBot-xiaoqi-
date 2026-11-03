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


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual("required", self.primary.requests[0]["tool_choice"])
        self.assertEqual("auto", self.primary.requests[1]["tool_choice"])
        self.assertTrue(
            all(r["request_max_retries"] == 0 for r in self.primary.requests)
        )
        self.assertEqual(self.scope, self.executions[0][0])
        self.assertEqual(1, len(self.permissions))
        self.assertEqual(1, self.service.get(job.task_id, job.scope).attempts)

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
        self.fallback = Provider(Response("verified"))
        result = await self.executor().execute(resumed)
        self.assertEqual("verified", result["text"])
        self.assertEqual(1, len(self.executions))
        self.assertEqual("tool", self.fallback.requests[0]["contexts"][-1]["role"])
        self.assertIn(
            "actual result", self.fallback.requests[0]["contexts"][-1]["content"]
        )
        with closing(sqlite3.connect(self.service.path)) as db:
            self.assertNotIn(
                "SECRET", repr(db.execute("SELECT * FROM pt_tasks").fetchall())
            )
            self.assertNotIn(
                "SECRET", repr(db.execute("SELECT * FROM pt_outbox").fetchall())
            )

    async def test_missing_required_tool_only_one_correction(self):
        job = self.submit()
        self.primary = Provider(Response("done"))
        state = await self.run_failure(self.executor(), job, "missing_required_tool")
        self.assertEqual("waiting_retry", state.status)
        self.now += 16
        job = self.service.claim("retry")
        self.fallback = Provider(Response("done again"))
        state = await self.run_failure(self.executor(), job, "missing_required_tool")
        self.assertEqual("failed", state.status)
        self.assertEqual([], self.executions)

    async def test_no_available_tool_never_falls_back_to_host_tools(self):
        job = self.submit()
        self.tools = {}
        state = await self.run_failure(self.executor(), job, "permission_denied")
        self.assertEqual("waiting_user", state.status)
        self.assertEqual([], self.primary.requests)

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

    async def test_tool_exception_is_unknown_result_and_never_automatic_retry(self):
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {})]))

        async def execute(job, tool, args):
            raise RuntimeError("SECRET tool token")

        state = await self.run_failure(
            self.executor(execute_tool=execute), job, "unknown_result"
        )
        self.assertEqual("unknown_result", state.status)
        self.assertIsNone(self.service.claim("again"))

    async def test_provider_budget_is_shared_by_primary_and_fallback(self):
        job = self.submit(mode="reasoning", required_tool_names=[])
        self.primary = Provider(RuntimeError("down"))
        self.fallback = Provider(RuntimeError("down"), RuntimeError("down"))
        for delay in (16, 61, 0):
            state = await self.run_failure(self.executor(), job, "provider_unavailable")
            self.now += delay
            job = self.service.claim("next")
        self.assertEqual("failed", state.status)
        self.assertEqual(1, len(self.primary.requests))
        self.assertEqual(2, len(self.fallback.requests))
        self.assertIsNone(job)

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

    async def test_thirty_successful_steps_are_an_absolute_bound(self):
        job = self.submit()
        self.primary = Provider(*[Response(calls=[("read", {})]) for _ in range(31)])
        state = await self.run_failure(self.executor(), job, "step_limit")
        self.assertEqual(30, len(self.executions))
        self.assertEqual(30, len(state.steps))

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
        job = self.submit()
        self.primary = Provider(Response("done"))
        await self.run_failure(self.executor(), job, "missing_required_tool")
        self.now += 16
        job = self.service.claim("retry")
        self.fallback = Provider(Response(calls=[("read", {})]), Response("done"))
        self.primary = Provider(Response("done"))
        await self.executor().execute(job)
        self.assertIn(
            "missing_required_tool", self.fallback.requests[0]["system_prompt"]
        )

    async def test_schema_error_corrects_once_without_invoking_tool(self):
        self.tools["read"].parameters = {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string"}},
        }
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {"path": 123})]))
        state = await self.run_failure(self.executor(), job, "tool_argument_error")
        self.assertEqual("waiting_retry", state.status)
        self.assertEqual([], self.executions)

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
        job = self.submit()
        self.primary = Provider(Response(calls=[("read", {}), ("write", {})]))
        await self.run_failure(self.executor(), job, "tool_argument_error")
        self.now += 16
        job = self.service.claim("correct")
        self.primary = Provider(
            Response(calls=[("write", {"path": "fixed"})]), Response("done")
        )
        result = await self.executor().execute(job)
        self.assertEqual(["read", "write"], [x[1] for x in self.executions])
        self.assertEqual(["read", "write"], result["successful_tool_names"])

    async def test_oversized_context_rejected_before_provider_or_tool(self):
        job = self.submit(contexts=[{"role": "user", "content": "x" * 131072}])
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


if __name__ == "__main__":
    unittest.main()
