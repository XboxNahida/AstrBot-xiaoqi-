"""Script-facing interaction data and reply receipts; no scenario decisions."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .task_service import TaskScope


class InteractionChannels:
    """Persist account-bound input streams and explicit script output requests.

    Args:
        path: Host-owned database outside task-controlled directories.
        clock: UTC epoch clock, injectable for tests.
    """

    def __init__(self, path, *, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.transaction() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ic_channels (
                    id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, session_key TEXT NOT NULL,
                    sender_id TEXT NOT NULL, origin_key TEXT NOT NULL, token TEXT NOT NULL,
                    metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    generation INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0,
                    state_json TEXT NOT NULL DEFAULT '{}', state_revision INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL,
                    UNIQUE(bot_id,session_key,sender_id,origin_key)
                );
                CREATE TABLE IF NOT EXISTS ic_events (
                    channel_id TEXT NOT NULL, seq INTEGER NOT NULL, message_id TEXT NOT NULL,
                    received_at REAL NOT NULL, text TEXT NOT NULL, components TEXT NOT NULL,
                    PRIMARY KEY(channel_id,seq), UNIQUE(channel_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS ic_replies (
                    id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, request_key TEXT NOT NULL,
                    digest TEXT NOT NULL, generation INTEGER NOT NULL, based_on_seq INTEGER NOT NULL,
                    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', token TEXT,
                    lease_until REAL, receipt TEXT, UNIQUE(channel_id,request_key)
                );
                CREATE INDEX IF NOT EXISTS ic_reply_ready ON ic_replies(status,channel_id);
            """
            )

    @contextmanager
    def transaction(self):
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

    @staticmethod
    def encode(value):
        result = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(result.encode("utf-8")) > 262144:
            raise ValueError("interaction_payload_too_large")
        return result

    @staticmethod
    def _identity(scope):
        if not isinstance(scope, TaskScope) or not all(
            isinstance(v, str) and v.strip()
            for v in (scope.bot_id, scope.session_key, scope.sender_id)
        ):
            raise ValueError("trusted_identity_required")
        return scope.bot_id, scope.session_key, scope.sender_id

    def _channel(self, db, channel_id, *, allow_paused=True):
        row = db.execute(
            "SELECT * FROM ic_channels WHERE id=? AND expires_at>? AND status!='closed'",
            (channel_id, self.clock()),
        ).fetchone()
        if row is None or (not allow_paused and row["status"] != "open"):
            raise ValueError("channel_not_open")
        data = dict(row)
        data["metadata"] = json.loads(data["metadata"])
        data["state"] = json.loads(data.pop("state_json"))
        return data

    def open(self, scope, origin_key, metadata, *, ttl_seconds=86400):
        """Open one channel idempotently using a source task's trusted identity.

        Args:
            scope: Original bot, session and real account.
            origin_key: Stable task ID and fragment chosen by the host adapter.
            metadata: Host-only request/chat snapshot and source task ID.
            ttl_seconds: Explicit positive finite channel lifetime.

        Returns:
            Internal channel record, including its script-only credential.
        """
        identity = self._identity(scope)
        if (
            type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or not math.isfinite(self.clock() + ttl_seconds)
        ):
            raise ValueError("invalid_channel_lifetime")
        if (
            not isinstance(origin_key, str)
            or not origin_key
            or len(origin_key) > 256
            or not isinstance(metadata, dict)
        ):
            raise ValueError("invalid_channel_source")
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO ic_channels(id,bot_id,session_key,sender_id,origin_key,token,metadata,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    *identity,
                    origin_key,
                    secrets.token_urlsafe(32),
                    self.encode(metadata),
                    self.clock() + ttl_seconds,
                ),
            )
            row = db.execute(
                "SELECT id FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? AND origin_key=?",
                (*identity, origin_key),
            ).fetchone()
            return self._channel(db, row["id"])

    def get(self, scope, channel_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT id FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? AND id=?",
                (*self._identity(scope), channel_id),
            ).fetchone()
            if row is None:
                return None
            try:
                return self._channel(db, row["id"])
            except ValueError:
                return None

    def list(self, scope):
        with self.transaction() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,status,seq,generation,expires_at FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? ORDER BY rowid DESC LIMIT 50",
                    self._identity(scope),
                ).fetchall()
            ]

    def authenticate(self, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            return None
        with self.transaction() as db:
            row = db.execute(
                "SELECT id,token FROM ic_channels WHERE token=?", (token,)
            ).fetchone()
            if row is None or not secrets.compare_digest(row["token"], token):
                return None
            try:
                return self._channel(db, row["id"])
            except ValueError:
                return None

    def observe(self, scope, message_id, text, components, received_at):
        """Store verified inbound content once; scripts never supply this identity.

        Args:
            scope: Real message bot/session/account from the host.
            message_id: Stable platform message ID.
            text: Message text, including an empty caption for media-only input.
            components: Observed component type names, not executable content.
            received_at: Host UTC reception timestamp.

        Returns:
            Number of channels that recorded a new message.
        """
        if not message_id:
            return 0
        updated = 0
        with self.transaction() as db:
            rows = db.execute(
                "SELECT id,seq FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? AND status!='closed' AND expires_at>?",
                (*self._identity(scope), self.clock()),
            ).fetchall()
            for row in rows:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO ic_events VALUES(?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["seq"] + 1,
                        str(message_id),
                        received_at,
                        str(text)[:16000],
                        self.encode(list(components)[:32]),
                    ),
                ).rowcount
                if inserted:
                    updated += 1
                    db.execute(
                        "UPDATE ic_channels SET seq=seq+1 WHERE id=?", (row["id"],)
                    )
                    db.execute(
                        "UPDATE ic_replies SET status='superseded' WHERE channel_id=? AND status IN ('pending','preparing')",
                        (row["id"],),
                    )
        return updated

    def events(self, channel_id, *, after_seq=0, since=None, until=None, limit=500):
        if (
            type(after_seq) is not int
            or after_seq < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid_cursor_or_limit")
        if any(
            v is not None and (type(v) not in (int, float) or not math.isfinite(v))
            for v in (since, until)
        ) or (since is not None and until is not None and since > until):
            raise ValueError("invalid_time_window")
        with self.transaction() as db:
            channel = self._channel(db, channel_id)
            params = [channel_id, after_seq]
            where = "channel_id=? AND seq>?"
            if since is not None:
                where += " AND received_at>=?"
                params.append(since)
            if until is not None:
                where += " AND received_at<=?"
                params.append(until)
            count = db.execute(
                "SELECT count(*) FROM ic_events WHERE " + where, params
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM ic_events WHERE " + where + " ORDER BY seq LIMIT ?",
                (*params, limit),
            ).fetchall()
            last = db.execute(
                "SELECT seq,text,received_at FROM ic_events WHERE channel_id=? ORDER BY seq DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
            events = [
                {
                    "seq": row["seq"],
                    "message_id": row["message_id"],
                    "received_at": row["received_at"],
                    "text": row["text"],
                    "components": json.loads(row["components"]),
                }
                for row in rows
            ]
            return {
                "channel_id": channel_id,
                "status": channel["status"],
                "generation": channel["generation"],
                "total_messages": channel["seq"],
                "messages_after_cursor": max(0, channel["seq"] - after_seq),
                "window_count": count,
                "last_message": dict(last) if last else None,
                "events": events,
                "next_cursor": events[-1]["seq"] if events else after_seq,
                "has_more": count > len(events),
                "state": channel["state"],
                "state_revision": channel["state_revision"],
                "expires_at": channel["expires_at"],
            }

    def set_state(self, channel_id, expected_revision, state):
        if type(expected_revision) is not int or not isinstance(state, dict):
            raise ValueError("invalid_script_state")
        with self.transaction() as db:
            channel = self._channel(db, channel_id)
            if channel["state_revision"] != expected_revision:
                raise ValueError("state_conflict")
            db.execute(
                "UPDATE ic_channels SET state_json=?,state_revision=state_revision+1 WHERE id=?",
                (self.encode(state), channel_id),
            )
        return {"state": state, "state_revision": expected_revision + 1}

    def control(self, channel_id, action, *, pause=True):
        if action not in {"stop", "resume", "close"} or type(pause) is not bool:
            raise ValueError("invalid_channel_control")
        with self.transaction() as db:
            channel = self._channel(db, channel_id)
            status = (
                "closed"
                if action == "close"
                else "open"
                if action == "resume" or not pause
                else "paused"
            )
            db.execute(
                "UPDATE ic_channels SET status=?,generation=generation+1 WHERE id=?",
                (status, channel_id),
            )
            stopped = db.execute(
                "UPDATE ic_replies SET status='superseded' WHERE channel_id=? AND status IN ('pending','preparing')",
                (channel_id,),
            ).rowcount
            return {
                "status": status,
                "generation": channel["generation"] + 1,
                "cancelled_pending": stopped,
            }

    def request_reply(self, channel_id, key, message, *, based_on_seq):
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or not isinstance(message, str)
            or not message.strip()
            or len(message) > 4000
            or type(based_on_seq) is not int
        ):
            raise ValueError("invalid_reply_request")
        digest = hashlib.sha256(
            self.encode([message, based_on_seq]).encode()
        ).hexdigest()
        with self.transaction() as db:
            channel = self._channel(db, channel_id, allow_paused=False)
            old = db.execute(
                "SELECT id,digest,status FROM ic_replies WHERE channel_id=? AND request_key=?",
                (channel_id, key),
            ).fetchone()
            if old:
                if old["digest"] != digest:
                    raise ValueError("request_key_conflict")
                return {"id": old["id"], "status": old["status"], "replayed": True}
            if channel["seq"] != based_on_seq:
                raise ValueError("stale_cursor")
            request_id = uuid4().hex
            db.execute(
                "INSERT INTO ic_replies(id,channel_id,request_key,digest,generation,based_on_seq,message) VALUES(?,?,?,?,?,?,?)",
                (
                    request_id,
                    channel_id,
                    key,
                    digest,
                    channel["generation"],
                    based_on_seq,
                    message.strip(),
                ),
            )
            return {"id": request_id, "status": "pending", "replayed": False}

    def reply_status(self, channel_id, request_id):
        with self.transaction() as db:
            row = db.execute(
                "SELECT id,status,receipt FROM ic_replies WHERE channel_id=? AND id=?",
                (channel_id, request_id),
            ).fetchone()
            if row is None:
                raise ValueError("reply_not_found")
            return {
                "id": row["id"],
                "status": row["status"],
                "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            }

    def claim_reply(self):
        with self.transaction() as db:
            db.execute(
                "UPDATE ic_replies SET status='unknown' WHERE status='sending' AND lease_until<=?",
                (self.clock(),),
            )
            db.execute(
                "UPDATE ic_replies SET status='pending' WHERE status='preparing' AND lease_until<=?",
                (self.clock(),),
            )
            row = db.execute(
                "SELECT r.* FROM ic_replies r JOIN ic_channels c ON c.id=r.channel_id WHERE r.status='pending' AND c.status='open' AND c.expires_at>? AND r.generation=c.generation AND r.based_on_seq=c.seq ORDER BY r.rowid LIMIT 1",
                (self.clock(),),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute(
                "UPDATE ic_replies SET status='preparing',token=?,lease_until=? WHERE id=?",
                (token, self.clock() + 180, row["id"]),
            )
            return {
                **dict(row),
                "token": token,
                "channel": self._channel(db, row["channel_id"]),
            }

    def begin_reply(self, reply):
        with self.transaction() as db:
            return bool(
                db.execute(
                    "UPDATE ic_replies SET status='sending',lease_until=? WHERE id=? AND token=? AND status='preparing' AND lease_until>? AND EXISTS(SELECT 1 FROM ic_channels c WHERE c.id=ic_replies.channel_id AND c.status='open' AND c.expires_at>? AND c.generation=ic_replies.generation AND c.seq=ic_replies.based_on_seq)",
                    (
                        self.clock() + 180,
                        reply["id"],
                        reply["token"],
                        self.clock(),
                        self.clock(),
                    ),
                ).rowcount
            )

    def finish_reply(self, reply, status):
        if status not in {"sent", "failed", "unknown"}:
            raise ValueError("invalid_delivery_status")
        with self.transaction() as db:
            return bool(
                db.execute(
                    "UPDATE ic_replies SET status=?,receipt=? WHERE id=? AND token=? AND status IN ('preparing','sending')",
                    (
                        status,
                        self.encode({"status": status, "at": self.clock()}),
                        reply["id"],
                        reply["token"],
                    ),
                ).rowcount
            )
