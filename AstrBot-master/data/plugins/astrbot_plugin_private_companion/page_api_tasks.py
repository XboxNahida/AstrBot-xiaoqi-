"""Expose authenticated task management without exporting execution context."""

from __future__ import annotations

import inspect
import re
import sqlite3
from dataclasses import asdict
from functools import wraps

import quart
from quart import request

from .task_permissions import TaskActor
from .task_proactive_policy import RuleConfig
from .task_service import TaskScope


def _authenticated(handler):
    @wraps(handler)
    async def wrapped(self, *args, **kwargs):
        try:
            username = getattr(request, "username", "")
            if not username:
                username = getattr(getattr(quart, "g", None), "username", "")
        except RuntimeError:
            username = ""
        if not isinstance(username, str) or not username.strip():
            return self._error("请先登录管理页面", status_code=401)
        if getattr(self.plugin, "_persistent_task_runtime", None) is None:
            return self._error("任务管理暂未就绪", status_code=503)
        try:
            return await handler(self, username.strip(), *args, **kwargs)
        except PermissionError:
            return self._error("当前操作未获授权", status_code=403)
        except LookupError:
            return self._error("未找到所选记录", status_code=404)
        except (ValueError, TypeError):
            return self._error("请检查任务范围和填写内容", status_code=400)
        except Exception:
            return self._error("任务管理操作失败，请稍后重试", status_code=500)

    return wrapped


def _scope(values):
    scope = TaskScope(
        *(
            str(values.get(key) or "").strip()
            for key in ("bot_id", "session_key", "sender_id")
        )
    )
    if not all((scope.bot_id, scope.session_key, scope.sender_id)):
        raise ValueError("Missing task scope")
    if any(len(value) > 512 for value in vars(scope).values()):
        raise ValueError("Invalid task scope")
    return scope


def _public_evidence(value, depth=0, budget=None):
    if budget is None:
        budget = [8000]
    if depth > 4 or budget[0] <= 0:
        return "…"
    budget[0] -= 80
    if isinstance(value, dict):
        if value.get("type") in {"userMessage", "reasoning"}:
            return {"type": value["type"], "content": "[输入或内部推理已省略]"}
        return {
            str(key)[:80]: _public_evidence(item, depth + 1, budget)
            for key, item in list(value.items())[:30]
            if (str(key) == "tokenUsage" and isinstance(item, dict))
            or (
                type(item) is int
                and str(key)
                in {
                    "inputTokens",
                    "outputTokens",
                    "totalTokens",
                    "cachedInputTokens",
                    "cacheWriteInputTokens",
                    "reasoningOutputTokens",
                }
            )
            or not re.search(
                r"secret|token|password|authorization|cookie|api.?key|credential|system.?prompt|request|runtime|reply_context|^input$|baseinstructions|developerinstructions",
                str(key),
                re.I,
            )
        }
    if isinstance(value, (list, tuple)):
        return [_public_evidence(item, depth + 1, budget) for item in value[:20]]
    if isinstance(value, str):
        text = value[: min(2000, max(0, budget[0]))]
        budget[0] -= len(text)
        text = re.sub(
            r"(?i)(bearer\s+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*)\S+",
            r"\1[已隐藏]",
            text,
        )
        text = re.sub(r"data:image/[^\s\"'<>]+", "[图片已省略]", text)
        return re.sub(
            r"\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,})", "[已隐藏]", text
        )
    return (
        value
        if value is None or type(value) in (int, float, bool)
        else str(value)[:200]
    )


def _job_view(job, *, detail=False, service=None):
    value = {
        "task_id": job.task_id,
        **asdict(job.scope),
        "goal": _public_evidence(job.goal),
        "status": job.status,
        "revision": job.revision,
        "step_key": job.step_key,
        "attempts": job.attempts,
        "error_kind": _public_evidence(job.error_kind),
        "engine": job.metadata.get("engine", "native"),
        "codex": _public_evidence(
            {
                key: value
                for key, value in job.metadata.get("codex", {}).items()
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
        ),
        "model_id": (job.metadata.get("request") or {}).get("primary")
        if isinstance(job.metadata.get("request"), dict)
        else None,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at,
    }
    if service is not None:
        value["execution"] = _public_evidence(
            {
                key: item
                for key, item in service.execution_status(
                    job.status, job.metadata, job.lease_until
                ).items()
                if key != "worker_lease_valid"
            }
        )
    if detail:
        budget = [8000]
        value.update(
            steps=_public_evidence(job.steps, budget=budget),
            result=_public_evidence(job.result, budget=budget),
        )
    return value


class TasksPageApiMixin:
    """Use the host dashboard login for management and server actors for grants."""

    @_authenticated
    async def get_tasks(self, username):
        values = request.args
        scope = _scope(values)
        offset = int(values.get("offset", 0))
        if offset < 0 or offset > 1000000:
            raise ValueError("Invalid offset")
        service = self.plugin._persistent_task_runtime.service
        with sqlite3.connect(service.path) as db:
            key = (scope.bot_id, scope.session_key, scope.sender_id)
            total = db.execute(
                "SELECT COUNT(*) FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=?",
                key,
            ).fetchone()[0]
            ids = db.execute(
                "SELECT task_id FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? ORDER BY created_at DESC, task_id DESC LIMIT 20 OFFSET ?",
                (*key, offset),
            ).fetchall()
        tasks = [service.get(row[0], scope) for row in ids]
        return self._ok(
            {
                "tasks": [_job_view(job, service=service) for job in tasks if job],
                "total": total,
                "offset": offset,
                "limit": 20,
            }
        )

    @_authenticated
    async def get_task_detail(self, username):
        values = request.args
        job = self.plugin._persistent_task_runtime.service.get(
            str(values.get("task_id") or ""), _scope(values)
        )
        if job is None:
            raise LookupError("Task not found")
        runtime = self.plugin._persistent_task_runtime
        data = {"task": _job_view(job, detail=True, service=runtime.service)}
        if job.metadata.get("engine") == "codex":
            after, limit = int(values.get("after", 0)), int(values.get("limit", 20))
            if after < 0 or not 1 <= limit <= 20:
                raise ValueError("Invalid event page")
            codex = runtime.codex
            binding = codex.store.get(job.scope, task_id=job.task_id)
            data["codex_events"] = (
                codex.read(job.scope, task_id=job.task_id, after=after, limit=limit)[
                    "events"
                ]
                if binding
                else {"items": [], "next_after": None, "available": False}
            )
        return self._ok(data)

    @_authenticated
    async def control_task(self, username):
        values = await request.get_json(silent=True) or {}
        scope = _scope(values)
        runtime = self.plugin._persistent_task_runtime
        task_id, action = str(values.get("task_id") or ""), values.get("action")
        if action not in {"pause", "resume", "retry", "cancel", "revise", "reconcile"}:
            raise ValueError("Unknown task action")
        job = runtime.service.get(task_id, scope)
        if job is None:
            raise LookupError("Task not found")
        if job.metadata.get("engine") == "codex" and action != "reconcile":
            receipt = await runtime.control_codex_task(job, action)
            if not receipt.get("accepted"):
                if receipt.get("code") == "permission_denied":
                    raise PermissionError("Execution not authorized")
                return self._error(
                    "Codex控制未完成：" + str(receipt.get("code", "unknown_result")),
                    status_code=409,
                )
            return self._ok(
                {
                    "task": _job_view(
                        runtime.service.get(task_id, scope), service=runtime.service
                    ),
                    "principal": username,
                    "control": {
                        key: receipt.get(key) for key in ("code", "status", "thread_id")
                    },
                }
            )
        if job.metadata.get("engine") == "codex" and task_id in runtime.codex.active:
            return self._error(
                "Codex执行进程仍在活动，请先暂停或取消并等待真实回执", status_code=409
            )
        if action in {"resume", "retry"} and job.status == "unknown_result":
            return self._error("请先核实执行结果，不能直接重新执行", status_code=409)
        if action in {"resume", "retry"}:
            authorize = getattr(runtime, "authorize_job", None)
            authorized = authorize(job) if callable(authorize) else False
            if inspect.isawaitable(authorized):
                authorized = await authorized
            if authorized is not True:
                raise PermissionError("Execution not authorized")
        if action == "revise":
            changed = runtime.service.revise_goal(task_id, scope, values.get("goal"))
        elif action == "reconcile":
            if type(values.get("worker_stopped")) is not bool:
                raise ValueError("Explicit worker stop verification is required")
            changed = runtime.service.reconcile(
                task_id,
                scope,
                values.get("outcome"),
                values.get("evidence"),
                worker_stopped=values["worker_stopped"],
            )
        else:
            changed = getattr(runtime.service, action)(task_id, scope)
        if not changed:
            return self._error("当前任务状态不支持此操作", status_code=409)
        return self._ok(
            {
                "task": _job_view(
                    runtime.service.get(task_id, scope), service=runtime.service
                ),
                "principal": username,
            }
        )

    async def _tasks_dashboard_actor(self, values, username):
        bot, group = (
            str(values.get("bot_id") or "").strip(),
            str(values.get("group_id") or "").strip(),
        )
        resolver = getattr(
            self.plugin._persistent_task_runtime, "dashboard_actor", None
        )
        if not bot or not group or not callable(resolver):
            raise PermissionError("Missing trusted identity mapping")
        actor = resolver(bot, group, username)
        if inspect.isawaitable(actor):
            actor = await actor
        if (
            not isinstance(actor, TaskActor)
            or actor.bot_id != bot
            or actor.group_id != group
            or actor.is_primary_user is not True
        ):
            raise PermissionError("Primary account not verified")
        return actor

    @_authenticated
    async def get_task_grants(self, username):
        actor = await self._tasks_dashboard_actor(request.args, username)
        store = self.plugin._persistent_task_runtime.permissions
        grants = []
        for grant in store.list(actor):
            item = asdict(grant)
            item.update(
                capabilities=sorted(grant.capabilities),
                roots=[str(path) for path in grant.roots],
            )
            grants.append(item)
        return self._ok(
            {
                "grants": grants,
                "allowed_capabilities": sorted(store.allowed_capabilities),
                "allowed_roots": [str(path) for path in store.allowed_roots],
            }
        )

    @_authenticated
    async def grant_task_access(self, username):
        values = await request.get_json(silent=True) or {}
        actor = await self._tasks_dashboard_actor(values, username)
        if not isinstance(values.get("capabilities"), list) or not isinstance(
            values.get("roots"), list
        ):
            raise ValueError("Explicit capability and root lists required")
        grant = self.plugin._persistent_task_runtime.permissions.grant(
            actor,
            str(values.get("user_id") or "").strip(),
            capabilities=values["capabilities"],
            roots=values["roots"],
            expires_at=values.get("expires_at"),
        )
        result = asdict(grant)
        result.update(
            capabilities=sorted(grant.capabilities),
            roots=[str(path) for path in grant.roots],
        )
        return self._ok({"grant": result, "principal": username})

    @_authenticated
    async def revoke_task_access(self, username):
        values = await request.get_json(silent=True) or {}
        actor = await self._tasks_dashboard_actor(values, username)
        if not self.plugin._persistent_task_runtime.permissions.revoke(
            actor, str(values.get("grant_id") or "")
        ):
            raise LookupError("Grant not found")
        return self._ok({"revoked": True, "principal": username})

    async def _tasks_cron_rule(self, values):
        scope, rule_id = _scope(values), str(values.get("rule_id") or "").strip()
        manager = getattr(self.plugin._persistent_task_runtime, "cron_manager", None)
        if not rule_id or manager is None:
            raise LookupError("Cron unavailable")
        job = await manager.db.get_cron_job(rule_id)
        if job is None:
            raise LookupError("Cron not found")
        payload = job.payload if isinstance(job.payload, dict) else {}
        if (
            payload.get("origin") != "tool"
            or payload.get("session") != scope.session_key
            or str(payload.get("sender_id") or "") != scope.sender_id
            or scope.session_key.split(":", 1)[0] != scope.bot_id
            or (
                payload.get("bot_id") is not None
                and str(payload["bot_id"]) != scope.bot_id
            )
        ):
            raise PermissionError("Cron identity mismatch")
        return scope, rule_id, manager, job

    @_authenticated
    async def get_task_proactive_policy(self, username):
        scope, rule_id, _, job = await self._tasks_cron_rule(request.args)
        raw = self.plugin._persistent_task_runtime.policy.get(scope, rule_id)
        policy = (
            None
            if raw is None
            else {
                key: raw[key]
                for key in ("config", "sent_count", "last_sent_at", "state")
            }
        )
        return self._ok(
            {
                "rule_id": rule_id,
                "cron_expression": job.cron_expression,
                "run_once": job.run_once,
                "task_kind": job.payload.get("source")
                if job.payload.get("source") in {"reminder", "monitor"}
                else None,
                "policy": policy,
                "defaults": asdict(RuleConfig()),
            }
        )

    @_authenticated
    async def update_task_proactive_policy(self, username):
        values = await request.get_json(silent=True) or {}
        scope, rule_id, manager, job = await self._tasks_cron_rule(values)
        config = values.get("config")
        if not isinstance(config, dict) or set(config) - set(
            RuleConfig.__dataclass_fields__
        ):
            raise ValueError("Invalid policy fields")
        store = self.plugin._persistent_task_runtime.policy
        previous = store.get(scope, rule_id)
        combined = {
            **(previous["config"] if previous else asdict(RuleConfig())),
            **config,
        }
        replacement = RuleConfig(**combined)
        task_kind = values.get("task_kind", job.payload.get("source"))
        if task_kind not in {"reminder", "monitor"}:
            raise ValueError("Explicit reminder or monitor type is required")
        updates = {}
        if task_kind != job.payload.get("source"):
            updates["payload"] = {**job.payload, "source": task_kind}
        expression = None
        if "cron_expression" in values:
            from apscheduler.triggers.cron import CronTrigger

            expression = values["cron_expression"]
            if job.run_once or not isinstance(expression, str):
                raise ValueError("Invalid recurring expression")
            CronTrigger.from_crontab(expression.strip())
            updates["cron_expression"] = expression.strip()
        # Validate before touching the existing schedule; rollback policy on failure.
        store.save(scope, rule_id, replacement)
        if updates:
            previous_cron = {key: getattr(job, key) for key in updates}
            try:
                if await manager.update_job(rule_id, **updates) is None:
                    raise LookupError("Cron disappeared")
            except Exception:
                if previous:
                    store.save(scope, rule_id, RuleConfig(**previous["config"]))
                else:
                    with store._transaction() as db:
                        db.execute(
                            "DELETE FROM pt_proactive_rules WHERE bot_id=? AND session_key=? AND sender_id=? AND existing_rule_id=?",
                            (*vars(scope).values(), rule_id),
                        )
                await manager.update_job(rule_id, **previous_cron)
                raise
        return self._ok(
            {"rule_id": rule_id, "config": asdict(replacement), "principal": username}
        )
