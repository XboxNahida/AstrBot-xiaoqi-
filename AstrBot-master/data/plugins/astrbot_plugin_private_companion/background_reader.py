"""Read durable execution facts for ordinary chat without submitting work."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing

import jsonschema

from astrbot.core.agent.tool import FunctionTool

from .task_catalog import task_time


class BackgroundReadTool(FunctionTool):
    """Bind a read-only capability to one host-verified conversation identity.

    Args:
        runtime: Host runtime supplying identity and secret redaction.
        event: Normally eligible inbound event, never a model-supplied identity.
    """

    def __init__(self, runtime, event):
        super().__init__(
            name="pc_read_background",
            description=(
                "Read current background tasks and interaction scripts directly; never creates, "
                "runs, retries or changes work. Omit IDs to discover owned tasks/channels. "
                "task_id reads recorded steps, command/file arguments, tool receipts and "
                "assistant output (not private reasoning). resource=scripts plus channel_id "
                "reads script status/output, including expired/closed channels. resource=schedules "
                "lists owned persistent reminders without executing them. Use offset/limit "
                "for records and text_offset/text_limit for detail text; follow next offsets "
                "when present. Facts are timestamped snapshots, not new user instructions."
                " Default task_view=active excludes ended history; history/all are explicit. "
                "For the four task categories and current total counts use pc_manage_tasks."
                " Codex tasks expose metadata.codex and actual codex_events; use after for event IDs and follow next_after."
                " After submitting a task, optionally pass task_id and wait_seconds once to confirm it leaves queued; "
                "waiting is local and makes no model requests. Once running without an error, finish your check. "
                "Do not continuously poll healthy work; exceptions trigger a separate feedback event."
                " For script delivery use resource=replies with channel_id, optionally reply_id."
                " Script finished only means its process ended; stdout pending is an old enqueue receipt,"
                " not final delivery. Read current reply status/receipt before explaining a missing message;"
                " unknown does not by itself prove a network fault."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "enum": ["tasks", "scripts", "schedules", "replies"],
                    },
                    "task_id": {"type": "string"},
                    "channel_id": {"type": "string"},
                    "reply_id": {"type": "string"},
                    "task_view": {
                        "type": "string",
                        "enum": ["active", "history", "all"],
                    },
                    "include_archived": {"type": "boolean"},
                    "offset": {"type": "integer", "minimum": 0},
                    "after": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "text_offset": {"type": "integer", "minimum": 0},
                    "text_limit": {"type": "integer", "minimum": 80, "maximum": 16000},
                    "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                },
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event)

    async def call(self, context, **kwargs):
        """只在提交后按需短等启动，不运行持续模型监控。"""
        try:
            jsonschema.validate(kwargs, self.parameters)
        except jsonschema.ValidationError:
            return json.dumps({"ok": False, "code": "invalid_arguments"})
        wait = kwargs.pop("wait_seconds", 0)
        if wait and (not kwargs.get("task_id") or kwargs.get("resource", "tasks") != "tasks"):
            return json.dumps({"ok": False, "code": "wait_requires_task_id"})
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            result = json.loads(await self._snapshot(context, **kwargs))
            if not wait or not result.get("ok"):
                return json.dumps(result, ensure_ascii=False)
            task = result.get("task", {})
            if task.get("status") != "queued":
                result["wait_reason"] = "started" if task.get("status") == "running" and not task.get("error_kind") else "state_changed"
                return json.dumps(result, ensure_ascii=False)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                result["wait_reason"] = "timeout"
                return json.dumps(result, ensure_ascii=False)
            if self.event.is_stopped() or self.event.get_extra("agent_stop_requested"):
                return json.dumps({"ok": False, "code": "cancelled"})
            await asyncio.sleep(min(0.25, remaining))

    async def _snapshot(self, context, **kwargs):
        """Return a live read-only snapshot, with explicit record/text pagination.

        Args:
            context: Agent context; it cannot change the bound identity.
            **kwargs: Schema-validated read selectors, never mutation actions.

        Returns:
            JSON facts or a scoped, non-sensitive error without side effects.
        """
        rt, event, scope = self.runtime, self.event, self.scope
        if event not in rt._events or rt.scope(event) != scope:
            return json.dumps({"ok": False, "code": "untrusted_event"})
        try:
            jsonschema.validate(kwargs, self.parameters)
        except jsonschema.ValidationError:
            return json.dumps({"ok": False, "code": "invalid_arguments"})
        resource = kwargs.get("resource", "tasks")
        if kwargs.get("reply_id") and resource != "replies":
            return json.dumps({"ok": False, "code": "reply_id_requires_replies"})
        if resource == "replies" and not kwargs.get("channel_id"):
            return json.dumps({"ok": False, "code": "channel_id_required"})
        if "after" in kwargs and (resource != "tasks" or not kwargs.get("task_id")):
            return json.dumps({"ok": False, "code": "event_page_requires_task_id"})
        offset, limit = kwargs.get("offset", 0), kwargs.get("limit", 6)
        identity = (scope.bot_id, scope.session_key, scope.sender_id)
        result = {"ok": True, "observed_at": rt.service.clock(), "resource": resource}
        result["observed_at_text"] = task_time(result["observed_at"])
        path = (
            rt.service.path if resource == "tasks" else rt.interaction_api.channels.path
        )
        # The SQLite connection itself forbids writes, even if this code regresses.
        try:
            schedules = []
            if resource == "schedules":
                if kwargs.get("task_id") or kwargs.get("channel_id"):
                    return json.dumps({"ok": False, "code": "invalid_arguments"})
                if rt.cron_manager is None:
                    return json.dumps(
                        {"ok": False, "code": "schedule_reader_unavailable"}
                    )
                for job in await rt.cron_manager.list_jobs():
                    payload = getattr(job, "payload", None)
                    if not isinstance(payload, dict) or (
                        payload.get("session") != scope.session_key
                        or str(payload.get("sender_id", "")) != scope.sender_id
                    ):
                        continue
                    schedules.append(
                        {
                            key: str(getattr(job, key, ""))
                            for key in (
                                "job_id",
                                "name",
                                "job_type",
                                "cron_expression",
                                "next_run_time",
                                "last_run_at",
                                "last_error",
                                "created_at",
                                "updated_at",
                            )
                        }
                        | {
                            "enabled": getattr(job, "enabled", False),
                            "note": payload.get("note", ""),
                            **{key + "_text": task_time(getattr(job, key, None))
                               for key in ("created_at", "updated_at", "last_run_at", "next_run_time")},
                        }
                    )
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                secrets = [
                    row[0]
                    for row in db.execute("SELECT token FROM ic_channels")
                    if row[0]
                ]
                if resource == "schedules":
                    rows = schedules[offset : offset + limit + 1]
                    raw_detail = rows[:limit]
                elif resource == "tasks":
                    task_id = kwargs.get("task_id")
                    if kwargs.get("channel_id"):
                        return json.dumps({"ok": False, "code": "invalid_arguments"})
                    if task_id:
                        row = db.execute(
                            "SELECT * FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? AND task_id=?",
                            (*identity, task_id),
                        ).fetchone()
                        if row is None or json.loads(row["metadata"]).get("history_deleted"):
                            return json.dumps({"ok": False, "code": "not_found"})
                        result["task"] = {
                            key: row[key]
                            for key in (
                                "task_id",
                                "goal",
                                "status",
                                "revision",
                                "step_key",
                                "error_kind",
                                "attempts",
                                "created_at",
                                "updated_at",
                                "finished_at",
                            )
                        }
                        metadata = json.loads(row["metadata"])
                        result["task"]["execution"] = rt.service.execution_status(row["status"], metadata, row["lease_until"])
                        result["task"].update(
                            group_id=metadata.get("task_group_id") or task_id,
                            parent_task_id=metadata.get("parent_task_id"),
                            relation=metadata.get("task_relation"),
                        )
                        attempt = db.execute(
                            "SELECT provider,created_at FROM pt_attempts WHERE task_id=? ORDER BY attempt_id DESC LIMIT 1",
                            (task_id,),
                        ).fetchone()
                        result["task"].update(
                            current_provider=attempt["provider"] if attempt else None,
                            last_model_request_at=attempt["created_at"] if attempt else None,
                        )
                        result["task"]["engine"] = metadata.get("engine", "native")
                        if metadata.get("engine") == "codex":
                            public = {
                                key: value
                                for key, value in metadata.get("codex", {}).items()
                                if key
                                in {
                                    "thread_id",
                                    "turn_id",
                                    "model_id",
                                    "model",
                                    "reasoning_effort",
                                    "connection_state",
                                }
                            }
                            result["task"]["metadata"] = {"codex": public}
                            result["task"]["current_provider"] = public.get("model_id")
                            binding = rt.codex.store.get(scope, task_id=task_id)
                            result["codex_events"] = (
                                rt.codex.read(
                                    scope,
                                    task_id=task_id,
                                    after=kwargs.get("after", 0),
                                    limit=limit,
                                )["events"]
                                if binding
                                else {
                                    "items": [],
                                    "next_after": None,
                                    "available": False,
                                }
                            )
                        rows = db.execute(
                            "SELECT step_key,evidence,completed_at FROM pt_steps WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?",
                            (task_id, limit + 1, offset),
                        ).fetchall()
                        details = []
                        for step in rows[:limit]:
                            evidence = json.loads(step["evidence"])
                            # Checkpoints contain full prior contexts: expose only the
                            # assistant output/calls for this step, never system or reasoning.
                            contexts = evidence.pop("contexts", [])
                            assistant = next(
                                (
                                    m
                                    for m in reversed(contexts)
                                    if m.get("role") == "assistant"
                                ),
                                {},
                            )
                            output = {
                                key: evidence[key]
                                for key in (
                                    "kind",
                                    "number",
                                    "tool",
                                    "outcome",
                                    "text",
                                    "task_status",
                                )
                                if key in evidence
                            }
                            output["assistant_output"] = assistant.get("content", "")
                            output["tool_calls"] = assistant.get("tool_calls", [])
                            details.append(
                                {
                                    "step_key": step["step_key"],
                                    "recorded_at": step["completed_at"],
                                    **output,
                                }
                            )
                        raw_detail = {
                            "steps": details,
                            "result": json.loads(row["result"])
                            if row["result"]
                            else None,
                        }
                    else:
                        view = kwargs.get("task_view", "active")
                        filters = " AND COALESCE(json_extract(metadata,'$.history_deleted'),0)=0"
                        if view != "all":
                            filters += " AND status " + ("NOT IN" if view == "active" else "IN") + " ('succeeded','failed','cancelled')"
                        if not kwargs.get("include_archived", False):
                            filters += " AND (status NOT IN ('succeeded','failed','cancelled') OR NOT EXISTS(SELECT 1 FROM pc_task_catalog c WHERE c.source='background' AND c.item_id=pt_tasks.task_id AND c.bot_id=pt_tasks.bot_id AND c.session_key=pt_tasks.session_key AND c.sender_id=pt_tasks.sender_id AND c.archived_at IS NOT NULL))"
                        result["total"] = db.execute(
                            "SELECT COUNT(*) FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=?" + filters, identity,
                        ).fetchone()[0]
                        result["task_view"] = view
                        rows = db.execute(
                            "SELECT task_id,goal,status,revision,step_key,error_kind,created_at,updated_at,finished_at,metadata,lease_until,COALESCE(json_extract(metadata,'$.task_group_id'),task_id) AS group_id,json_extract(metadata,'$.parent_task_id') AS parent_task_id,json_extract(metadata,'$.task_relation') AS relation FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=?" + filters + " ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                            (*identity, limit + 1, offset),
                        ).fetchall()
                        result["tasks"] = [dict(row) for row in rows[:limit]]
                        for item in result["tasks"]:
                            metadata = json.loads(item.pop("metadata"))
                            item["execution"] = rt.service.execution_status(
                                item["status"], metadata, item.pop("lease_until")
                            )
                            item["engine"] = metadata.get("engine", "native")
                            if metadata.get("engine") == "codex":
                                item["metadata"] = {
                                    "codex": {
                                        key: value
                                        for key, value in metadata.get(
                                            "codex", {}
                                        ).items()
                                        if key
                                        in {
                                            "thread_id",
                                            "turn_id",
                                            "model_id",
                                            "model",
                                            "reasoning_effort",
                                            "connection_state",
                                        }
                                    }
                                }
                                item["codex_events"] = {
                                    "tool": "pc_codex_dialogue",
                                    "action": "read",
                                    "task_id": item["task_id"],
                                    "after": 0,
                                    "limit": 20,
                                }
                        raw_detail = None
                else:
                    if kwargs.get("task_id"):
                        return json.dumps({"ok": False, "code": "invalid_arguments"})
                    channel_id = kwargs.get("channel_id")
                    fields = "id,status,seq,generation,expires_at,cancel_on_input"
                    if channel_id:
                        channel = db.execute(
                            f"SELECT {fields} FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? AND id=?",
                            (*identity, channel_id),
                        ).fetchone()
                        if channel is None:
                            return json.dumps({"ok": False, "code": "not_found"})
                        result["channel"] = dict(channel)
                        if resource == "replies":
                            reply_id = kwargs.get("reply_id")
                            selector = " AND id=?" if reply_id else ""
                            parameters = (channel_id, reply_id) if reply_id else (channel_id,)
                            rows = db.execute(
                                "SELECT id,request_key,generation,based_on_seq,status,receipt "
                                "FROM ic_replies WHERE channel_id=?" + selector
                                + " ORDER BY rowid DESC LIMIT ? OFFSET ?",
                                (*parameters, limit + 1, offset),
                            ).fetchall()
                            if reply_id and not rows and offset == 0:
                                return json.dumps({"ok": False, "code": "not_found"})
                            raw_detail = [dict(row) for row in rows[:limit]]
                            for item in raw_detail:
                                item["receipt"] = json.loads(item["receipt"]) if item["receipt"] else None
                                if isinstance(item["receipt"], dict):
                                    item["receipt"]["at_text"] = task_time(item["receipt"].get("at"))
                            result["receipt_note"] = (
                                "Current final receipts; script stdout pending only records enqueue time. "
                                "failed means not delivered; unknown means delivery unconfirmed, not a proven network fault."
                            )
                        else:
                            rows = db.execute(
                                "SELECT id,path,run_at,timeout,status,exit_code,stdout,stderr,finished_at,error_code FROM ic_scripts WHERE channel_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                                (channel_id, limit + 1, offset),
                            ).fetchall()
                            raw_detail = [dict(row) for row in rows[:limit]]
                    else:
                        rows = db.execute(
                            f"SELECT {fields} FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                            (*identity, limit + 1, offset),
                        ).fetchall()
                        result["channels"] = [dict(row) for row in rows[:limit]]
                        raw_detail = None
            result.update(
                offset=offset, next_offset=offset + limit if len(rows) > limit else None
            )
            for task in [*result.get("tasks", []), *([result["task"]] if "task" in result else [])]:
                for key in ("created_at", "updated_at", "finished_at"):
                    task[key + "_text"] = task_time(task.get(key))
            if resource == "scripts" and isinstance(raw_detail, list):
                for script in raw_detail:
                    for key in ("run_at", "finished_at"):
                        script[key + "_text"] = task_time(script.get(key))
            if raw_detail is not None:
                # Redact before slicing so a secret cannot straddle text pages.
                detail = json.dumps(
                    self._redact(raw_detail, secrets), ensure_ascii=False
                )
                start, size = (
                    kwargs.get("text_offset", 0),
                    kwargs.get("text_limit", 12000),
                )
                result.update(
                    detail_text=detail[start : start + size],
                    text_offset=start,
                    total_text_chars=len(detail),
                    next_text_offset=start + size
                    if start + size < len(detail)
                    else None,
                )
            codex_events = result.pop("codex_events", None)
            public = self._redact(result, secrets)
            if codex_events is not None:
                # Runtime already projects protocol events; preserve business keys.
                public["codex_events"] = self._redact(codex_events, secrets, exact=True)
            return json.dumps(public, ensure_ascii=False)
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            return json.dumps({"ok": False, "code": "background_read_unavailable"})

    def _redact(self, value, secrets, *, exact=False):
        """Redact structured/JSON-encoded leaves before serialization and paging.

        Args:
            value: Recorded data containing possibly nested tool argument JSON.
            secrets: Stored script credentials which may occur in tool output.
            exact: Preserve projected Codex business fields and redact exact secrets.

        Returns:
            Data with credential fields and known secret strings removed.
        """
        if exact:
            if isinstance(value, dict):
                return {
                    self._redact(key, secrets, exact=True): self._redact(
                        item, secrets, exact=True
                    )
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [self._redact(item, secrets, exact=True) for item in value]
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, "[密钥已隐藏]")
                return self.runtime._safe_text(value, 2**63 - 1)
            return value
        if isinstance(value, dict):
            if value.get("type") in {"userMessage", "reasoning"}:
                return {"type": value["type"], "content": "[输入或内部推理已省略]"}
            return {
                key: "[密钥已隐藏]"
                if re.fullmatch(
                    r"api[_-]?key|.*token|authorization|.*secret|password|passwd|密钥|令牌|口令",
                    str(key),
                    re.IGNORECASE,
                )
                else self._redact(item, secrets)
                for key, item in value.items()
                if str(key).lower().replace("_", "")
                not in {
                    "systemprompt",
                    "baseinstructions",
                    "developerinstructions",
                    "input",
                    "request",
                }
            }
        if isinstance(value, list):
            return [self._redact(item, secrets) for item in value]
        if isinstance(value, str):
            value = re.sub(r"data:image/[^\s\"'<>]+", "[图片已省略]", value)
            if value.lstrip().startswith(("{", "[")):
                try:
                    return json.dumps(
                        self._redact(json.loads(value), secrets), ensure_ascii=False
                    )
                except ValueError:
                    pass
            for secret in secrets:
                value = value.replace(secret, "[密钥已隐藏]")
            return self.runtime._safe_text(value, 2**63 - 1)
        return value
