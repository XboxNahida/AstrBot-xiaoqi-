"""Persist task ownership, request history, checkpoints, and delivery separately."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    from .task_model_policy import request_policy
except ImportError:
    from task_model_policy import request_policy


@dataclass(frozen=True)
class TaskScope:
    bot_id: str
    session_key: str
    sender_id: str


@dataclass(frozen=True)
class TaskJob:
    task_id: str
    scope: TaskScope
    origin_message_id: str
    fragment_key: str
    goal: str
    workdir: str
    status: str
    revision: int
    step_key: str
    attempts: int
    lease_token: str | None
    lease_until: float | None
    error_kind: str | None
    metadata: dict[str, Any]
    steps: dict[str, Any]
    result: dict[str, Any] | None
    created_at: float | None = None
    updated_at: float | None = None
    finished_at: float | None = None


@dataclass(frozen=True)
class Notification:
    notification_id: int
    task_id: str
    scope: TaskScope
    revision: int
    kind: str
    payload: dict[str, Any]
    lease_token: str


class TaskFailure(Exception):
    def __init__(self, error_kind: str, *, retryable: bool = False):
        super().__init__(error_kind)
        self.error_kind = error_kind
        self.retryable = retryable


class TaskService:
    """Own SQLite queue state; trusted identity and execution grants stay external."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60,
        max_concurrency: int = 1,
    ):
        if lease_seconds <= 0 or max_concurrency < 1:
            raise ValueError("Lease and concurrency must be positive")
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.max_concurrency = max_concurrency
        with self._transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pt_tasks (
                    task_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL,
                    session_key TEXT NOT NULL, sender_id TEXT NOT NULL,
                    origin_message_id TEXT NOT NULL, fragment_key TEXT NOT NULL,
                    goal TEXT NOT NULL, workdir TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                    step_key TEXT NOT NULL DEFAULT 'main',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    cycle INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT, lease_token TEXT, lease_until REAL,
                    next_run REAL NOT NULL DEFAULT 0, error_kind TEXT,
                    metadata TEXT NOT NULL, result TEXT, created_at REAL NOT NULL,
                    UNIQUE(bot_id,session_key,sender_id,origin_message_id,fragment_key)
                );
                CREATE TABLE IF NOT EXISTS pt_steps (
                    task_id TEXT NOT NULL, step_key TEXT NOT NULL,
                    evidence TEXT NOT NULL, completed_at REAL NOT NULL,
                    PRIMARY KEY(task_id,step_key)
                );
                CREATE TABLE IF NOT EXISTS pt_attempts (
                    attempt_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    step_key TEXT NOT NULL, cycle INTEGER NOT NULL,
                    provider TEXT NOT NULL, lease_token TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pt_corrections (
                    task_id TEXT NOT NULL,step_key TEXT NOT NULL,cycle INTEGER NOT NULL,
                    PRIMARY KEY(task_id,step_key,cycle)
                );
                CREATE TABLE IF NOT EXISTS pt_outbox (
                    notification_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
                    revision INTEGER NOT NULL, kind TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_run REAL NOT NULL DEFAULT 0,
                    lease_token TEXT, lease_until REAL, last_error TEXT,
                    UNIQUE(task_id,kind,fingerprint)
                );
                CREATE INDEX IF NOT EXISTS pt_tasks_ready ON pt_tasks(status,next_run);
                CREATE INDEX IF NOT EXISTS pt_outbox_ready ON pt_outbox(state,next_run);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(pt_tasks)")}
            for name in ("updated_at", "finished_at"):
                if name not in columns:
                    db.execute(f"ALTER TABLE pt_tasks ADD COLUMN {name} REAL")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _job(self, db, row) -> TaskJob | None:
        if row is None:
            return None
        steps = {
            item["step_key"]: json.loads(item["evidence"])
            for item in db.execute(
                "SELECT * FROM pt_steps WHERE task_id=?", (row["task_id"],)
            )
        }
        return TaskJob(
            row["task_id"],
            TaskScope(row["bot_id"], row["session_key"], row["sender_id"]),
            row["origin_message_id"],
            row["fragment_key"],
            row["goal"],
            row["workdir"],
            row["status"],
            row["revision"],
            row["step_key"],
            row["attempts"],
            row["lease_token"],
            row["lease_until"],
            row["error_kind"],
            json.loads(row["metadata"]),
            steps,
            json.loads(row["result"]) if row["result"] else None,
            row["created_at"], row["updated_at"], row["finished_at"],
        )

    def _touch(self, db, task_id, *, record_finish=False):
        """Record a factual mutation; legacy timestamps remain unknown until changed."""
        now = self.clock()
        db.execute(
            "UPDATE pt_tasks SET updated_at=?,finished_at=CASE WHEN ?=0 THEN finished_at "
            "WHEN status IN ('succeeded','failed','cancelled') "
            "THEN COALESCE(finished_at,?) ELSE NULL END WHERE task_id=?",
            (now, int(record_finish), now, task_id),
        )

    def _owned(self, db, job):
        return db.execute(
            "SELECT * FROM pt_tasks WHERE task_id=? AND status='running' "
            "AND lease_token=? AND lease_until>?",
            (job.task_id, job.lease_token, self.clock()),
        ).fetchone()

    def _notify(self, db, task_id, kind, payload, fingerprint=None):
        self._touch(db, task_id, record_finish=kind in {"result", "state"})
        revision = db.execute(
            "SELECT revision FROM pt_tasks WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        return (
            db.execute(
                "INSERT OR IGNORE INTO pt_outbox(task_id,revision,kind,fingerprint,payload) VALUES(?,?,?,?,?)",
                (
                    task_id,
                    revision,
                    kind,
                    fingerprint or str(revision),
                    json.dumps(payload),
                ),
            ).rowcount
            > 0
        )

    def submit(
        self,
        scope: TaskScope,
        origin_message_id: str,
        fragment_key: str,
        goal: str,
        workdir: str | Path,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> TaskJob:
        """Persist a fragment using identity supplied only by the trusted event adapter.

        Args:
            scope: Identity already verified against the original platform event.
            origin_message_id: Stable original platform message ID.
            fragment_key: Stable fragment key shared by hub and reply adapters.
            goal: Original requested goal, never a claim of permission.
            workdir: Requested directory; the executor must enforce authorization.
            metadata: Optional plan and existing cron references, not credentials.

        Returns:
            The existing source fragment or newly committed task.

        Raises:
            ValueError: A required identity or goal is missing.
        """
        if not all(
            (
                scope.bot_id,
                scope.session_key,
                scope.sender_id,
                origin_message_id,
                fragment_key,
                goal,
                str(workdir),
            )
        ):
            raise ValueError(
                "Identity, source fragment, goal, and workdir are required"
            )
        values = (
            scope.bot_id,
            scope.session_key,
            scope.sender_id,
            origin_message_id,
            fragment_key,
        )
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? "
                "AND origin_message_id=? AND fragment_key=?",
                values,
            ).fetchone()
            if row is None:
                task_id = uuid4().hex
                db.execute(
                    "INSERT INTO pt_tasks(task_id,bot_id,session_key,sender_id,origin_message_id,"
                    "fragment_key,goal,workdir,status,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id,
                        *values,
                        goal,
                        str(workdir),
                        "queued",
                        json.dumps(dict(metadata or {})),
                        self.clock(),
                    ),
                )
                self._notify(db, task_id, "accepted", {"status": "queued"})
                row = db.execute(
                    "SELECT * FROM pt_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            return self._job(db, row)

    def get(self, task_id: str, scope: TaskScope) -> TaskJob | None:
        with self._transaction() as db:
            return self._job(
                db,
                db.execute(
                    "SELECT * FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                    (task_id, scope.bot_id, scope.session_key, scope.sender_id),
                ).fetchone(),
            )

    def link_tasks(self, task_id: str, scope: TaskScope, related_task_id: str) -> bool:
        """Associate model-selected executions without changing execution status."""
        with self._transaction() as db:
            rows = db.execute(
                "SELECT task_id,metadata FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=?",
                (scope.bot_id, scope.session_key, scope.sender_id),
            ).fetchall()
            owned = {row["task_id"]: json.loads(row["metadata"]) for row in rows}
            if any(key not in owned or owned[key].get("history_deleted")
                   for key in (task_id, related_task_id)):
                return False
            previous = owned[task_id].get("task_group_id", task_id)
            target = owned[related_task_id].get("task_group_id", related_task_id)
            if previous == target:
                return True
            for key, metadata in owned.items():
                if metadata.get("history_deleted") or metadata.get("task_group_id", key) != previous:
                    continue
                metadata["task_group_id"] = target
                if key == task_id:
                    metadata.update(parent_task_id=related_task_id, task_relation="related")
                db.execute("UPDATE pt_tasks SET metadata=? WHERE task_id=?",
                           (json.dumps(metadata), key))
                self._touch(db, key)
            return True

    def list(
        self, scope: TaskScope, *, limit: int = 50, latest: bool = False
    ) -> list[TaskJob]:
        with self._transaction() as db:
            order = "created_at DESC,rowid DESC" if latest else "created_at,rowid"
            rows = db.execute(
                "SELECT * FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? "
                f"ORDER BY {order} LIMIT ?",
                (
                    scope.bot_id,
                    scope.session_key,
                    scope.sender_id,
                    max(1, min(limit, 200)),
                ),
            ).fetchall()
            return [self._job(db, row) for row in rows]

    def claim(self, worker_id: str) -> TaskJob | None:
        """Reserve capacity and the first model request of the current step.

        Args:
            worker_id: Internal worker identity, not a platform user account.

        Returns:
            A fenced lease, or None if work or capacity is unavailable.
        """
        with self._transaction() as db:
            # Expired workers require explicit recovery; they still occupy capacity.
            active = db.execute(
                "SELECT COUNT(*) FROM pt_tasks WHERE lease_token IS NOT NULL"
            ).fetchone()[0]
            if active >= self.max_concurrency:
                return None
            row = db.execute(
                "SELECT * FROM pt_tasks WHERE status IN ('queued','waiting_retry') "
                "AND lease_token IS NULL AND next_run<=? ORDER BY created_at,rowid LIMIT 1",
                (self.clock(),),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute(
                "UPDATE pt_tasks SET status='running',revision=revision+1,worker_id=?,lease_token=?,"
                "lease_until=?,attempts=attempts+1 WHERE task_id=?",
                (worker_id, token, self.clock() + self.lease_seconds, row["task_id"]),
            )
            self._touch(db, row["task_id"])
            db.execute(
                "INSERT INTO pt_attempts(task_id,step_key,cycle,provider,lease_token,created_at) VALUES(?,?,?,?,?,?)",
                (
                    row["task_id"],
                    row["step_key"],
                    row["cycle"],
                    "primary",
                    token,
                    self.clock(),
                ),
            )
            return self._job(
                db,
                db.execute(
                    "SELECT * FROM pt_tasks WHERE task_id=?", (row["task_id"],)
                ).fetchone(),
            )

    def heartbeat(self, job: TaskJob) -> bool:
        with self._transaction() as db:
            if not self._owned(db, job):
                return False
            db.execute(
                "UPDATE pt_tasks SET lease_until=? WHERE task_id=?",
                (self.clock() + self.lease_seconds, job.task_id),
            )
            return True

    def ack_control(self, job: TaskJob) -> bool:
        """Release fenced capacity after the worker has stopped its execution.

        Args:
            job: Original worker lease, including its unguessable token.

        Returns:
            Whether a controlled or expired lease was acknowledged. This does
            not establish the external outcome or reopen an uncertain task.
        """
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE pt_tasks SET lease_token=NULL,lease_until=NULL,worker_id=NULL "
                    "WHERE task_id=? AND lease_token=? AND status!='running'",
                    (job.task_id, job.lease_token),
                ).rowcount
                > 0
            )

    def begin_attempt(self, job: TaskJob, provider: str) -> bool:
        """Record another model request, including fallback, under the live lease.

        Args:
            job: Live lease capability returned by claim.
            provider: Actual model role or provider identifier, without secrets.

        Returns:
            Whether the caller may issue this additional request.
        """
        with self._transaction() as db:
            row = self._owned(db, job)
            if not row:
                return False
            db.execute(
                "UPDATE pt_tasks SET attempts=attempts+1 WHERE task_id=?",
                (job.task_id,),
            )
            self._touch(db, job.task_id)
            db.execute(
                "INSERT INTO pt_attempts(task_id,step_key,cycle,provider,lease_token,created_at) VALUES(?,?,?,?,?,?)",
                (
                    job.task_id,
                    row["step_key"],
                    row["cycle"],
                    provider,
                    job.lease_token,
                    self.clock(),
                ),
            )
            return True

    def record_step(
        self,
        job: TaskJob,
        step_key: str,
        evidence: Mapping,
        *,
        next_step: str | None = None,
    ) -> bool:
        if not evidence:
            raise ValueError("Successful steps require verified evidence")
        with self._transaction() as db:
            row = self._owned(db, job)
            if not row or row["step_key"] != step_key:
                return False
            inserted = db.execute(
                "INSERT OR IGNORE INTO pt_steps(task_id,step_key,evidence,completed_at) VALUES(?,?,?,?)",
                (job.task_id, step_key, json.dumps(dict(evidence)), self.clock()),
            ).rowcount
            if not inserted:
                return False
            if next_step:
                if db.execute(
                    "SELECT 1 FROM pt_steps WHERE task_id=? AND step_key=?",
                    (job.task_id, next_step),
                ).fetchone():
                    raise ValueError("A completed step cannot be executed again")
                db.execute(
                    "UPDATE pt_tasks SET step_key=?,attempts=0,"
                    "error_kind=CASE WHEN error_kind='provider_unavailable' THEN NULL ELSE error_kind END WHERE task_id=?",
                    (next_step, job.task_id),
                )
            db.execute(
                "UPDATE pt_tasks SET revision=revision+1 WHERE task_id=?",
                (job.task_id,),
            )
            self._touch(db, job.task_id)
            return True

    def progress(
        self, job: TaskJob, text: str, *, content_fingerprint: str | None = None
    ) -> bool:
        with self._transaction() as db:
            owned = self._owned(db, job)
            if not owned or owned["error_kind"] == "provider_unavailable":
                return False
            if (
                content_fingerprint
                and db.execute(
                    "SELECT 1 FROM pt_outbox WHERE task_id=? AND kind='progress' AND fingerprint=?",
                    (job.task_id, content_fingerprint),
                ).fetchone()
            ):
                return False
            db.execute(
                "UPDATE pt_tasks SET revision=revision+1 WHERE task_id=?",
                (job.task_id,),
            )
            db.execute(
                "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                (job.task_id,),
            )
            return self._notify(
                db, job.task_id, "progress", {"text": text}, content_fingerprint
            )

    def finish(self, job: TaskJob, evidence: Mapping) -> bool:
        if not evidence:
            raise ValueError("Completion requires verified evidence")
        with self._transaction() as db:
            if not self._owned(db, job):
                return False
            db.execute(
                "UPDATE pt_tasks SET status='succeeded',revision=revision+1,result=?,error_kind=NULL,"
                "lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE task_id=?",
                (json.dumps(dict(evidence)), job.task_id),
            )
            db.execute(
                "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                (job.task_id,),
            )
            self._notify(
                db,
                job.task_id,
                "result",
                {"status": "succeeded", "evidence": dict(evidence)},
            )
            return True

    def fail(self, job: TaskJob, error_kind: str, *, retryable: bool = False) -> bool:
        """Persist a classified outcome and back off explicitly safe retries.

        Args:
            job: Live execution lease.
            error_kind: Stable error category without sensitive raw traces.
            retryable: True only when the executor knows repetition is safe.

        Returns:
            Whether the lease was transitioned; stale results have no effect.
        """
        with self._transaction() as db:
            row = self._owned(db, job)
            if not row:
                return False
            status = "failed"
            next_run = 0
            if error_kind in {
                "permission_denied",
                "authorization_expired",
                "missing_input",
            }:
                status = "waiting_user"
            elif error_kind in {"unknown_result", "interrupted"}:
                status = "unknown_result"
            elif retryable:
                status = "waiting_retry"
                next_run = self.clock() + (15 if row["attempts"] <= 1 else 60)
                if error_kind == "provider_unavailable":
                    request = json.loads(row["metadata"]).get("request", {})
                    policy = request_policy({"primary": "legacy", **request})
                    if row["attempts"] >= policy.max_attempts:
                        status, next_run = "failed", 0
            if db.execute(
                "SELECT 1 FROM pt_steps WHERE task_id=? AND step_key=?",
                (job.task_id, row["step_key"]),
            ).fetchone():
                status, next_run = "unknown_result", 0
            db.execute(
                "UPDATE pt_tasks SET status=?,revision=revision+1,error_kind=?,next_run=?,"
                "lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE task_id=?",
                (status, error_kind, next_run, job.task_id),
            )
            self._touch(db, job.task_id, record_finish=True)
            db.execute(
                "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                (job.task_id,),
            )
            if status != "waiting_retry":
                db.execute("UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='model_retry' AND state='pending'", (job.task_id,))
                self._notify(
                    db,
                    job.task_id,
                    "result",
                    {"status": status, "error_kind": error_kind},
                )
            elif error_kind == "provider_unavailable":
                fingerprint = f"model-retry:{row['cycle']}"
                if not db.execute(
                    "SELECT 1 FROM pt_outbox WHERE task_id=? AND kind='model_retry' AND fingerprint=?",
                    (job.task_id, fingerprint),
                ).fetchone():
                    self._notify(db, job.task_id, "model_retry", {
                        "status": status, "error_kind": error_kind,
                        "model_id": policy.provider_id,
                        "timeout_seconds": policy.timeout_seconds,
                        "max_attempts": policy.max_attempts,
                        "step_key": row["step_key"],
                        "text": "后台模型首次请求失败，正在重连同一模型；用户未再次催促。",
                    }, fingerprint)
            return True

    def _control(self, task_id, scope, action, request_patch=None):
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                (task_id, scope.bot_id, scope.session_key, scope.sender_id),
            ).fetchone()
            if not row:
                return False
            if json.loads(row["metadata"]).get("history_deleted"):
                return False
            status = row["status"]
            attempts = row["attempts"]
            cycle = row["cycle"]
            if action in {"resume", "retry"} and row["lease_token"] is not None:
                return False
            if action == "cancel" and status not in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                status = "cancelled"
            elif action == "pause" and status in {"queued", "waiting_retry", "running"}:
                status = "unknown_result" if status == "running" else "paused"
            elif action == "resume" and status in {"paused", "waiting_user"}:
                status = "queued"
            elif action == "retry" and status in {"failed", "waiting_retry"}:
                status, attempts, cycle = "queued", 0, cycle + 1
            else:
                return False
            if request_patch is not None:
                metadata = json.loads(row["metadata"])
                metadata.setdefault("request", {}).update(request_policy(request_patch).as_request())
                db.execute("UPDATE pt_tasks SET metadata=? WHERE task_id=?", (json.dumps(metadata), task_id))
            db.execute(
                "UPDATE pt_tasks SET status=?,revision=revision+1,attempts=?,cycle=?,next_run=0 "
                "WHERE task_id=?",
                (status, attempts, cycle, task_id),
            )
            db.execute("UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='model_retry' AND state='pending'", (task_id,))
            if status in {"cancelled", "unknown_result", "paused"}:
                db.execute(
                    "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                    (task_id,),
                )
            self._notify(db, task_id, "state", {"status": status})
            return True

    def cancel(self, task_id: str, scope: TaskScope) -> bool:
        return self._control(task_id, scope, "cancel")

    def pause(self, task_id: str, scope: TaskScope) -> bool:
        return self._control(task_id, scope, "pause")

    def resume(self, task_id: str, scope: TaskScope, *, request_patch=None) -> bool:
        return self._control(task_id, scope, "resume", request_patch)

    def retry(self, task_id: str, scope: TaskScope, *, request_patch=None) -> bool:
        return self._control(task_id, scope, "retry", request_patch)

    def recover_with_guidance(self, task_id, scope, expected_revision, guidance):
        """前台纠正原失败任务；原子保留检查点、权限来源及模型请求策略。"""
        if type(expected_revision) is not int or not isinstance(guidance, str) or not guidance.strip() or len(guidance) > 4000:
            return False
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                (task_id, scope.bot_id, scope.session_key, scope.sender_id),
            ).fetchone()
            if row is None or row["revision"] != expected_revision or row["lease_token"] is not None or row["status"] not in {"failed", "waiting_user"}:
                return False
            if row["error_kind"] in {"provider_unavailable", "unknown_result", "lease_lost", "delivery_unknown"}:
                return False
            metadata = json.loads(row["metadata"])
            if metadata.get("history_deleted"):
                return False
            key = hashlib.sha256((str(row["error_kind"]) + "\0" + guidance.strip()).encode()).hexdigest()
            if metadata.get("feedback_recovery_key") == key:
                return False
            metadata["feedback_recovery_key"] = key
            metadata.setdefault("request", {})["recovery_guidance"] = guidance.strip()
            db.execute(
                "UPDATE pt_tasks SET status='queued',metadata=?,revision=revision+1,attempts=0,cycle=cycle+1,next_run=0,error_kind=NULL,finished_at=NULL WHERE task_id=?",
                (json.dumps(metadata, ensure_ascii=False), task_id),
            )
            self._touch(db, task_id)
            return True

    def reconcile(
        self,
        task_id: str,
        scope: TaskScope,
        outcome: str,
        evidence: Mapping,
        *,
        worker_stopped: bool = False,
    ) -> bool:
        """Apply operator-verified reconciliation; never infer an external outcome.

        Only trusted program code may call this API after checking real receipts.
        ``not_executed`` leaves work paused and preserves its request history.
        An outstanding worker lease additionally requires an operator-confirmed
        stop; model assertions must never supply ``worker_stopped``.
        """
        if outcome not in {"completed", "not_executed"}:
            raise ValueError("Reconciliation requires a definite external outcome")
        if not isinstance(evidence, Mapping) or not evidence:
            raise ValueError("Reconciliation requires verified evidence")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                (task_id, scope.bot_id, scope.session_key, scope.sender_id),
            ).fetchone()
            if not row or row["status"] != "unknown_result":
                return False
            if row["lease_token"] is not None and worker_stopped is not True:
                return False
            if (
                outcome == "not_executed"
                and db.execute(
                    "SELECT 1 FROM pt_steps WHERE task_id=? AND step_key=?",
                    (task_id, row["step_key"]),
                ).fetchone()
            ):
                return False
            status = "succeeded" if outcome == "completed" else "paused"
            metadata = json.loads(row["metadata"])
            metadata["reconciliation"] = {
                "outcome": outcome,
                "evidence": dict(evidence),
                "verified_at": self.clock(),
            }
            db.execute(
                "UPDATE pt_tasks SET status=?,revision=revision+1,metadata=?,result=?,"
                "error_kind=NULL,next_run=0,lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE task_id=?",
                (
                    status,
                    json.dumps(metadata),
                    json.dumps(dict(evidence)) if outcome == "completed" else None,
                    task_id,
                ),
            )
            db.execute(
                "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                (task_id,),
            )
            self._notify(
                db,
                task_id,
                "result" if outcome == "completed" else "state",
                {"status": status, "outcome": outcome, "evidence": dict(evidence)},
            )
            return True

    def revise_goal(self, task_id: str, scope: TaskScope, goal: str, *, input_metadata=None) -> bool:
        """Revise one safely idle task without reopening or erasing evidence."""
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 4000:
            raise ValueError("A revised goal of at most 4000 characters is required")
        with self._transaction() as db:
            row = db.execute(
                "SELECT metadata FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                (task_id, scope.bot_id, scope.session_key, scope.sender_id),
            ).fetchone()
            if row is None:
                return False
            metadata = json.loads(row["metadata"])
            incoming = (input_metadata or {}).get("request", {})
            if "request_timeout_seconds" in incoming:
                metadata.setdefault("request", {}).update(request_policy(incoming).as_request())
            if incoming.get("current_input"):
                saved = metadata.setdefault("request", {})
                contexts = list(saved.get("contexts", ()))
                previous = saved.get("current_input", {})
                if previous.get("content"):
                    contexts.append({"role": "user", "content": previous["content"]})
                saved.update(
                    contexts=contexts,
                    current_input=incoming["current_input"],
                    media={**saved.get("media", {}), **incoming.get("media", {})},
                )
            changed = db.execute(
                "UPDATE pt_tasks SET goal=?,metadata=?,revision=revision+1 WHERE task_id=? AND bot_id=? "
                "AND session_key=? AND sender_id=? AND status IN ('queued','paused') "
                "AND NOT EXISTS(SELECT 1 FROM pt_steps WHERE pt_steps.task_id=pt_tasks.task_id)",
                (goal, json.dumps(metadata, ensure_ascii=False), task_id, scope.bot_id, scope.session_key, scope.sender_id),
            ).rowcount
            if not changed:
                return False
            self._notify(db, task_id, "state", {"goal": goal, "action": "revise_goal"})
            return True

    def recover_expired(self) -> int:
        """Fence expired leases without replaying unknown external operations.

        Returns:
            Number of execution leases moved to unknown_result.
        """
        with self._transaction() as db:
            rows = db.execute(
                "SELECT task_id FROM pt_tasks WHERE status='running' AND lease_until<=?",
                (self.clock(),),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE pt_tasks SET status='unknown_result',revision=revision+1,"
                    "error_kind='lease_expired' WHERE task_id=?",
                    (row["task_id"],),
                )
                db.execute(
                    "UPDATE pt_outbox SET state='superseded' WHERE task_id=? AND kind='progress' AND state='pending'",
                    (row["task_id"],),
                )
                self._notify(
                    db,
                    row["task_id"],
                    "result",
                    {"status": "unknown_result", "error_kind": "lease_expired"},
                )
            db.execute(
                "UPDATE pt_outbox SET state='delivery_unknown',lease_token=NULL,lease_until=NULL "
                "WHERE state='sending' AND lease_until<=?",
                (self.clock(),),
            )
            return len(rows)

    def claim_notification(self, sender_id: str) -> Notification | None:
        with self._transaction() as db:
            db.execute(
                "UPDATE pt_outbox SET state='superseded' WHERE kind='progress' AND state='pending' "
                "AND EXISTS(SELECT 1 FROM pt_tasks t WHERE t.task_id=pt_outbox.task_id "
                "AND (t.status!='running' OR t.revision!=pt_outbox.revision))"
            )
            row = db.execute(
                "SELECT o.*,t.bot_id,t.session_key,t.sender_id FROM pt_outbox o "
                "JOIN pt_tasks t ON t.task_id=o.task_id WHERE o.state='pending' AND o.next_run<=? "
                "AND NOT EXISTS(SELECT 1 FROM pt_outbox p WHERE p.task_id=o.task_id "
                "AND p.notification_id<o.notification_id AND p.state IN ('pending','sending','delivery_unknown')) "
                "ORDER BY o.notification_id LIMIT 1",
                (self.clock(),),
            ).fetchone()
            if not row:
                return None
            token = f"{sender_id}:{uuid4().hex}"
            db.execute(
                "UPDATE pt_outbox SET state='sending',lease_token=?,lease_until=?,attempts=attempts+1 WHERE notification_id=?",
                (token, self.clock() + self.lease_seconds, row["notification_id"]),
            )
            return Notification(
                row["notification_id"],
                row["task_id"],
                TaskScope(row["bot_id"], row["session_key"], row["sender_id"]),
                row["revision"],
                row["kind"],
                json.loads(row["payload"]),
                token,
            )

    def ack_notification(self, notification: Notification) -> bool:
        with self._transaction() as db:
            return (
                db.execute(
                    "UPDATE pt_outbox SET state='sent',lease_token=NULL,lease_until=NULL WHERE notification_id=? "
                    "AND state='sending' AND lease_token=? AND lease_until>?",
                    (
                        notification.notification_id,
                        notification.lease_token,
                        self.clock(),
                    ),
                ).rowcount
                > 0
            )

    def fail_notification(
        self, notification: Notification, error_kind: str, *, retryable: bool = False
    ) -> bool:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM pt_outbox WHERE notification_id=? AND state='sending' AND lease_token=? AND lease_until>?",
                (notification.notification_id, notification.lease_token, self.clock()),
            ).fetchone()
            if not row:
                return False
            state = (
                "pending" if retryable and row["attempts"] < 3 else "delivery_unknown"
            )
            if row["kind"] == "progress" and retryable:
                task = db.execute(
                    "SELECT status,revision FROM pt_tasks WHERE task_id=?",
                    (row["task_id"],),
                ).fetchone()
                if task["status"] != "running" or task["revision"] != row["revision"]:
                    state = "superseded"
            db.execute(
                "UPDATE pt_outbox SET state=?,last_error=?,next_run=?,lease_token=NULL,lease_until=NULL WHERE notification_id=?",
                (
                    state,
                    error_kind,
                    self.clock() + (15 if row["attempts"] == 1 else 60),
                    notification.notification_id,
                ),
            )
            return True


class TaskScheduler:
    def __init__(
        self,
        service: TaskService,
        execute,
        authorize,
        *,
        worker_id: str | None = None,
        progress_after_seconds: float | None = 90,
    ):
        if (
            progress_after_seconds is not None
            and not 0 <= progress_after_seconds <= 120
        ):
            raise ValueError("Progress delay must be between 0 and 120 seconds")
        self.service = service
        self.execute = execute
        self.authorize = authorize
        self.worker_id = worker_id or uuid4().hex
        self.progress_after_seconds = progress_after_seconds

    async def schedule_once(self) -> bool:
        """Run one leased task with injected evidence-producing execution.

        Returns:
            Whether a task was claimed. Execution failures are persisted.

        Raises:
            asyncio.CancelledError: The worker was interrupted after persisting uncertainty.
        """
        self.service.recover_expired()
        job = self.service.claim(self.worker_id)
        if job is None:
            return False
        heartbeat = None
        execution = None
        progress = None
        lease_lost = False

        async def renew():
            nonlocal lease_lost
            while True:
                await asyncio.sleep(self.service.lease_seconds / 3)
                if not self.service.heartbeat(job):
                    lease_lost = True
                    if execution:
                        execution.cancel()
                    return

        async def report_progress():
            await asyncio.sleep(self.progress_after_seconds)
            if execution and not execution.done():
                self.service.progress(
                    job,
                    "Task is still running",
                    content_fingerprint=f"runtime90:{job.lease_token}",
                )

        try:
            authorization = self.authorize(job)
            if inspect.isawaitable(authorization):
                authorization = await authorization
            if authorization is not True:
                self.service.fail(job, "permission_denied")
                return True
            # Authorization callbacks may yield; fence again before calling the executor.
            if not self.service.heartbeat(job):
                return True
            heartbeat = asyncio.create_task(renew())
            execution = asyncio.create_task(self.execute(job))
            if self.progress_after_seconds:
                progress = asyncio.create_task(report_progress())
            evidence = await execution
            if not isinstance(evidence, Mapping) or not evidence:
                self.service.fail(job, "unknown_result")
            else:
                self.service.finish(job, evidence)
        except TaskFailure as error:
            self.service.fail(job, error.error_kind, retryable=error.retryable)
        except asyncio.CancelledError:
            self.service.fail(job, "interrupted")
            if not lease_lost:
                raise
        except Exception:
            # An unclassified exception may follow a committed external side effect.
            self.service.fail(job, "unknown_result")
        finally:
            if progress:
                progress.cancel()
                with suppress(asyncio.CancelledError):
                    await progress
            if heartbeat:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat
            if execution and not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
            # Never leave a live lease running on an unexpected exit path.
            self.service.fail(job, "unknown_result")
            self.service.recover_expired()
            self.service.ack_control(job)
        return True

    async def worker_loop(self, stop: asyncio.Event, *, poll_seconds: float = 1):
        """Poll persisted ready work until the caller requests shutdown.

        Args:
            stop: Application-owned shutdown event.
            poll_seconds: Idle queue polling delay, not a task schedule rule.

        Raises:
            ValueError: The polling delay is not positive.
        """
        if poll_seconds <= 0:
            raise ValueError("Polling delay must be positive")
        while not stop.is_set():
            if await self.schedule_once():
                continue
            try:
                await asyncio.wait_for(stop.wait(), poll_seconds)
            except TimeoutError:
                pass
