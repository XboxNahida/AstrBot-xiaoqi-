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


class InteractionScripts:
    """Persist script starts and receipts without deciding interaction behavior.

    Args:
        api: Owner-bound interaction API with access to the existing task broker.
    """

    def __init__(self, api):
        self.api = api
        self.running = {}
        self.stop_event = asyncio.Event()
        with api.channels.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ic_scripts (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, request_key TEXT NOT NULL,
                digest TEXT NOT NULL, path TEXT NOT NULL, arguments TEXT NOT NULL,
                run_at REAL NOT NULL, timeout REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                exit_code INTEGER, stdout TEXT NOT NULL DEFAULT '', stderr TEXT NOT NULL DEFAULT '',
                UNIQUE(channel_id,request_key)
            )""")
            db.execute(
                "UPDATE ic_scripts SET status='interrupted' WHERE status IN ('starting','running')"
            )

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
            or not 0 < timeout <= 3600
        ):
            raise ValueError("invalid_script_timeout")
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
                raise ValueError("script_starts_after_channel_expiry")
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
                "SELECT id,path,run_at,timeout,status,exit_code,stdout,stderr FROM ic_scripts WHERE id=? AND channel_id=?",
                (script_id, channel["id"]),
            ).fetchone()
            if row is None:
                raise ValueError("script_not_found")
            return dict(row)

    async def cancel(self, channel, script_id):
        self.status(channel, script_id)
        with self.api.channels.transaction() as db:
            db.execute(
                "UPDATE ic_scripts SET status='cancelled' WHERE id=? AND channel_id=? AND status IN ('pending','starting','running')",
                (script_id, channel["id"]),
            )
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
                "UPDATE ic_scripts SET status='cancelled' WHERE channel_id=? AND status IN ('pending','starting','running')",
                (channel_id,),
            )
        tasks = [self.running[row["id"]] for row in rows if row["id"] in self.running]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self):
        self.running = {
            key: task for key, task in self.running.items() if not task.done()
        }
        with self.api.channels.transaction() as db:
            row = db.execute(
                "SELECT s.* FROM ic_scripts s JOIN ic_channels c ON c.id=s.channel_id WHERE s.status='pending' AND s.run_at<=? AND c.status!='closed' AND c.expires_at>? AND NOT EXISTS(SELECT 1 FROM ic_scripts active WHERE active.channel_id=s.channel_id AND active.status IN ('starting','running')) ORDER BY s.run_at,s.rowid LIMIT 1",
                (self.api.channels.clock(), self.api.channels.clock()),
            ).fetchone()
            if row is None:
                return False
            channel = self.api.channels._channel(db, row["channel_id"])
            db.execute(
                "UPDATE ic_scripts SET status='starting' WHERE id=? AND status='pending'",
                (row["id"],),
            )
            record = dict(row)
        if self.api.runtime.interaction_allowed(channel) is not True:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status='permission_denied' WHERE id=?",
                    (record["id"],),
                )
            return True
        self.running[record["id"]] = asyncio.create_task(self._run(record, channel))
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
                    "UPDATE ic_scripts SET status=?,exit_code=?,stdout=?,stderr=? WHERE id=? AND status='running'",
                    (
                        "finished" if result.exit_code == 0 else "failed",
                        result.exit_code,
                        stdout,
                        stderr,
                        record["id"],
                    ),
                )
        except asyncio.CancelledError:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status='interrupted' WHERE id=? AND status IN ('starting','running')",
                    (record["id"],),
                )
            raise
        except Exception as exc:
            with self.api.channels.transaction() as db:
                db.execute(
                    "UPDATE ic_scripts SET status=?,stderr=? WHERE id=? AND status IN ('starting','running')",
                    (
                        "permission_denied"
                        if isinstance(exc, PermissionError)
                        else "failed",
                        type(exc).__name__,
                        record["id"],
                    ),
                )

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
