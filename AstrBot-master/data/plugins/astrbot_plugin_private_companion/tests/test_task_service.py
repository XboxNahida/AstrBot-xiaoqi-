import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from task_service import TaskFailure, TaskScheduler, TaskScope, TaskService


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "conversation_tasks.db"
        self.clock = Clock()
        self.scope = TaskScope("bot", "group", "account")
        self.service = TaskService(self.path, clock=self.clock, lease_seconds=30)

    def submit(self, fragment="first", message="message"):
        return self.service.submit(
            self.scope,
            message,
            fragment,
            "Inspect release notes",
            self.temp.name,
            metadata={"schedule_rule_id": "existing-cron-id"},
        )

    def test_restart_deduplicates_source_but_keeps_fragments_and_identity(self):
        first = self.submit()
        service = TaskService(self.path, clock=self.clock)
        same = service.submit(
            self.scope, "message", "first", "Reworded goal", self.temp.name
        )
        self.assertEqual(first.task_id, same.task_id)
        self.assertNotEqual(first.task_id, self.submit("second").task_id)
        self.assertEqual(2, len(service.list(self.scope)))
        for field in ("bot_id", "session_key", "sender_id"):
            wrong = replace(self.scope, **{field: "other"})
            self.assertIsNone(service.get(first.task_id, wrong))
            self.assertEqual([], service.list(wrong))
            self.assertFalse(service.cancel(first.task_id, wrong))
        self.assertEqual(
            "existing-cron-id",
            service.get(first.task_id, self.scope).metadata["schedule_rule_id"],
        )

    def test_claim_is_single_concurrency_and_lease_fenced_after_restart(self):
        task = self.submit()
        self.submit("second")
        first = self.service.claim("one")
        second = TaskService(self.path, clock=self.clock)
        self.assertIsNone(second.claim("two"))
        self.assertTrue(self.service.heartbeat(first))
        self.clock.now += 31
        self.assertFalse(self.service.finish(first, {"receipt": "late"}))
        self.assertEqual(1, second.recover_expired())
        self.assertEqual("unknown_result", second.get(task.task_id, self.scope).status)
        self.assertFalse(second.resume(task.task_id, self.scope))
        self.assertFalse(second.retry(task.task_id, self.scope))
        self.assertIsNone(second.claim("two"))
        self.assertTrue(second.ack_control(first))
        self.assertEqual("second", second.claim("two").fragment_key)

    def test_cancel_rejects_late_results_progress_and_heartbeat(self):
        task = self.submit()
        job = self.service.claim("one")
        self.assertTrue(self.service.cancel(task.task_id, self.scope))
        self.assertFalse(self.service.finish(job, {"receipt": "late"}))
        self.assertFalse(self.service.fail(job, "network", retryable=True))
        self.assertFalse(self.service.record_step(job, "main", {"receipt": "late"}))
        self.assertFalse(self.service.heartbeat(job))
        self.assertFalse(self.service.progress(job, "late"))
        self.assertEqual("cancelled", self.service.get(task.task_id, self.scope).status)

    def test_retry_budget_counts_primary_and_fallback_and_backoff(self):
        task = self.submit()
        job = self.service.claim("one")
        self.assertEqual(1, job.attempts)
        self.assertTrue(self.service.fail(job, "network", retryable=True))
        self.assertIsNone(self.service.claim("two"))
        self.clock.now += 15
        job = self.service.claim("two")
        self.assertEqual(2, job.attempts)
        self.assertTrue(self.service.fail(job, "rate_limit", retryable=True))
        self.clock.now += 59
        self.assertIsNone(self.service.claim("three"))
        self.clock.now += 1
        job = self.service.claim("three")
        self.assertEqual(3, job.attempts)
        self.assertFalse(self.service.begin_attempt(job, "fallback"))
        self.assertTrue(self.service.fail(job, "network", retryable=True))
        self.assertEqual("failed", self.service.get(task.task_id, self.scope).status)
        self.assertTrue(self.service.retry(task.task_id, self.scope))
        job = self.service.claim("manual")
        self.assertEqual(1, job.attempts)
        self.assertTrue(self.service.begin_attempt(job, "fallback"))
        self.assertEqual(2, self.service.get(task.task_id, self.scope).attempts)

    def test_step_checkpoints_survive_retry_and_require_evidence(self):
        task = self.submit()
        job = self.service.claim("one")
        with self.assertRaises(ValueError):
            self.service.record_step(job, "main", {})
        self.assertTrue(
            self.service.record_step(
                job, "main", {"receipt": "file hash"}, next_step="verify"
            )
        )
        self.assertFalse(
            self.service.record_step(job, "main", {"receipt": "duplicate"})
        )
        self.assertTrue(self.service.begin_attempt(job, "primary"))
        self.service.fail(job, "network", retryable=True)
        self.clock.now += 15
        restarted = TaskService(self.path, clock=self.clock)
        job = restarted.claim("two")
        self.assertEqual("verify", job.step_key)
        self.assertEqual(["main"], list(job.steps))
        self.assertEqual(2, job.attempts)
        self.assertTrue(restarted.finish(job, {"receipt": "verified"}))
        self.assertEqual("succeeded", restarted.get(task.task_id, self.scope).status)

    def test_permission_pause_and_nonretryable_failure_are_not_auto_executed(self):
        task = self.submit()
        self.assertTrue(self.service.pause(task.task_id, self.scope))
        self.assertIsNone(self.service.claim("worker"))
        self.assertTrue(self.service.resume(task.task_id, self.scope))
        job = self.service.claim("worker")
        self.assertTrue(self.service.fail(job, "permission_denied", retryable=False))
        self.assertEqual(
            "waiting_user", self.service.get(task.task_id, self.scope).status
        )
        self.assertIsNone(self.service.claim("worker"))
        self.assertTrue(self.service.resume(task.task_id, self.scope))
        job = self.service.claim("worker")
        self.service.fail(job, "protocol", retryable=False)
        self.assertEqual("failed", self.service.get(task.task_id, self.scope).status)

    def test_running_pause_requires_reconciliation_before_resume(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.assertTrue(self.service.pause(task.task_id, self.scope))
        self.assertEqual(
            "unknown_result", self.service.get(task.task_id, self.scope).status
        )
        self.assertFalse(self.service.resume(task.task_id, self.scope))
        self.assertFalse(self.service.finish(job, {"receipt": "late"}))

    def test_latest_list_limits_before_returning_newest_tasks(self):
        tasks = [self.submit(str(index)) for index in range(205)]
        self.assertEqual(tasks[:2], self.service.list(self.scope, limit=2))
        self.assertEqual(
            [task.task_id for task in reversed(tasks[-200:])],
            [
                task.task_id
                for task in self.service.list(self.scope, limit=300, latest=True)
            ],
        )

    def test_reconcile_requires_verified_outcome_and_preserves_attempt_budget(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.service.pause(task.task_id, self.scope)
        self.service.ack_control(job)
        for outcome, evidence in (("unknown", {"receipt": "?"}), ("completed", {})):
            with self.assertRaises(ValueError):
                self.service.reconcile(task.task_id, self.scope, outcome, evidence)
        wrong = replace(self.scope, sender_id="another")
        self.assertFalse(
            self.service.reconcile(
                task.task_id, wrong, "completed", {"receipt": "verified"}
            )
        )
        self.assertTrue(
            self.service.reconcile(
                task.task_id,
                self.scope,
                "not_executed",
                {"receipt": "provider confirmed no operation"},
            )
        )
        state = self.service.get(task.task_id, self.scope)
        self.assertEqual("paused", state.status)
        self.assertEqual(job.attempts, state.attempts)
        self.assertIsNone(self.service.claim("worker"))
        self.assertFalse(self.service.finish(job, {"receipt": "stale"}))
        self.assertTrue(self.service.resume(task.task_id, self.scope))
        job = self.service.claim("worker")
        self.service.fail(job, "unknown_result")
        receipt = {"receipt": "provider verified committed"}
        self.assertTrue(
            self.service.reconcile(task.task_id, self.scope, "completed", receipt)
        )
        state = self.service.get(task.task_id, self.scope)
        self.assertEqual("succeeded", state.status)
        self.assertEqual(receipt, state.result)
        self.assertFalse(
            self.service.reconcile(task.task_id, self.scope, "not_executed", receipt)
        )
        self.assertFalse(self.service.resume(task.task_id, self.scope))
        self.assertIsNone(self.service.claim("worker"))

    def test_reconcile_not_executed_rejects_existing_current_step_receipt(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.service.record_step(job, "main", {"receipt": "committed"})
        self.service.fail(job, "unknown_result")
        self.assertFalse(
            self.service.reconcile(
                task.task_id, self.scope, "not_executed", {"receipt": "contradiction"}
            )
        )
        self.assertEqual(
            "unknown_result", self.service.get(task.task_id, self.scope).status
        )

    def test_goal_revision_keeps_identity_and_cannot_overwrite_unsafe_states(self):
        task = self.submit()
        self.assertTrue(
            self.service.revise_goal(
                task.task_id, self.scope, "Read only the newest release"
            )
        )
        state = self.service.get(task.task_id, self.scope)
        self.assertEqual(task.task_id, state.task_id)
        self.assertEqual("Read only the newest release", state.goal)
        self.assertGreater(state.revision, task.revision)
        self.assertFalse(
            self.service.revise_goal(
                task.task_id, replace(self.scope, bot_id="another"), "Other"
            )
        )
        with self.assertRaises(ValueError):
            self.service.revise_goal(task.task_id, self.scope, " ")
        self.service.pause(task.task_id, self.scope)
        self.assertTrue(
            self.service.revise_goal(task.task_id, self.scope, "Revised while paused")
        )
        self.service.resume(task.task_id, self.scope)
        job = self.service.claim("worker")
        self.assertFalse(self.service.revise_goal(task.task_id, self.scope, "Unsafe"))
        self.service.pause(task.task_id, self.scope)
        self.assertFalse(
            self.service.revise_goal(task.task_id, self.scope, "Still unsafe")
        )
        self.service.cancel(task.task_id, self.scope)
        self.assertFalse(self.service.revise_goal(task.task_id, self.scope, "Reopen"))
        self.assertFalse(self.service.finish(job, {"receipt": "late"}))

    def test_completed_goal_cannot_be_revised_or_reopened(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.service.finish(job, {"receipt": "done"})
        self.assertFalse(self.service.revise_goal(task.task_id, self.scope, "Reopen"))
        self.assertEqual("succeeded", self.service.get(task.task_id, self.scope).status)

    def test_goal_revision_rejects_existing_checkpoints_and_oversized_goal(self):
        task = self.submit()
        with self.assertRaises(ValueError):
            self.service.revise_goal(task.task_id, self.scope, "x" * 4001)
        job = self.service.claim("worker")
        self.service.record_step(job, "main", {"receipt": "done"}, next_step="verify")
        self.service.fail(job, "network", retryable=True)
        self.service.pause(task.task_id, self.scope)
        self.assertFalse(
            self.service.revise_goal(task.task_id, self.scope, "Different goal")
        )
        self.assertEqual(task.goal, self.service.get(task.task_id, self.scope).goal)

    def test_notification_order_dedup_and_completion_discards_progress(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.assertTrue(
            self.service.progress(job, "phase one", content_fingerprint="hash")
        )
        self.assertFalse(
            self.service.progress(job, "phase one", content_fingerprint="hash")
        )
        accepted = self.service.claim_notification("sender")
        self.assertEqual("accepted", accepted.kind)
        self.assertIsNone(self.service.claim_notification("other"))
        self.assertTrue(
            self.service.fail_notification(accepted, "temporary", retryable=True)
        )
        self.assertIsNone(self.service.claim_notification("other"))
        self.service.finish(job, {"receipt": "done"})
        self.clock.now += 15
        accepted = self.service.claim_notification("sender")
        self.assertTrue(self.service.ack_notification(accepted))
        result = self.service.claim_notification("sender")
        self.assertEqual("result", result.kind)
        self.assertGreater(result.revision, accepted.revision)
        self.assertTrue(
            self.service.fail_notification(result, "ambiguous", retryable=False)
        )
        self.assertIsNone(self.service.claim_notification("other"))
        self.assertEqual("succeeded", self.service.get(task.task_id, self.scope).status)

    def test_expired_delivery_becomes_unknown_and_blocks_later_notification(self):
        self.submit()
        accepted = self.service.claim_notification("sender")
        self.clock.now += 31
        self.service.recover_expired()
        self.assertFalse(self.service.ack_notification(accepted))
        self.assertIsNone(self.service.claim_notification("another"))

    def test_simultaneous_workers_only_one_claims_task(self):
        self.submit()
        other = TaskService(self.path, clock=self.clock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(service.claim, "worker")
                for service in (self.service, other)
            ]
            self.assertEqual(1, sum(future.result() is not None for future in futures))

    def test_protocol_error_gets_only_one_correction(self):
        task = self.submit()
        job = self.service.claim("one")
        self.service.fail(job, "missing_required_tool", retryable=True)
        self.clock.now += 15
        job = self.service.claim("two")
        self.service.fail(job, "missing_required_tool", retryable=True)
        self.assertEqual("failed", self.service.get(task.task_id, self.scope).status)

    def test_completed_current_step_is_not_automatically_repeated(self):
        task = self.submit()
        job = self.service.claim("worker")
        self.service.record_step(job, "main", {"receipt": "side effect committed"})
        self.service.fail(job, "network", retryable=True)
        self.clock.now += 100
        self.assertIsNone(self.service.claim("another"))
        self.assertEqual(
            "unknown_result", self.service.get(task.task_id, self.scope).status
        )

    def test_failed_inflight_progress_is_not_requeued_after_completion(self):
        self.submit()
        job = self.service.claim("worker")
        accepted = self.service.claim_notification("sender")
        self.service.ack_notification(accepted)
        self.service.progress(job, "running")
        progress = self.service.claim_notification("sender")
        self.service.finish(job, {"receipt": "done"})
        self.assertTrue(
            self.service.fail_notification(progress, "not_sent", retryable=True)
        )
        result = self.service.claim_notification("sender")
        self.assertIsNotNone(result)
        self.assertEqual("result", result.kind)

    def test_superseded_progress_is_not_delivered_or_retried(self):
        self.submit()
        job = self.service.claim("worker")
        self.service.ack_notification(self.service.claim_notification("sender"))
        self.service.progress(job, "old progress")
        old = self.service.claim_notification("sender")
        self.service.progress(job, "new progress")
        self.service.fail_notification(old, "not_sent", retryable=True)
        newest = self.service.claim_notification("sender")
        self.assertIsNotNone(newest)
        self.assertEqual("new progress", newest.payload["text"])
        self.service.ack_notification(newest)
        self.service.progress(job, "pending progress before retry")
        self.service.fail(job, "network", retryable=True)
        self.clock.now += 15
        self.assertIsNotNone(self.service.claim("worker"))
        self.assertIsNone(self.service.claim_notification("sender"))

    def test_inflight_progress_cannot_retry_into_a_new_execution_attempt(self):
        self.submit()
        job = self.service.claim("worker")
        self.service.ack_notification(self.service.claim_notification("sender"))
        self.service.progress(job, "first attempt")
        progress = self.service.claim_notification("sender")
        self.service.fail(job, "network", retryable=True)
        self.clock.now += 15
        self.service.claim("another")
        self.assertTrue(
            self.service.fail_notification(progress, "not_sent", retryable=True)
        )
        self.clock.now += 15
        self.assertIsNone(self.service.claim_notification("sender"))

    def test_pending_progress_from_before_a_checkpoint_is_not_delivered(self):
        self.submit()
        job = self.service.claim("worker")
        self.service.ack_notification(self.service.claim_notification("sender"))
        self.service.progress(job, "main is running")
        self.service.record_step(job, "main", {"receipt": "done"}, next_step="verify")
        self.assertIsNone(self.service.claim_notification("sender"))


class SchedulerTests(unittest.TestCase):
    setUp = TaskServiceTests.setUp
    submit = TaskServiceTests.submit

    def test_progress_delay_default_range_and_disabled_values(self):
        scheduler = TaskScheduler(self.service, None, None)
        self.assertEqual(90, scheduler.progress_after_seconds)
        for value in (0, None, 120):
            TaskScheduler(self.service, None, None, progress_after_seconds=value)
        for value in (-1, 121, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                TaskScheduler(self.service, None, None, progress_after_seconds=value)

    def test_progress_delay_sends_once_only_while_execution_is_running(self):
        async def scenario():
            self.submit()
            self.service.ack_notification(self.service.claim_notification("sender"))
            entered = asyncio.Event()
            release = asyncio.Event()

            async def execute(job):
                entered.set()
                await release.wait()
                return {"receipt": "done"}

            scheduler = TaskScheduler(
                self.service, execute, lambda job: True, progress_after_seconds=0.01
            )
            running = asyncio.create_task(scheduler.schedule_once())
            await entered.wait()
            await asyncio.sleep(0.04)
            progress = self.service.claim_notification("sender")
            self.assertIsNotNone(progress)
            self.assertEqual("progress", progress.kind)
            self.service.ack_notification(progress)
            await asyncio.sleep(0.03)
            self.assertIsNone(self.service.claim_notification("sender"))
            release.set()
            await running
            result = self.service.claim_notification("sender")
            self.assertEqual("result", result.kind)
            self.service.ack_notification(result)
            await asyncio.sleep(0.02)
            self.assertIsNone(self.service.claim_notification("sender"))

        asyncio.run(scenario())

    def test_short_completed_and_disabled_tasks_never_emit_progress(self):
        async def scenario(delay, wait):
            task = self.submit(str(delay))
            self.service.ack_notification(self.service.claim_notification("sender"))

            async def execute(job):
                await asyncio.sleep(wait)
                return {"receipt": "done"}

            scheduler = TaskScheduler(
                self.service, execute, lambda job: True, progress_after_seconds=delay
            )
            await scheduler.schedule_once()
            await asyncio.sleep(0.03)
            result = self.service.claim_notification("sender")
            self.assertEqual("result", result.kind)
            self.service.ack_notification(result)
            self.assertIsNone(self.service.claim_notification("sender"))
            self.assertEqual(
                "succeeded", self.service.get(task.task_id, self.scope).status
            )

        for delay, wait in ((0.02, 0), (0, 0.02), (None, 0.02)):
            asyncio.run(scenario(delay, wait))

    def test_scheduler_success_failure_and_authorization(self):
        async def scenario():
            task = self.submit()
            calls = []

            async def execute(job):
                calls.append(job.task_id)
                return {"receipt": "real result"}

            scheduler = TaskScheduler(self.service, execute, lambda job: False)
            self.assertTrue(await scheduler.schedule_once())
            self.assertEqual([], calls)
            self.assertEqual(
                "waiting_user", self.service.get(task.task_id, self.scope).status
            )
            self.service.resume(task.task_id, self.scope)
            scheduler.authorize = lambda job: True
            self.assertTrue(await scheduler.schedule_once())
            self.assertEqual([task.task_id], calls)
            self.assertEqual(
                "succeeded", self.service.get(task.task_id, self.scope).status
            )
            self.assertFalse(await scheduler.schedule_once())

        asyncio.run(scenario())

    def test_worker_loop_and_unclassified_failure(self):
        async def scenario():
            task = self.submit()
            stop = asyncio.Event()

            async def execute(job):
                stop.set()
                raise RuntimeError("Unknown external result")

            await TaskScheduler(self.service, execute, lambda job: True).worker_loop(
                stop
            )
            self.assertEqual(
                "unknown_result", self.service.get(task.task_id, self.scope).status
            )
            self.assertIsNone(self.service.claim("other"))

        asyncio.run(scenario())

    def test_heartbeat_cancels_execution_after_controlled_cancel(self):
        async def scenario():
            service = TaskService(self.path, clock=self.clock, lease_seconds=0.03)
            task = self.submit()
            entered = asyncio.Event()

            async def execute(job):
                entered.set()
                await asyncio.Event().wait()

            scheduler = TaskScheduler(service, execute, lambda job: True)
            running = asyncio.create_task(scheduler.schedule_once())
            await entered.wait()
            service.cancel(task.task_id, self.scope)
            self.assertTrue(await asyncio.wait_for(running, 1))
            self.assertEqual("cancelled", service.get(task.task_id, self.scope).status)

        asyncio.run(scenario())

    def test_scheduler_cancellation_and_exception_always_release_lease(self):
        async def scenario():
            task = self.submit()

            async def failure(job):
                raise TaskFailure("network", retryable=True)

            scheduler = TaskScheduler(self.service, failure, lambda job: True)
            await scheduler.schedule_once()
            self.assertEqual(
                "waiting_retry", self.service.get(task.task_id, self.scope).status
            )
            self.clock.now += 15

            async def interrupted(job):
                raise asyncio.CancelledError

            scheduler.execute = interrupted
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.schedule_once()
            self.assertEqual(
                "unknown_result", self.service.get(task.task_id, self.scope).status
            )

        asyncio.run(scenario())

    def test_scheduler_missing_receipt_is_protocol_failure(self):
        async def scenario():
            task = self.submit()

            async def execute(job):
                return {}

            await TaskScheduler(self.service, execute, lambda job: True).schedule_once()
            state = self.service.get(task.task_id, self.scope)
            self.assertEqual("unknown_result", state.status)
            self.assertEqual("unknown_result", state.error_kind)
            self.assertFalse(self.service.retry(task.task_id, self.scope))

        asyncio.run(scenario())

    def test_cancelled_execution_keeps_capacity_until_cleanup_acknowledged(self):
        async def scenario():
            service = TaskService(self.path, clock=self.clock, lease_seconds=0.03)
            first = self.submit()
            self.submit("second")
            entered, cleanup, release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )

            async def execute(job):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()

            pending = asyncio.create_task(
                TaskScheduler(service, execute, lambda job: True).schedule_once()
            )
            await entered.wait()
            try:
                service.cancel(first.task_id, self.scope)
                self.assertIsNone(service.claim("other"))
                await asyncio.wait_for(cleanup.wait(), 1)
                self.assertIsNone(service.claim("other"))
            finally:
                release.set()
                await asyncio.wait_for(pending, 1)
            self.assertEqual("second", service.claim("other").fragment_key)

        asyncio.run(scenario())

    def test_expired_worker_capacity_requires_cleanup_or_reconciliation(self):
        first = self.submit()
        self.submit("second")
        job = self.service.claim("worker")
        self.clock.now += 31
        self.service.recover_expired()
        self.assertIsNone(self.service.claim("other"))
        self.assertFalse(self.service.ack_control(replace(job, lease_token="wrong")))
        self.assertTrue(self.service.ack_control(job))
        self.assertEqual(
            "unknown_result", self.service.get(first.task_id, self.scope).status
        )
        self.assertEqual("second", self.service.claim("other").fragment_key)

    def test_reconciliation_cannot_release_unstopped_worker(self):
        first = self.submit()
        self.submit("second")
        self.service.claim("worker")
        self.service.pause(first.task_id, self.scope)
        evidence = {"receipt": "operator verified outcome"}
        self.assertFalse(
            self.service.reconcile(first.task_id, self.scope, "completed", evidence)
        )
        self.assertIsNone(self.service.claim("other"))
        self.assertTrue(
            self.service.reconcile(
                first.task_id, self.scope, "completed", evidence, worker_stopped=True
            )
        )
        self.assertEqual("second", self.service.claim("other").fragment_key)
