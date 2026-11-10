"""Persist scope-bound Codex thread identities and bounded factual events."""

from __future__ import annotations

import json
import re
import sqlite3


def _text(value, name, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"Invalid {name}")


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid {name}")


def public_event(value, *, method="", mode="event", depth=0):
    """Project protocol positions while preserving ordinary tool-result fields.

    Args:
        value: Event payload, including possibly nested public event records.
        method: Protocol event name when projecting its outer payload.
        mode: Structural protocol position, not inferred from business field names.
        depth: Defensive recursion bound.

    Returns:
        Public structure with input/instructions/reasoning excluded before previews.
    """
    if depth > 24:
        return "[omitted: nesting limit]"
    if isinstance(value, dict):
        if depth == 0 and value.get("truncated") and "preview" in value:
            return {"truncated": True, "preview": "[legacy opaque preview omitted]"}
        if mode == "item" and value.get("type") in {"userMessage", "reasoning"}:
            return {key: value[key] for key in ("id", "type") if key in value} | {
                "omitted": True
            }
        record = (
            isinstance(value.get("method"), str)
            and value["method"].startswith(("item/", "turn/", "thread/", "host_tool/"))
            and isinstance(value.get("payload"), dict)
        )
        result = {}
        for key, item in value.items():
            if mode in {"event", "turn", "thread"} and key.lower().replace(
                "_", ""
            ).replace("-", "") in {
                "input",
                "instructions",
                "systemprompt",
                "baseinstructions",
                "developerinstructions",
                "request",
                "requestbody",
                "fullrequest",
                "authorization",
                "headers",
                "credentials",
                "apikey",
                "accesstoken",
                "refreshtoken",
                "temporarytoken",
                "gatewaytoken",
                "cookie",
                "cookies",
            }:
                continue
            child = "business"
            if mode == "event" and key in {"item", "turn", "thread"}:
                child = key
            elif mode == "turn" and key == "items":
                child = "item"
            elif mode == "thread" and key == "turns":
                child = "turn"
            if record and key == "payload":
                result[key] = public_event(
                    item, method=value["method"], depth=depth + 1
                )
            else:
                result[key] = public_event(item, mode=child, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [public_event(item, mode=mode, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(
                    public_event(decoded, mode="business", depth=depth + 1),
                    ensure_ascii=False,
                )
        return re.sub(r"data:image/[^\s\"'<>]+", "[image omitted]", value)
    return value


def _bounded(value, depth=0, budget=None):
    """Bound already-projected data without deleting ordinary business keys.

    Args:
        value: JSON-like event data, never a request envelope.
        depth: Current recursive depth.
        budget: Shared serialized-size budget.

    Returns:
        A readable JSON-compatible preview.
    """
    if budget is None:
        budget = [12000]
    budget[0] -= 40
    if depth >= 12 or budget[0] <= 0:
        return "[truncated: depth limit]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            if not isinstance(key, str):
                raise ValueError("Event keys must be strings")
            if budget[0] <= 0:
                result["_truncated"] = "size limit"
                break
            budget[0] -= len(key[:256])
            result[key[:256]] = _bounded(item, depth + 1, budget)
        if len(value) > 40:
            result["_truncated"] = "object limit"
        return result
    if isinstance(value, list):
        result = []
        for item in value[:40]:
            if budget[0] <= 0:
                break
            result.append(_bounded(item, depth + 1, budget))
        if len(value) > len(result):
            result.append("[truncated: array limit]")
        return result
    if isinstance(value, str):
        size = min(12000, max(0, budget[0]))
        budget[0] -= min(size, len(value))
        return value[:size] + (" [truncated]" if len(value) > size else "")
    if value is None or type(value) in (int, float, bool):
        return value
    raise ValueError("Event payload must be JSON data")


class CodexStore:
    """Share TaskService transactions rather than owning a second task queue."""

    def __init__(self, service):
        self.service = service
        with service._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pc_codex_threads (
                task_id TEXT PRIMARY KEY REFERENCES pt_tasks(task_id),
                thread_id TEXT UNIQUE, turn_id TEXT, model_id TEXT NOT NULL,
                model TEXT NOT NULL, reasoning_effort TEXT NOT NULL,
                connection_state TEXT NOT NULL, last_event_at REAL,
                generation INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS pc_codex_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES pt_tasks(task_id),
                generation INTEGER NOT NULL,turn_id TEXT,method TEXT NOT NULL,
                payload TEXT NOT NULL,created_at REAL NOT NULL)""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS pc_codex_events_task ON pc_codex_events(task_id,id)"
            )

    def _row(self, db, scope, task_id="", thread_id=""):
        for name, value in (("task_id", task_id), ("thread_id", thread_id)):
            if value != "":
                _text(value, name)
        if not task_id and not thread_id:
            raise ValueError("A task_id or thread_id is required")
        return db.execute(
            "SELECT c.*,t.bot_id,t.session_key,t.sender_id FROM pc_codex_threads c "
            "JOIN pt_tasks t ON t.task_id=c.task_id WHERE t.bot_id=? "
            "AND t.session_key=? AND t.sender_id=? "
            "AND (?='' OR c.task_id=?) AND (?='' OR c.thread_id=?) "
            "AND COALESCE(json_extract(t.metadata,'$.history_deleted'),0)=0",
            (
                scope.bot_id,
                scope.session_key,
                scope.sender_id,
                task_id,
                task_id,
                thread_id,
                thread_id,
            ),
        ).fetchone()

    def _snapshot(self, db, task_id):
        row = db.execute(
            "SELECT * FROM pc_codex_threads WHERE task_id=?", (task_id,)
        ).fetchone()
        metadata = json.loads(
            db.execute(
                "SELECT metadata FROM pt_tasks WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        )
        metadata["engine"] = "codex"
        metadata["codex"] = {
            key: row[key]
            for key in (
                "thread_id",
                "turn_id",
                "model_id",
                "model",
                "reasoning_effort",
                "connection_state",
            )
        }
        metadata.setdefault("request", {})["primary"] = row["model_id"]
        db.execute(
            "UPDATE pt_tasks SET metadata=? WHERE task_id=?",
            (json.dumps(metadata, ensure_ascii=False), task_id),
        )

    def bind(self, task, *, model_id, model, reasoning_effort="medium"):
        """Idempotently register a persisted Codex task using trusted identity.

        Args:
            task: TaskService job with its trusted platform scope.
            model_id: Host provider catalog identifier.
            model: Actual upstream model identifier.
            reasoning_effort: Model-supported reasoning setting.

        Returns:
            The persisted binding including platform identity.
        """
        for name, value in (
            ("task_id", task.task_id),
            ("model_id", model_id),
            ("model", model),
            ("reasoning_effort", reasoning_effort),
        ):
            _text(value, name)
        with self.service._transaction() as db:
            real = db.execute(
                "SELECT * FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                (
                    task.task_id,
                    task.scope.bot_id,
                    task.scope.session_key,
                    task.scope.sender_id,
                ),
            ).fetchone()
            if (
                real is None
                or json.loads(real["metadata"]).get("engine") != "codex"
                or json.loads(real["metadata"]).get("history_deleted")
            ):
                raise ValueError("A live persisted Codex task is required")
            db.execute(
                "INSERT OR IGNORE INTO pc_codex_threads(task_id,model_id,model,reasoning_effort,connection_state) VALUES(?,?,?,?,?)",
                (task.task_id, model_id, model, reasoning_effort, "disconnected"),
            )
            self._snapshot(db, task.task_id)
            return dict(self._row(db, task.scope, task.task_id))

    def get(self, scope, *, task_id="", thread_id=""):
        with self.service._transaction() as db:
            row = self._row(db, scope, task_id, thread_id)
            return dict(row) if row else None

    def list(self, scope, *, offset=0, limit=20):
        _integer(offset, "offset")
        _integer(limit, "limit")
        limit = max(1, min(limit, 50))
        with self.service._transaction() as db:
            args = (scope.bot_id, scope.session_key, scope.sender_id)
            query = " FROM pc_codex_threads c JOIN pt_tasks t ON t.task_id=c.task_id WHERE t.bot_id=? AND t.session_key=? AND t.sender_id=? AND COALESCE(json_extract(t.metadata,'$.history_deleted'),0)=0"
            total = db.execute("SELECT COUNT(*)" + query, args).fetchone()[0]
            rows = db.execute(
                "SELECT c.*,t.bot_id,t.session_key,t.sender_id"
                + query
                + " ORDER BY c.rowid LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "next_offset": offset + len(rows)
                if offset + len(rows) < total
                else None,
            }

    def attach(self, scope, task_id, *, thread_id, generation):
        _text(thread_id, "thread_id")
        _integer(generation, "generation")
        with self.service._transaction() as db:
            row = self._row(db, scope, task_id)
            if (
                row is None
                or row["generation"] != generation
                or row["thread_id"] not in (None, thread_id)
            ):
                return False
            try:
                db.execute(
                    "UPDATE pc_codex_threads SET thread_id=?,connection_state='connected' WHERE task_id=?",
                    (thread_id, task_id),
                )
            except sqlite3.IntegrityError:
                return False
            self._snapshot(db, task_id)
            return True

    def update(self, scope, task_id, *, generation, **patch):
        _integer(generation, "generation")
        allowed = {
            "turn_id",
            "model_id",
            "model",
            "reasoning_effort",
            "connection_state",
        }
        if not patch.keys() <= allowed:
            raise ValueError("Unsupported binding patch")
        for name, value in patch.items():
            _text(value, name, nullable=name == "turn_id")
        if "connection_state" in patch and patch["connection_state"] not in {
            "connected",
            "connecting",
            "disconnected",
        }:
            raise ValueError("Invalid connection_state")
        with self.service._transaction() as db:
            row = self._row(db, scope, task_id)
            if row is None or row["generation"] != generation:
                return False
            if patch:
                db.execute(
                    "UPDATE pc_codex_threads SET "
                    + ",".join(f"{key}=?" for key in patch)
                    + " WHERE task_id=?",
                    (*patch.values(), task_id),
                )
            self._snapshot(db, task_id)
            return True

    def next_generation(self, scope, task_id):
        with self.service._transaction() as db:
            row = self._row(db, scope, task_id)
            if row is None:
                return None
            generation = row["generation"] + 1
            db.execute(
                "UPDATE pc_codex_threads SET generation=?,connection_state='connecting',turn_id=NULL WHERE task_id=?",
                (generation, task_id),
            )
            self._snapshot(db, task_id)
            return generation

    def append_event(self, scope, task_id, *, generation, method, payload):
        """Persist a bounded observed event without inferring task success/failure.

        Args:
            scope: Trusted platform identity.
            task_id: Exact associated task.
            generation: Expected connection generation.
            method: Observed protocol method, never a heartbeat.
            payload: Event data excluding credentials and request envelopes.

        Returns:
            Event id, or None for stale connection or missing binding.
        """
        _integer(generation, "generation")
        _text(method, "method")
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be an object")
        if "heartbeat" in method.lower():
            raise ValueError("Heartbeat is not an execution event")
        bounded = _bounded(public_event(payload, method=method))
        encoded = json.dumps(bounded, ensure_ascii=False, allow_nan=False)
        if len(encoded) > 16000:
            bounded = {"preview": encoded[:14500], "truncated": True}
            encoded = json.dumps(bounded, ensure_ascii=False)
            while len(encoded) > 16000:
                bounded["preview"] = bounded["preview"][: len(bounded["preview"]) // 2]
                encoded = json.dumps(bounded, ensure_ascii=False)
        with self.service._transaction() as db:
            row = self._row(db, scope, task_id)
            if row is None or row["generation"] != generation:
                return None
            now = self.service.clock()
            cursor = db.execute(
                "INSERT INTO pc_codex_events(task_id,generation,turn_id,method,payload,created_at) VALUES(?,?,?,?,?,?)",
                (task_id, generation, row["turn_id"], method, encoded, now),
            )
            db.execute(
                "UPDATE pc_codex_threads SET last_event_at=? WHERE task_id=?",
                (now, task_id),
            )
            return cursor.lastrowid

    def events(self, scope, task_id, *, after=0, limit=20):
        _integer(after, "after")
        _integer(limit, "limit")
        limit = max(1, min(limit, 50))
        with self.service._transaction() as db:
            if self._row(db, scope, task_id) is None:
                raise LookupError("Codex binding not found")
            total = db.execute(
                "SELECT COUNT(*) FROM pc_codex_events WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM pc_codex_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                (task_id, after, limit),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["payload"] = _bounded(
                    public_event(json.loads(item["payload"]), method=item["method"])
                )
                if (
                    len(
                        json.dumps(
                            {
                                "items": [*items, item],
                                "total": total,
                                "next_after": item["id"],
                            },
                            ensure_ascii=False,
                        )
                    )
                    > 20000
                ):
                    break
                items.append(item)
            cursor = items[-1]["id"] if items else after
            more = db.execute(
                "SELECT 1 FROM pc_codex_events WHERE task_id=? AND id>? LIMIT 1",
                (task_id, cursor),
            ).fetchone()
            return {
                "items": items,
                "total": total,
                "next_after": cursor if more else None,
            }

    def disconnected(self, scope, task_id, *, generation):
        return self.update(
            scope, task_id, generation=generation, connection_state="disconnected"
        )
