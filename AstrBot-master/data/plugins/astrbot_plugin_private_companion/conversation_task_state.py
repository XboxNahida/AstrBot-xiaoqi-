"""Task lifecycle storage with bounded memory and optional SQLite persistence."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections import OrderedDict
from contextlib import closing
from dataclasses import asdict, dataclass, field, fields
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any
from weakref import WeakValueDictionary

TASK_STATUSES = frozenset(
    {
        "queued",
        "planning",
        "running",
        "progress",
        "waiting_external",
        "retry_wait",
        "waiting_user",
        "paused",
        "unknown_result",
        "complete",
        "failed",
        "timeout",
        "cancelled",
    }
)
INACTIVE_STATUSES = frozenset(
    {"complete", "failed", "timeout", "cancelled", "paused", "unknown_result"}
)


def _transactional(method):
    """Serialize store operations and roll back cached objects on write failure.

    Args:
        method: Store method sharing one short-lived SQLite transaction.

    Returns:
        A transaction-protected method preserving the original API.
    """

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            if self._db_path is None or self._connection is not None:
                return method(self, *args, **kwargs)
            with closing(sqlite3.connect(self._db_path, timeout=30)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._connection = connection
                try:
                    result = method(self, *args, **kwargs)
                    connection.commit()
                    return result
                except Exception:
                    connection.rollback()
                    for task_id in tuple(self._tasks):
                        row = connection.execute(
                            "SELECT payload FROM conversation_tasks WHERE task_id = ?",
                            (task_id,),
                        ).fetchone()
                        if row:
                            self._cache_row(row)
                        else:
                            self._tasks.pop(task_id, None)
                    raise
                finally:
                    self._connection = None

    return wrapped


@dataclass(slots=True)
class ConversationTaskState:
    task_id: str
    session_key: str
    task_text: str
    unfinished_task: str
    provider_id: str
    fallback_provider_id: str
    status: str
    started_at: float
    updated_at: float
    estimated_seconds: int
    progress_note: str = ""
    error: str = ""
    result_preview: str = ""
    sender_id: str = ""
    conversation_id: str = ""
    phase: str = "waiting_model"
    revision: int = 0
    tool_name: str = ""
    tool_result: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    reply_context: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Return a bounded, JSON-safe view suitable for prompt injection."""
        data = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "reply_context"
        }
        data["task_text"] = data["task_text"][:2400]
        data["unfinished_task"] = data["unfinished_task"][:2400]
        data["progress_note"] = data["progress_note"][:500]
        data["error"] = data["error"][:800]
        data["result_preview"] = data["result_preview"][:1600]
        data["tool_events"] = [dict(item) for item in self.tool_events[-4:]]
        data["notices"] = list(self.notices[-4:])
        return data


class ConversationTaskStateStore:
    """Keep the newest task states with deterministic lifecycle transitions."""

    def __init__(
        self,
        *,
        max_tasks: int = 32,
        clock: Any = time.time,
        db_path: Path | None = None,
    ) -> None:
        self.max_tasks = max(1, int(max_tasks))
        self._clock = clock
        self._tasks: OrderedDict[str, ConversationTaskState] = OrderedDict()
        self._notice_locks: (
            dict[str, asyncio.Lock] | WeakValueDictionary[str, asyncio.Lock]
        ) = WeakValueDictionary() if db_path is not None else {}
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        self._db_path = Path(db_path) if db_path is not None else None
        if self._db_path is not None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self._db_path)) as connection, connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS conversation_tasks (
                    task_id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL, revision INTEGER NOT NULL,
                    sequence INTEGER NOT NULL, payload TEXT NOT NULL)""")
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS conversation_tasks_session ON conversation_tasks(session_key, sender_id, conversation_id, sequence DESC)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS conversation_tasks_sequence ON conversation_tasks(sequence DESC)"
                )

    @staticmethod
    def _text(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def _touch(self, state: ConversationTaskState) -> ConversationTaskState:
        state.updated_at = float(self._clock())
        state.revision += 1
        self._tasks.move_to_end(state.task_id)
        self._persist(state)
        return state

    def _persist(self, state: ConversationTaskState) -> None:
        """Write a complete snapshot in the caller's current transaction.

        Args:
            state: Task snapshot to persist; memory mode requires no write.
        """
        if self._connection is not None:
            self._connection.execute(
                """INSERT INTO conversation_tasks
                (task_id, session_key, sender_id, conversation_id, status, revision, sequence, payload)
                VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sequence), 0) + 1 FROM conversation_tasks), ?)
                ON CONFLICT(task_id) DO UPDATE SET session_key=excluded.session_key,
                sender_id=excluded.sender_id, conversation_id=excluded.conversation_id,
                status=excluded.status, revision=excluded.revision,
                sequence=excluded.sequence, payload=excluded.payload""",
                (
                    state.task_id,
                    state.session_key,
                    state.sender_id,
                    state.conversation_id,
                    state.status,
                    state.revision,
                    json.dumps(asdict(state), ensure_ascii=False),
                ),
            )

    def _cache_row(self, row) -> ConversationTaskState:
        """Refresh a cached object in place so existing caller references survive.

        Args:
            row: SQLite result containing a serialized task snapshot.

        Returns:
            The refreshed task held in the bounded memory cache.
        """
        loaded = ConversationTaskState(**json.loads(row[0]))
        state = self._tasks.get(loaded.task_id)
        if state is None:
            state = loaded
            self._tasks[state.task_id] = state
        else:
            for item in fields(state):
                setattr(state, item.name, getattr(loaded, item.name))
        self._tasks.move_to_end(state.task_id)
        while len(self._tasks) > self.max_tasks:
            self._tasks.popitem(last=False)
        return state

    def _find(self, task_id: str) -> ConversationTaskState:
        state = self.get(task_id)
        if state is None:
            raise KeyError(f"unknown conversation task: {task_id}")
        return state

    @_transactional
    def start_task(
        self,
        *,
        session_key: str,
        task_text: str,
        provider_id: str,
        fallback_provider_id: str = "",
        estimated_seconds: int = 0,
        task_id: str | None = None,
        sender_id: str = "",
        conversation_id: str = "",
    ) -> ConversationTaskState:
        now = float(self._clock())
        estimate = max(0, min(3600, int(estimated_seconds or 0)))
        state = ConversationTaskState(
            task_id=self._text(task_id, 120) or uuid.uuid4().hex,
            session_key=self._text(session_key, 240),
            task_text=self._text(task_text, 2400),
            unfinished_task=self._text(task_text, 2400),
            provider_id=self._text(provider_id, 240),
            fallback_provider_id=self._text(fallback_provider_id, 240),
            status="running",
            started_at=now,
            updated_at=now,
            estimated_seconds=estimate,
            sender_id=self._text(sender_id, 160),
            conversation_id=self._text(conversation_id, 160),
        )
        if self._connection is not None and self.get(state.task_id) is not None:
            raise ValueError(f"duplicate conversation task: {state.task_id}")
        self._persist(state)
        self._tasks[state.task_id] = state
        while len(self._tasks) > self.max_tasks:
            evicted, _ = self._tasks.popitem(last=False)
            if self._db_path is None:
                self._notice_locks.pop(evicted, None)
        return state

    @_transactional
    def get(self, task_id: str | None) -> ConversationTaskState | None:
        if self._connection is not None:
            row = self._connection.execute(
                "SELECT payload FROM conversation_tasks WHERE task_id = ?",
                (str(task_id or ""),),
            ).fetchone()
            return self._cache_row(row) if row else None
        return self._tasks.get(str(task_id or ""))

    @_transactional
    def mark_progress(self, task_id: str, progress_note: str) -> ConversationTaskState:
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES:
            return state
        state.status = "progress"
        state.progress_note = self._text(progress_note, 500)
        return self._touch(state)

    @_transactional
    def notice_lock(self, task_id: str) -> asyncio.Lock:
        """Serialize notices for one task across automatic and query events.

        Args:
            task_id: Existing task identifier.

        Returns:
            A bounded, task-scoped asyncio lock.
        """
        self._find(task_id)
        return self._notice_locks.setdefault(task_id, asyncio.Lock())

    @_transactional
    def record_tool_start(self, task_id: str, name: str) -> ConversationTaskState:
        """Record a real tool invocation without claiming its success.

        Args:
            task_id: Task receiving the invocation.
            name: Tool name observed by the framework hook.

        Returns:
            Updated task, or an unchanged terminal task.
        """
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES:
            return state
        state.status = "progress"
        state.phase = "tool_running"
        state.tool_name = self._text(name, 120)
        state.progress_note = "工具调用已开始，尚未返回结果"
        return self._touch(state)

    @_transactional
    def record_tool_result(
        self,
        task_id: str,
        name: str,
        result: str,
        *,
        failed: bool = False,
        background_running: bool = False,
    ) -> ConversationTaskState:
        """Keep bounded tool evidence while allowing the agent to recover.

        Args:
            task_id: Task receiving the result.
            name: Observed tool name.
            result: Redacted, bounded result text.
            failed: A structured tool error or nonzero process exit.
            background_running: A managed process explicitly remains running.

        Returns:
            Updated task, or an unchanged terminal task.
        """
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES:
            return state
        state.status = "progress"
        state.phase = (
            "tool_error"
            if failed
            else ("background_running" if background_running else "waiting_model")
        )
        state.tool_name = self._text(name, 120)
        state.tool_result = self._text(result, 1200)
        state.progress_note = (
            "工具返回错误，任务尚未结束"
            if failed
            else (
                "后台进程仍在运行"
                if background_running
                else "已收到工具结果，等待任务模型继续处理"
            )
        )
        state.tool_events = (
            state.tool_events
            + [
                {
                    "tool": state.tool_name,
                    "result": state.tool_result[:500],
                    "failed": bool(failed),
                }
            ]
        )[-4:]
        return self._touch(state)

    @_transactional
    def complete_task(self, task_id: str, result: str) -> ConversationTaskState:
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES - {"complete"}:
            return state
        state.status = "complete"
        state.phase = "finished"
        state.result_preview = self._text(result, 1600)
        state.progress_note = ""
        state.unfinished_task = ""
        return self._touch(state)

    @_transactional
    def fail_task(
        self, task_id: str, error: str, *, unfinished_task: str | None = None
    ) -> ConversationTaskState:
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES - {"failed", "timeout"}:
            return state
        state.status = "failed"
        state.phase = "failed"
        state.error = self._text(error, 800)
        if unfinished_task is not None:
            state.unfinished_task = self._text(unfinished_task, 2400)
        return self._touch(state)

    @_transactional
    def timeout_task(self, task_id: str, error: str = "") -> ConversationTaskState:
        state = self._find(task_id)
        if state.status in INACTIVE_STATUSES:
            return state
        state.status = "timeout"
        state.phase = "timeout"
        state.error = self._text(error, 800)
        return self._touch(state)

    @_transactional
    def get_active_task(
        self,
        session_key: str,
        *,
        sender_id: str = "",
        conversation_id: str | None = None,
    ) -> ConversationTaskState | None:
        key = self._text(session_key, 240)
        if self._connection is not None:
            query = "SELECT payload FROM conversation_tasks WHERE session_key = ? AND status IN ('queued', 'running', 'progress')"
            params: list[Any] = [key]
            if sender_id:
                query += " AND sender_id = ?"
                params.append(sender_id)
            if conversation_id is not None:
                query += " AND conversation_id = ?"
                params.append(conversation_id)
            row = self._connection.execute(
                query + " ORDER BY sequence DESC LIMIT 1", params
            ).fetchone()
            return self._cache_row(row) if row else None
        for state in reversed(tuple(self._tasks.values())):
            if (
                state.session_key == key
                and state.status in {"queued", "running", "progress"}
                and (not sender_id or state.sender_id == sender_id)
                and (
                    conversation_id is None or state.conversation_id == conversation_id
                )
            ):
                return state
        return None

    def summary_for_session(
        self,
        session_key: str,
        *,
        sender_id: str = "",
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        result = self.list_for_session(
            session_key, sender_id=sender_id, conversation_id=conversation_id, limit=1
        )
        return result[0] if result else {}

    @_transactional
    def list_for_session(
        self,
        session_key: str,
        *,
        sender_id: str = "",
        conversation_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return newest summaries restricted to the supplied actor and branch.

        Args:
            session_key: Bot-qualified session identifier.
            sender_id: Exact actor filter; empty keeps the legacy session view.
            conversation_id: Exact branch filter, or all branches when omitted.
            limit: Maximum number of summaries.

        Returns:
            Bounded summaries without private reply context.
        """
        limit = max(0, int(limit))
        key = self._text(session_key, 240)
        if self._connection is not None:
            query = "SELECT payload FROM conversation_tasks WHERE session_key = ?"
            params: list[Any] = [key]
            if sender_id:
                query += " AND sender_id = ?"
                params.append(sender_id)
            if conversation_id is not None:
                query += " AND conversation_id = ?"
                params.append(conversation_id)
            rows = self._connection.execute(
                query + " ORDER BY sequence DESC LIMIT ?", [*params, limit]
            ).fetchall()
            return [
                ConversationTaskState(**json.loads(row[0])).summary() for row in rows
            ]
        result = []
        for state in reversed(tuple(self._tasks.values())):
            if (
                state.session_key == key
                and (not sender_id or state.sender_id == sender_id)
                and (
                    conversation_id is None or state.conversation_id == conversation_id
                )
            ):
                result.append(state.summary())
        return result[:limit]

    @_transactional
    def save_task(self, state: ConversationTaskState) -> ConversationTaskState:
        """Persist direct field changes without overwriting a newer revision.

        Args:
            state: Existing task with JSON-serializable reply context and notices.

        Returns:
            The same state object after a successful save.

        Raises:
            KeyError: The task does not exist.
            ValueError: The supplied revision is stale or status is invalid.
        """
        if state.status not in TASK_STATUSES:
            raise ValueError(f"invalid conversation task status: {state.status}")
        if self._connection is not None:
            row = self._connection.execute(
                "SELECT revision, status FROM conversation_tasks WHERE task_id = ?",
                (state.task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown conversation task: {state.task_id}")
            if row[0] != state.revision:
                raise ValueError(f"stale conversation task revision: {state.task_id}")
            if row[1] in INACTIVE_STATUSES and state.status != row[1]:
                raise ValueError(
                    f"terminal conversation task cannot change status: {state.task_id}"
                )
        elif state.task_id not in self._tasks:
            raise KeyError(f"unknown conversation task: {state.task_id}")
        self._tasks[state.task_id] = state
        result = self._touch(state)
        while len(self._tasks) > self.max_tasks:
            self._tasks.popitem(last=False)
        return result

    @_transactional
    def recover_interrupted(self) -> list[ConversationTaskState]:
        """Mark previous execution uncertain after the old worker has stopped.

        Returns:
            States moved to unknown_result; no work is automatically retried.
        """
        if self._connection is not None:
            rows = self._connection.execute(
                "SELECT payload FROM conversation_tasks WHERE status IN ('queued', 'running', 'progress') ORDER BY sequence"
            ).fetchall()
            states = [ConversationTaskState(**json.loads(row[0])) for row in rows]
        else:
            states = [
                state
                for state in self._tasks.values()
                if state.status in {"queued", "running", "progress"}
            ]
        recovered = []
        for state in states:
            if self._connection is not None:
                state = self._cache_row(
                    (json.dumps(asdict(state), ensure_ascii=False),)
                )
            state.status = "unknown_result"
            state.phase = "unknown_result"
            state.progress_note = "执行中断，结果待核实；不会自动重复执行"
            self._touch(state)
            recovered.append(state)
        return recovered
