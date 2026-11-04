"""Run explicitly supplied scripts with existing restricted task identities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from uuid import uuid4


class ScriptScheduleError(ValueError):
    """Provide factual schedule constraints without replacing the caller's plan."""

    def __init__(self, code, **details):
        super().__init__(code)
        self.details = details


class InteractionScripts:
    """Persist script starts and receipts without deciding interaction behavior.

    Args:
        api: Owner-bound interaction API with access to the existing task broker.
    """

    def __init__(self, api, *, on_script_event=None):
        self.api = api
        self.running = {}
        self.stop_event = asyncio.Event()
        self.on_script_event = on_script_event
        self._event_lock = asyncio.Lock()
        with api.channels.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ic_scripts (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, request_key TEXT NOT NULL,
                digest TEXT NOT NULL, path TEXT NOT NULL, arguments TEXT NOT NULL,
                run_at REAL NOT NULL, timeout REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                exit_code INTEGER, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
                UNIQUE(channel_id,request_key)
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(ic_scripts)")}
            for name, kind in (("finished_at", "REAL"), ("error_code", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE ic_scripts ADD COLUMN {name} {kind}")
            db.execute("""CREATE TABLE IF NOT EXISTS ic_script_events (
                event_id TEXT PRIMARY KEY, script_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                status TEXT NOT NULL, receipt TEXT NOT NULL, recorded_at REAL NOT NULL,
                callback_status TEXT NOT NULL DEFAULT 'pending', notified_at REAL
            )""")
            # Preserve historical facts without starting new repair work for old
            # failures when this event interface is first installed.
            self._record_events(db, notify=False)
            db.execute(
                "UPDATE ic_scripts SET status='interrupted',error_code='service_restarted',finished_at=? WHERE status IN ('starting','running')",
                (api.channels.clock(),),
            )
            self._record_events(db)

    def _record_events(self, db, *, notify=True):
        rows = db.execute("""SELECT s.* FROM ic_scripts s
            WHERE s.status IN ('finished','failed','interrupted','expired','permission_denied','cancelled')
            AND NOT EXISTS(SELECT 1 FROM ic_script_events e WHERE e.event_id='script:'||s.id||':'||s.status)""").fetchall()
        for row in rows:
            receipt = {key: row[key] for key in (
                "id", "path", "run_at", "timeout", "status", "exit_code",
                "stdout", "stderr", "finished_at", "error_code",
            )}
            db.execute(
                "INSERT INTO ic_script_events(event_id,script_id,channel_id,status,receipt,recorded_at,callback_status) VALUES(?,?,?,?,?,?,?)",
                (f"script:{row['id']}:{row['status']}", row["id"], row["channel_id"],
                 row["status"], self.api.channels.encode(receipt), self.api.channels.clock(),
                 "pending" if notify and row["status"] in {"failed", "interrupted", "expired"} else "receipt"),
            )

    async def _dispatch_events(self):
        """Notify an injected trusted host callback; never rerun scripts or send chat.

        The callback must durably accept the stable event_id idempotently and
        return True. A crash before acknowledgment may redeliver that same ID.
        Current identity authorization is rechecked before every delivery.
        """
        if not callable(self.on_script_event):
            return False
        delivered = False
        async with self._event_lock:
            with self.api.channels.transaction() as db:
                rows = db.execute("""SELECT e.*,c.bot_id,c.session_key,c.sender_id
                    FROM ic_script_events e JOIN ic_channels c ON c.id=e.channel_id
                    WHERE e.callback_status='pending' ORDER BY e.rowid LIMIT 50""").fetchall()
            from .task_service import TaskScope

            for row in rows:
                scope = TaskScope(row["bot_id"], row["session_key"], row["sender_id"])
                channel = self.api.channels.get(scope, row["channel_id"], include_inactive=True)
                if channel is None or channel["status"] == "closed" or self.api.runtime.interaction_allowed(channel) is not True:
                    outcome = "suppressed"
                else:
                    public_channel = {k: v for k, v in channel.items() if k != "token"}
                    event = {
                        "event_id": row["event_id"], "script_id": row["script_id"],
                        "channel_id": row["channel_id"], "status": row["status"],
                        "recorded_at": row["recorded_at"], "channel": public_channel,
                        "receipt": json.loads(row["receipt"]),
                    }
                    try:
                        if await self.on_script_event(event) is not True:
                            continue
                    except Exception:
                        # Preserve the event for retry without hiding its receipt.
                        continue
                    outcome = "delivered"
                with self.api.channels.transaction() as db:
                    db.execute(
                        "UPDATE ic_script_events SET callback_status=?,notified_at=? WHERE event_id=? AND callback_status='pending'",
                        (outcome, self.api.channels.clock(), row["event_id"]),
                    )
                delivered = True
        return delivered

    async def schedule(
        self, channel, *, path, key, arguments=None, run_at=None, timeout=600
    ):
        """Schedule a caller-authored script, preserving its exact argument vector.

        Args:
            channel: Authenticated channel, not a user-supplied identity.
            path: Python or PowerShell script relative to the original workspace.
            key: Stable operation key; retries cannot start duplicate processes.
            arguments: Explicit string argument vector.
            run_at: UTC epoch or timezone-aware ISO datetime; None starts promptly.
            timeout: Existing broker limit, up to 3600 seconds for each invocation.

        Returns:
            Durable start ID and state, never a claim that execution completed.
        """
        if self.api.runtime.interaction_allowed(channel) is not True:
            raise PermissionError("permission_denied")
        if (
            not isinstance(path, str)
            or len(path) > 1024
            or Path(path).suffix.lower() not in {".py", ".ps1"}
            or not isinstance(key, str)
            or not 1 <= len(key) <= 128
        ):
            raise ValueError("invalid_script_request")
        arguments = [] if arguments is None else arguments
        if (
            not isinstance(arguments, list)
            or len(arguments) > 64
            or any(
                not isinstance(v, str) or len(v) > 4000 or "\0" in v for v in arguments
            )
        ):
            raise ValueError("invalid_script_arguments")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("invalid_script_timeout")
        if timeout > 3600:
            raise ScriptScheduleError(
                "script_timeout_exceeds_execution_limit", execution_timeout_limit=3600,
                hint="The Windows execution broker currently limits each process job to 3600 seconds. Save state and choose further scheduled invocations if needed; this is not the channel lifetime.",
            )
        if isinstance(run_at, str):
            parsed = datetime.fromisoformat(run_at)
            if parsed.tzinfo is None:
                raise ValueError("run_at_requires_timezone")
            run_at = parsed.timestamp()
        if run_at is not None and (
            type(run_at) not in (int, float) or not math.isfinite(run_at)
        ):
            raise ValueError("invalid_run_at")
        actor = self.api.runtime.interaction_actor(channel)
        broker = self.api.runtime.get_execution_broker(actor)
        checked = await asyncio.to_thread(
            broker.checked_path, actor, "read", path, _verify=False
        )
        if not checked.is_file():
            raise ValueError("script_file_not_found")
        digest = hashlib.sha256(
            self.api.channels.encode([path, arguments, run_at, timeout]).encode()
        ).hexdigest()
        with self.api.channels.transaction() as db:
            current = self.api.channels._channel(db, channel["id"])
            if run_at is not None and run_at >= current["expires_at"]:
                raise ScriptScheduleError(
                    "script_starts_after_channel_expiry",
                    expires_at=current["expires_at"], run_at=run_at,
                    hint="The requested run_at is outside this channel lifetime. Inspect list_scripts before changing existing work; renew this channel with ttl_seconds covering the intended plan, or choose another run_at. Reuse existing channel and script IDs; do not duplicate already pending scripts.",
                )
            old = db.execute(
                "SELECT id,digest FROM ic_scripts WHERE channel_id=? AND request_key=?",
                (channel["id"], key),
            ).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ValueError("request_key_conflict")
                script_id = old["id"]
            else:
                script_id = uuid4().hex
                db.execute(
                    "INSERT INTO ic_scripts(id,channel_id,request_key,digest,path,arguments,run_at,timeout) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        script_id,
                        channel["id"],
                        key,
                        digest,
                        path,
                        self.api.channels.encode(arguments),
                        self.api.channels.clock() if run_at is None else run_at,
                        timeout,
                    ),
                )
        return self.status(channel, script_id)

    def status(self, channel, script_id):
        with self.api.channels.transaction() as db:
            row = db.execute(
                "SELECT id,path,run_at,timeout,status,exit_code,stdout,stderr,finished_at,error_code FROM ic_scripts WHERE id=? AND channel_id=?",
                (script_id, channel["id"]),
            ).fetchone()
            if row is None:
                raise ValueError("script_not_found")
            return dict(row)

    def list(self, channel, *, offset=0, limit=50):
        """Discover existing starts in this authenticated channel, without running them."""
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_script_page")
        with self.api.channels.transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,path,run_at,timeout,status,exit_code,stdout,stderr,finished_at,error_code FROM ic_scripts WHERE channel_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (channel["id"], limit, offset),
            ).fetchall()]

    async def cancel(self, channel, script_id):
        self.status(channel, script_id)
        with self.api.channels.transaction() as db:
            db.execute(
                "UPDATE ic_scripts SET status='cancelled',finished_at=? WHERE id=? AND channel_id=? AND status IN ('pending','starting','running')",
                (self.api.channels.clock(), script_id, channel["id"]),
            )
            self._record_events(db)
        task = self.running.get(script_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.status(channel, script_id)

    async def stop_channel(self, channel_id):
        with self.api.channels.transaction() as db:
            rows = db.execute(
                "SELECT id FROM ic_scripts WHERE channel_id=? AND status IN ('pending','starting','running')",
                (channel_id,),
            ).fetchall()
            db.execute(
                "UPDATE ic_scripts SET status='cancelled',finished_at=? WHERE channel_id=? AND status IN ('pending','starting','running')",
                (self.api.channels.clock(), channel_id),
            )
            self._record_events(db)
        tasks = [self.running[row["id"]] for row in rows if row["id"] in self.running]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self):
        self.running = {
            key: task for key, task in self.running.items() if not task.done()
        }
        with self.api.channels.transaction() as db:
            expired = db.execute(
                "UPDATE ic_scripts SET status='expired',error_code='channel_expired',finished_at=? WHERE status='pending' AND EXISTS(SELECT 1 FROM ic_channels c WHERE c.id=ic_scripts.channel_id AND c.expires_at<=?)",
                (self.api.channels.clock(), self.api.channels.clock()),
            ).rowcount
            self._record_events(db)
            row = db.execute(
                "SELECT s.* FROM ic_scripts s JOIN ic_channels c ON c.id=s.channel_id WHERE s.status='pending' AND s.run_at<=? AND c.status!='closed' AND c.expires_at>? AND NOT EXISTS(SELECT 1 FROM ic_scripts active WHERE active.channel_id=s.channel_id AND active.status IN ('starting','running')) ORDER BY s.run_at,s.rowid LIMIT 1",
                (self.api.channels.clock(), self.api.channels.clock()),
            ).fetchone()
            if row is not None:
                channel = self.api.channels._channel(db, row["channel_id"])
                db.execute(
                    "UPDATE ic_scripts SET status='starting' WHERE id=? AND status='pending'",
                    (row["id"],),
                )
                record = dict(row)
        if row is None:
            dispatched = await self._dispatch_events()
            return bool(expired or dispatched)
        if self.api.runtime.interaction_allowed(channel) is not True:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status='permission_denied',error_code='permission_denied',finished_at=? WHERE id=?",
                    (self.api.channels.clock(), record["id"]),
                )
                self._record_events(db)
            return True
        self.running[record["id"]] = asyncio.create_task(self._run(record, channel))
        await self._dispatch_events()
        return True

    async def _run(self, record, channel):
        try:
            actor = self.api.runtime.interaction_actor(channel)
            broker = self.api.runtime.get_execution_broker(actor)
            target = await asyncio.to_thread(
                broker.checked_path, actor, "read", record["path"], _verify=False
            )
            root = await asyncio.to_thread(
                broker.checked_path, actor, "shell", ".", _verify=False
            )
            config = await self.api.runtime.prepare_interaction_config(channel)
            refreshed = self.api.channels.authenticate(channel["token"])
            if (
                refreshed is None
                or self.api.runtime.interaction_allowed(refreshed) is not True
            ):
                raise PermissionError("permission_denied")
            with self.api.channels.transaction() as db:
                if not db.execute(
                    "UPDATE ic_scripts SET status='running' WHERE id=? AND status='starting'",
                    (record["id"],),
                ).rowcount:
                    return
            arguments = json.loads(record["arguments"])
            interpreter = root / ".venv/Scripts/python.exe"
            if not interpreter.is_file():
                interpreter = root / ".runtime/python.exe"
            argv = (
                [str(interpreter), "-X", "utf8", str(target), *arguments]
                if target.suffix.lower() == ".py"
                else [str(target), *arguments]
            )
            quoted = ["'" + value.replace("'", "''") + "'" for value in argv]
            sdk_dir = self.api.runtime.workspace / "tools"
            command = (
                "$ErrorActionPreference='Stop'; $env:ASTRBOT_INTERACTION_CONFIG='"
                + str(config).replace("'", "''")
                + "'; $env:PYTHONPATH='"
                + str(sdk_dir).replace("'", "''")
                + "'; & "
                + " ".join(quoted)
                + "; exit $LASTEXITCODE"
            )
            result = await broker.run_powershell(
                actor, command, timeout=record["timeout"]
            )
            stdout = self.api.runtime._safe_text(
                str(result.stdout).replace(channel["token"], "[redacted]"), 12000
            )
            stderr = self.api.runtime._safe_text(
                str(result.stderr).replace(channel["token"], "[redacted]"), 4000
            )
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status=?,exit_code=?,stdout=?,stderr=?,finished_at=?,error_code=? WHERE id=? AND status='running'",
                    (
                        "finished" if result.exit_code == 0 else "failed",
                        result.exit_code,
                        stdout,
                        stderr,
                        self.api.channels.clock(),
                        None if result.exit_code == 0 else "script_exit_nonzero",
                        record["id"],
                    ),
                )
                self._record_events(db)
        except asyncio.CancelledError:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status='interrupted',error_code='script_interrupted',finished_at=? WHERE id=? AND status IN ('starting','running')",
                    (self.api.channels.clock(), record["id"]),
                )
                self._record_events(db)
            raise
        except Exception as exc:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status=?,stderr=?,error_code=?,finished_at=? WHERE id=? AND status IN ('starting','running')",
                    (
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "failed",
                        self.api.runtime._safe_text(
                            (type(exc).__name__ + ": " + str(exc)).replace(channel["token"], "[redacted]"), 4000,
                        ),
                        "script_timeout" if isinstance(exc, TimeoutError) else type(exc).__name__,
                        self.api.channels.clock(),
                        record["id"],
                    ),
                )
                self._record_events(db)

    async def loop(self):
        while not self.stop_event.is_set():
            if await self.tick():
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), 1)

    async def stop(self):
        self.stop_event.set()
        for task in self.running.values():
            task.cancel()
        await asyncio.gather(*self.running.values(), return_exceptions=True)
