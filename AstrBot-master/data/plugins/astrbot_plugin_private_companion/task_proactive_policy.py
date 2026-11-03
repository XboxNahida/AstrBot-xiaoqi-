"""Data-driven delivery gates; the host owns identity, cron and actual sending."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .task_service import TaskScope


@dataclass(frozen=True)
class RuleConfig:
    timezone: str = "Asia/Shanghai"
    windows: tuple[tuple[str, str], ...] = ()
    max_deliveries: int | None = None
    min_interval_seconds: float = 0
    enabled: bool = True
    stopped: bool = False
    only_updates: bool = True
    expires_at: float | None = None


@dataclass(frozen=True)
class DeliveryLease:
    scope: TaskScope
    existing_rule_id: str
    token: str
    fingerprint: str


class ProactivePolicyStore:
    """Persist owner-scoped gates without registering or executing cron jobs."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        lease_seconds: float = 60,
    ):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("Lease must be finite and positive")
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lease_seconds = lease_seconds
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pt_proactive_rules (
                bot_id TEXT NOT NULL, session_key TEXT NOT NULL,
                sender_id TEXT NOT NULL, existing_rule_id TEXT NOT NULL,
                config TEXT NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0,
                last_sent_at REAL, last_fingerprint TEXT,
                state TEXT NOT NULL DEFAULT 'idle', token TEXT,
                pending_fingerprint TEXT, lease_until REAL,
                PRIMARY KEY(bot_id, session_key, sender_id, existing_rule_id)
            )""")

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

    def _key(self, scope: TaskScope, existing_rule_id: str) -> tuple[str, ...]:
        if not isinstance(scope, TaskScope):
            raise ValueError("Scope must come from trusted host identity")
        key = (scope.bot_id, scope.session_key, scope.sender_id, existing_rule_id)
        if any(not isinstance(value, str) or not value.strip() for value in key):
            raise ValueError("Owner identity and existing cron reference are required")
        return key

    def save(self, scope: TaskScope, existing_rule_id: str, config: RuleConfig):
        """Save current policy while retaining receipts and unresolved leases.

        Args:
            scope: Host-verified owner, never taken from model-generated fields.
            existing_rule_id: Existing core/cron ID validated by the host.
            config: Complete replacement policy; None count means unlimited.

        Raises:
            ValueError: A rule field is invalid.
        """
        key = self._key(scope, existing_rule_id)
        try:
            ZoneInfo(config.timezone)
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid timezone") from exc
        if any(
            type(value) is not bool
            for value in (config.enabled, config.stopped, config.only_updates)
        ):
            raise ValueError("Policy switches must be booleans")
        if config.max_deliveries is not None and (
            type(config.max_deliveries) is not int or config.max_deliveries < 0
        ):
            raise ValueError("Maximum must be a nonnegative integer or None")
        for value in (config.min_interval_seconds, config.expires_at):
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value)
            ):
                raise ValueError("Intervals and expiry must be finite numbers")
        if config.min_interval_seconds is None or config.min_interval_seconds < 0:
            raise ValueError("Minimum interval must be nonnegative")
        for window in config.windows:
            if len(window) != 2:
                raise ValueError("Windows require start and end")
            for value in window:
                if not isinstance(value, str):
                    raise ValueError("Window time must use HH:MM")
                parsed = datetime.strptime(value, "%H:%M")
                if parsed.strftime("%H:%M") != value:
                    raise ValueError("Window time must use HH:MM")
            if window[0] == window[1]:
                raise ValueError("Empty window; use no windows for unrestricted time")
        with self._transaction() as db:
            db.execute(
                "INSERT INTO pt_proactive_rules "
                "(bot_id,session_key,sender_id,existing_rule_id,config) "
                "VALUES(?,?,?,?,?) ON CONFLICT "
                "(bot_id,session_key,sender_id,existing_rule_id) "
                "DO UPDATE SET config=excluded.config",
                (*key, json.dumps(asdict(config))),
            )

    def get(self, scope: TaskScope, existing_rule_id: str) -> dict | None:
        """Read this owner's rule, counters and delivery state.

        Args:
            scope: Host-verified owner.
            existing_rule_id: Existing cron reference.

        Returns:
            Persisted snapshot, or None for an absent owner-scoped rule.
        """
        key = self._key(scope, existing_rule_id)
        with self._transaction() as db:
            db.execute(
                "UPDATE pt_proactive_rules SET state='unknown_result' WHERE "
                "bot_id=? AND session_key=? AND sender_id=? AND existing_rule_id=? "
                "AND state='reserved' AND lease_until<=?",
                (*key, self.clock()),
            )
            row = db.execute(
                "SELECT * FROM pt_proactive_rules WHERE "
                "bot_id=? AND session_key=? AND sender_id=? AND existing_rule_id=?",
                key,
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["config"] = json.loads(result["config"])
        return result

    def reserve_delivery(
        self,
        scope: TaskScope,
        existing_rule_id: str,
        fingerprint: str,
        *,
        has_update: bool = True,
    ) -> DeliveryLease | None:
        """Atomically reserve one send after the existing cron query completes.

        Args:
            scope: Host-verified cron owner.
            existing_rule_id: Existing cron reference; no schedule is created.
            fingerprint: Nonempty stable content fingerprint, computed by the host.
            has_update: Explicit query outcome; False never permits delivery.

        Returns:
            Fenced lease, or None when any current policy condition blocks sending.
        """
        key = self._key(scope, existing_rule_id)
        if (
            has_update is not True
            or not isinstance(fingerprint, str)
            or not fingerprint.strip()
        ):
            return None
        now = self.clock()
        where = "bot_id=? AND session_key=? AND sender_id=? AND existing_rule_id=?"
        with self._transaction() as db:
            db.execute(
                f"UPDATE pt_proactive_rules SET state='unknown_result' WHERE {where} "
                "AND state='reserved' AND lease_until<=?",
                (*key, now),
            )
            row = db.execute(
                f"SELECT * FROM pt_proactive_rules WHERE {where}", key
            ).fetchone()
            if row is None or row["state"] != "idle":
                return None
            config = json.loads(row["config"])
            if (
                not config["enabled"]
                or config["stopped"]
                or (config["expires_at"] is not None and now >= config["expires_at"])
                or (
                    config["max_deliveries"] is not None
                    and row["sent_count"] >= config["max_deliveries"]
                )
                or (
                    row["last_sent_at"] is not None
                    and now - row["last_sent_at"] < config["min_interval_seconds"]
                )
                or (config["only_updates"] and fingerprint == row["last_fingerprint"])
            ):
                return None
            local = datetime.fromtimestamp(now, ZoneInfo(config["timezone"]))
            current = local.strftime("%H:%M")
            if config["windows"] and not any(
                start <= current < end
                if start < end
                else current >= start or current < end
                for start, end in config["windows"]
            ):
                return None
            token = uuid4().hex
            db.execute(
                f"UPDATE pt_proactive_rules SET state='reserved',token=?, "
                f"pending_fingerprint=?,lease_until=? WHERE {where}",
                (token, fingerprint, now + self.lease_seconds, *key),
            )
            return DeliveryLease(scope, existing_rule_id, token, fingerprint)

    def ack(self, lease: DeliveryLease) -> bool:
        """Record confirmed delivery only while the reserved lease is live."""
        return self._settle(lease, delivered=True)

    def fail(self, lease: DeliveryLease, *, outcome_unknown: bool = True) -> bool:
        """Block unknown outcomes; release only a host-confirmed unsent attempt."""
        if type(outcome_unknown) is not bool:
            raise ValueError("Failure outcome must be a boolean")
        return self._settle(lease, delivered=None if outcome_unknown else False)

    def reconcile_delivery(self, lease: DeliveryLease, *, delivered: bool) -> bool:
        """Resolve an unknown outcome using external delivery evidence.

        Args:
            lease: Original owner-bound lease, retained by the trusted host.
            delivered: True for confirmed receipt; False for confirmed non-delivery.

        Returns:
            Whether this unresolved lease was reconciled exactly once.
        """
        if type(delivered) is not bool:
            raise ValueError("Reconciliation requires a definite outcome")
        return self._settle(lease, delivered=delivered, reconcile=True)

    def _settle(self, lease, *, delivered, reconcile=False):
        key = self._key(lease.scope, lease.existing_rule_id)
        now = self.clock()
        where = "bot_id=? AND session_key=? AND sender_id=? AND existing_rule_id=?"
        with self._transaction() as db:
            row = db.execute(
                f"SELECT * FROM pt_proactive_rules WHERE {where} "
                "AND token=? AND pending_fingerprint=?",
                (*key, lease.token, lease.fingerprint),
            ).fetchone()
            if row is None or row["state"] == "idle":
                return False
            unknown = row["state"] == "unknown_result" or now >= row["lease_until"]
            if unknown and not reconcile:
                db.execute(
                    f"UPDATE pt_proactive_rules SET state='unknown_result' WHERE {where}",
                    key,
                )
                return False
            if reconcile and not unknown:
                return False
            if delivered is None:
                db.execute(
                    f"UPDATE pt_proactive_rules SET state='unknown_result' WHERE {where}",
                    key,
                )
            else:
                if delivered:
                    db.execute(
                        f"UPDATE pt_proactive_rules SET sent_count=sent_count+1, "
                        f"last_sent_at=?,last_fingerprint=? WHERE {where}",
                        (now, lease.fingerprint, *key),
                    )
                db.execute(
                    f"UPDATE pt_proactive_rules SET state='idle',token=NULL, "
                    f"pending_fingerprint=NULL,lease_until=NULL WHERE {where}",
                    key,
                )
            return True
