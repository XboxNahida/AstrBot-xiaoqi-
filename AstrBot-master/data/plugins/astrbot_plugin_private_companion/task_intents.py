"""Adapt original user events without treating model fields as identity or grants."""

from __future__ import annotations

import inspect
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .task_service import TaskScope, TaskService
except ImportError:
    from task_service import TaskScope, TaskService


@dataclass(frozen=True)
class TaskIntent:
    """Contain bounded model suggestions, never account or authorization fields."""

    action: str
    goal: str = ""
    fragment_key: str = "main"
    task_id: str = ""
    related_task_id: str = ""
    relation: str = "follow_up"


@dataclass(frozen=True)
class TaskIntentRequest:
    """Supply host authorization with trusted scope and untrusted intent.

    Args:
        event: Original platform event retained by trusted host code.
        scope: Identity copied from original event methods, never model arguments.
        group_id: Original group, empty in private conversations.
        origin_message_id: Original platform message identifier.
        original_text: Original user text for checking the model's interpretation.
        intent: Parsed suggestions; action and goal do not themselves grant rights.
        workdir: Host-configured directory, or the stored task's directory on retry.
        source: Trusted caller role, either hub or reply.
    """

    event: Any
    scope: TaskScope
    group_id: str
    origin_message_id: str
    original_text: str
    intent: TaskIntent
    workdir: Path
    source: str


class TaskIntentAdapter:
    """Accept one durable submission per source fragment, shared by both models.

    Args:
        service: Dedicated persistent task service; never the runtime database.
        authorize: Trusted sync/async callback accepting TaskIntentRequest. It must
            verify live program permissions and return exactly True to authorize.
            Hub chat/text_only classifications are not permission decisions.
    """

    def __init__(self, service: TaskService, authorize: Callable, *, metadata_factory: Callable | None = None):
        self.service = service
        self.authorize = authorize
        self.metadata_factory = metadata_factory
        with closing(sqlite3.connect(service.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pt_intent_fragments ("
                "bot_id TEXT NOT NULL,session_key TEXT NOT NULL,sender_id TEXT NOT NULL,"
                "origin_message_id TEXT NOT NULL,fragment_key TEXT NOT NULL,"
                "PRIMARY KEY(bot_id,session_key,sender_id,origin_message_id,fragment_key))"
            )

    async def handle(
        self,
        event: Any,
        payload: Mapping[str, Any],
        *,
        trusted_user_event: bool,
        normally_awakened: bool,
        workdir: str | Path,
        source: str = "reply",
        model_policy: Mapping | None = None,
    ) -> dict[str, Any]:
        """Validate one request from an already awakened original user event.

        Args:
            event: Original inbound platform event, not a reconstructed tool event.
            payload: Untrusted model fields: action, goal, fragment_key, task_id.
            trusted_user_event: Required host-only proof of an original human event.
                The host must reject cron, synthetic replies, notifications, bot
                authors, recalled events and tool/web content before setting True.
                An event.extra claim, model argument or quoted text is not proof.
            normally_awakened: Required host-only normal wake/participation result.
                Never set True solely because this adapter/tool was requested.
            workdir: Directory selected by the host, never from model payload.
            source: Host caller role; both hub and reply must use this same adapter
                and fragment key. Omitted fragment_key always means main.

        Returns:
            A bounded receipt with accepted/code, created and task_id/status when
            available. Accepted means persisted or controlled, never executed.
        """
        receipt = {"accepted": False, "created": False}
        if (
            trusted_user_event is not True
            or normally_awakened is not True
            or source not in {"hub", "reply"}
        ):
            return {**receipt, "code": "untrusted_event"}
        try:
            scope = TaskScope(
                event.get_platform_id(), event.unified_msg_origin, event.get_sender_id()
            )
            message_id = event.message_obj.message_id
            group_id = event.get_group_id()
            original_text = event.message_str
            if (
                any(
                    not isinstance(value, str) or not value.strip() or len(value) > 1024
                    for value in (
                        scope.bot_id,
                        scope.session_key,
                        scope.sender_id,
                        message_id,
                    )
                )
                or not isinstance(group_id, str)
                or not isinstance(original_text, str)
            ):
                raise ValueError("Invalid original event identity")
        except (AttributeError, TypeError, ValueError, RuntimeError):
            return {**receipt, "code": "invalid_event"}
        if not isinstance(payload, Mapping):
            return {**receipt, "code": "invalid_intent"}
        action = payload.get("action", "submit")
        goal = payload.get("goal", "")
        fragment = payload.get("fragment_key", "main")
        task_id = payload.get("task_id", "")
        related_id = payload.get("related_task_id", "")
        relation = payload.get("relation", "follow_up")
        if (
            not isinstance(action, str)
            or action not in {"submit", "query", "cancel", "pause", "retry", "resume", "revise"}
            or not isinstance(goal, str)
            or len(goal) > 4000
            or not isinstance(fragment, str)
            or not fragment.strip()
            or len(fragment) > 64
            or not isinstance(task_id, str)
            or len(task_id) > 128
            or not isinstance(related_id, str)
            or len(related_id) > 128
            or not isinstance(relation, str)
            or relation not in {"follow_up", "revision", "retry", "related"}
            or (bool(related_id) and action != "submit")
            or (action in {"submit", "revise"} and not goal.strip())
            or (action not in {"submit", "query"} and not task_id)
        ):
            return {**receipt, "code": "invalid_intent"}
        intent = TaskIntent(action, goal.strip(), fragment, task_id, related_id, relation)
        parent = self.service.get(related_id, scope) if related_id else None
        if related_id and (parent is None or parent.metadata.get("history_deleted")):
            return {**receipt, "code": "not_found"}
        task = self.service.get(task_id, scope) if task_id else None
        if action != "submit" and task_id and task is None:
            return {**receipt, "code": "not_found"}
        if action == "query":
            tasks = [task] if task else self.service.list(scope, limit=20, latest=True)
            snapshots = [
                {
                    "task_id": item.task_id,
                    "status": item.status,
                    "goal": item.goal[:1000],
                    "revision": item.revision,
                    "error_kind": item.error_kind,
                    "model_policy": {key: item.metadata.get("request", {}).get(key)
                                     for key in ("primary", "request_timeout_seconds", "request_max_attempts")},
                }
                for item in tasks
            ]
            return {
                **receipt,
                "accepted": True,
                "code": "queried",
                "tasks": snapshots,
                **({"task_id": task.task_id, "status": task.status} if task else {}),
            }
        if action in {"submit", "retry", "resume", "revise"}:
            try:
                directory = Path(task.workdir if task else workdir)
                if not directory.is_absolute():
                    return {**receipt, "code": "invalid_workdir"}
                request = TaskIntentRequest(
                    event,
                    scope,
                    group_id,
                    message_id,
                    original_text,
                    intent,
                    directory,
                    source,
                )
                allowed = self.authorize(request)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
                if allowed is not True:
                    return {**receipt, "code": "permission_denied"}
            except Exception:
                return {**receipt, "code": "permission_denied"}
        if action == "submit":
            metadata = {"intent_source": source, "original_text": original_text[:16000]}
            if self.metadata_factory is not None:
                supplied = self.metadata_factory(request)
                if inspect.isawaitable(supplied):
                    supplied = await supplied
                if not isinstance(supplied, Mapping):
                    raise ValueError("Host metadata must be a mapping")
                metadata.update(supplied)
            if parent is not None:
                metadata.update(
                    task_group_id=parent.metadata.get("task_group_id", parent.task_id),
                    parent_task_id=parent.task_id,
                    task_relation=relation,
                )
            # Reserve fragment slots transactionally across adapters and restarts.
            # Reservation before submit also prevents a crash from granting a ninth
            # source fragment. The retry finds the same durable service identity.
            identity = (scope.bot_id, scope.session_key, scope.sender_id, message_id)
            with closing(sqlite3.connect(self.service.path, timeout=10)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                reserved = db.execute(
                    "SELECT 1 FROM pt_intent_fragments WHERE bot_id=? AND session_key=? "
                    "AND sender_id=? AND origin_message_id=? AND fragment_key=?",
                    (*identity, fragment),
                ).fetchone()
                existing_task = db.execute(
                    "SELECT 1 FROM pt_tasks WHERE bot_id=? AND session_key=? "
                    "AND sender_id=? AND origin_message_id=? AND fragment_key=?",
                    (*identity, fragment),
                ).fetchone()
                existing = reserved or existing_task
                if not reserved:
                    count = db.execute(
                        "SELECT COUNT(*) FROM (SELECT fragment_key FROM pt_intent_fragments "
                        "WHERE bot_id=? AND session_key=? AND sender_id=? AND origin_message_id=? "
                        "UNION SELECT fragment_key FROM pt_tasks WHERE bot_id=? "
                        "AND session_key=? AND sender_id=? AND origin_message_id=?)",
                        (*identity, *identity),
                    ).fetchone()[0]
                    if not existing_task and count >= 8:
                        return {**receipt, "code": "fragment_limit"}
                    db.execute(
                        "INSERT INTO pt_intent_fragments VALUES(?,?,?,?,?)",
                        (*identity, fragment),
                    )
            task = self.service.submit(
                scope,
                message_id,
                fragment,
                intent.goal,
                directory,
                metadata=metadata,
            )
            return {
                "accepted": True,
                "created": not bool(existing),
                "code": "already_exists" if existing else "accepted",
                "task_id": task.task_id,
                "status": task.status,
            }
        if action == "revise":
            supplied = self.metadata_factory(request) if self.metadata_factory else None
            if inspect.isawaitable(supplied):
                supplied = await supplied
            changed = self.service.revise_goal(task_id, scope, intent.goal, input_metadata=supplied)
        else:
            if action in {"retry", "resume"} and model_policy is not None:
                changed = getattr(self.service, action)(task_id, scope, request_patch=model_policy)
            else:
                changed = getattr(self.service, action)(task_id, scope)
        current = self.service.get(task_id, scope)
        return {
            **receipt,
            "accepted": changed,
            "code": "updated" if changed else "invalid_transition",
            "task_id": task_id,
            "status": current.status if current else None,
        }
