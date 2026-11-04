"""Persist task grants; trusted host code supplies identities and policy.

Path validation is an authorization check, not an OS or shell sandbox. The host
must enforce process/ACL isolation and recheck immediately before each tool call.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class TaskActor:
    """Carry identity verified by the host, never reconstructed from model data.

    Args:
        bot_id: Trusted bot instance identifier.
        group_id: Trusted group identifier, empty for private conversations.
        user_id: Real platform account identifier, never a display name.
        is_primary_user: Host-verified primary-user status.
    """

    bot_id: str
    group_id: str
    user_id: str
    is_primary_user: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bot_id, str)
            or not self.bot_id.strip()
            or not isinstance(self.user_id, str)
            or not self.user_id.strip()
            or not isinstance(self.group_id, str)
            or type(self.is_primary_user) is not bool
        ):
            raise ValueError("A trusted actor requires explicit account identifiers")


@dataclass(frozen=True)
class TaskGrant:
    """Represent one persisted group grant and its revocation audit fields."""

    grant_id: str
    bot_id: str
    group_id: str
    user_id: str
    granted_by: str
    capabilities: frozenset[str]
    roots: tuple[Path, ...]
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    revoked_by: str | None


class TaskPermissionStore:
    """Store grants with short SQLite connections and deny-by-default host policy.

    Args:
        db_path: Dedicated permission database file controlled by the host.
        allowed_roots: Absolute task workspace roots approved by the host.
        allowed_capabilities: Host-approved task capabilities; bot_admin is forbidden.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        allowed_roots: Iterable[str | Path] = (),
        allowed_capabilities: Iterable[str] = (),
        whole_task_grants: bool = False,
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.whole_task_grants = bool(whole_task_grants)
        self.allowed_capabilities = frozenset(allowed_capabilities)
        if "bot_admin" in self.allowed_capabilities or any(
            not isinstance(value, str) or not value.strip()
            for value in self.allowed_capabilities
        ):
            raise ValueError("Host policy must contain task capabilities only")
        roots = tuple(Path(root) for root in allowed_roots)
        if any(not root.is_absolute() for root in roots):
            raise ValueError("Host roots must be absolute")
        self.allowed_roots = tuple(root.resolve() for root in roots)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS task_permission_grants (
                    grant_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL,
                    group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    granted_by TEXT NOT NULL, capabilities TEXT NOT NULL,
                    roots TEXT NOT NULL, created_at REAL NOT NULL,
                    expires_at REAL, revoked_at REAL, revoked_by TEXT
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS task_permission_scope "
                "ON task_permission_grants(bot_id, group_id, user_id)"
            )

    def grant(
        self,
        actor: TaskActor,
        user_id: str,
        *,
        capabilities: Iterable[str],
        roots: Iterable[str | Path],
        expires_at: float | None = None,
    ) -> TaskGrant:
        """Create a group grant within the host policy.

        Args:
            actor: Host-verified primary user in the target bot and group.
            user_id: Real account identifier selected by the primary user.
            capabilities: Explicit task capabilities to grant.
            roots: Absolute directories to grant for these capabilities.
            expires_at: Future Unix UTC timestamp, or None until revoked.

        Returns:
            The newly persisted grant.

        Raises:
            PermissionError: Actor, capability, or root exceeds the allowed scope.
            ValueError: Target, roots, or expiry is invalid.
        """
        if (
            not isinstance(actor, TaskActor)
            or not actor.is_primary_user
            or not actor.group_id.strip()
        ):
            raise PermissionError("Only the primary user may grant group execution")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("A real target account is required")
        if isinstance(capabilities, str):
            raise ValueError("Capabilities must be a collection")
        requested = frozenset(capabilities)
        if (
            not requested
            or "bot_admin" in requested
            or not requested <= self.allowed_capabilities
        ):
            raise PermissionError("Capabilities exceed host policy")
        paths = tuple(Path(root) for root in roots)
        if not paths or any(not path.is_absolute() for path in paths):
            raise ValueError("Grant roots must be nonempty absolute paths")
        resolved = tuple(path.resolve() for path in paths)
        if any(
            not any(path.is_relative_to(root) for root in self.allowed_roots)
            for path in resolved
        ):
            raise PermissionError("Grant roots exceed host policy")
        now = time.time()
        if expires_at is not None and (
            type(expires_at) not in (int, float)
            or not math.isfinite(expires_at)
            or expires_at <= now
        ):
            raise ValueError("Expiry must be a finite future timestamp")
        grant = TaskGrant(
            uuid4().hex,
            actor.bot_id,
            actor.group_id,
            user_id,
            actor.user_id,
            requested,
            resolved,
            now,
            expires_at,
            None,
            None,
        )
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "INSERT INTO task_permission_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    grant.grant_id,
                    grant.bot_id,
                    grant.group_id,
                    user_id,
                    actor.user_id,
                    json.dumps(sorted(requested)),
                    json.dumps([str(path) for path in resolved]),
                    now,
                    expires_at,
                    None,
                    None,
                ),
            )
        return grant

    def revoke(self, actor: TaskActor, grant_id: str) -> bool:
        """Revoke a grant in this primary user's selected bot and group.

        Args:
            actor: Host-verified primary user.
            grant_id: Identifier returned by grant.

        Returns:
            Whether an active record in the selected scope was revoked.

        Raises:
            PermissionError: Actor is not a primary user in a group.
        """
        if (
            not isinstance(actor, TaskActor)
            or not actor.is_primary_user
            or not actor.group_id.strip()
        ):
            raise PermissionError("Only the primary user may revoke group execution")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            changed = db.execute(
                "UPDATE task_permission_grants SET revoked_at=?, revoked_by=? "
                "WHERE grant_id=? AND bot_id=? AND group_id=? AND revoked_at IS NULL",
                (time.time(), actor.user_id, grant_id, actor.bot_id, actor.group_id),
            ).rowcount
        return changed == 1

    def list(self, actor: TaskActor, *, user_id: str | None = None) -> list[TaskGrant]:
        """Read audit records within the bot/group, including expired/revoked grants.

        Args:
            actor: Trusted requesting account; members can only list their own grants.
            user_id: Optional primary-user filter for the real recipient account.

        Returns:
            Scope-filtered immutable grant records.
        """
        if not isinstance(actor, TaskActor) or not actor.group_id.strip():
            return []
        if not actor.is_primary_user and user_id not in (None, actor.user_id):
            return []
        target = user_id if actor.is_primary_user else actor.user_id
        query = "SELECT * FROM task_permission_grants WHERE bot_id=? AND group_id=?"
        args = [actor.bot_id, actor.group_id]
        if target is not None:
            query += " AND user_id=?"
            args.append(target)
        query += " ORDER BY created_at, grant_id"
        with closing(sqlite3.connect(self.db_path)) as db:
            rows = db.execute(query, args).fetchall()
        return [
            TaskGrant(
                *row[:5],
                frozenset(json.loads(row[5])),
                tuple(Path(path) for path in json.loads(row[6])),
                *row[7:],
            )
            for row in rows
        ]

    def check(self, actor: TaskActor, capability: str, path: str | Path) -> bool:
        """Authorize one operation against current policy and fresh persisted grants.

        Args:
            actor: Host-verified identity, never supplied by a model.
            capability: One requested capability, including network or shell.
            path: Absolute target path or task work directory for non-file operations.

        Returns:
            True only within both host policy and a current actor grant. This does
            not constrain arbitrary shell arguments or prevent filesystem TOCTOU.
        """
        if (
            not isinstance(actor, TaskActor)
            or not isinstance(capability, str)
            or capability == "bot_admin"
            or capability not in self.allowed_capabilities
        ):
            return False
        try:
            requested = Path(path)
            if not requested.is_absolute():
                return False
            resolved = requested.resolve()
            if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
                return False
            if actor.is_primary_user:
                return True
            now = time.time()
            return any(
                grant.revoked_at is None
                and (grant.expires_at is None or now < grant.expires_at)
                and (self.whole_task_grants and bool(grant.capabilities) or capability in grant.capabilities)
                and any(resolved.is_relative_to(root) for root in grant.roots)
                for grant in self.list(actor)
            )
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            return False
