"""Classify live schedules and model execution without creating another queue."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

TASK_DEFINITIONS = {
    "long_term": "长期任务：持续或周期执行的定时查询、提醒等未来安排；不是耗时较久的GPT执行。",
    "short_term": "短期任务：一次性或阶段性的定时查询、提醒等未来安排；不按固定分钟数区分，可按用户要求调整分类。",
    "background": "后台任务：用户提出要求后由GPT等任务模型执行的工作；等待输入、暂停、重试、结果未知仍需关注。",
    "history": "历史任务：已完成、已取消或已结束失败的执行记录，不计入待办；可归档后按用户要求删除历史内容。",
}
TASK_GUIDANCE = (
    "【任务分类定义与管理】\n"
    + "\n".join(TASK_DEFINITIONS.values())
    + "\n创建定时安排的后台工作完成，不等于定时安排已经执行或结束；周期任务的某一轮完成也不等于整个周期任务结束。"
    "pc_manage_tasks直接管理已有任务、分类和历史，不要为查询、分类或归档再创建GPT后台任务。"
    "先查询真实清单及counts；分页不完整或某来源不可用时，不得断言没有其他任务。"
    "归档不等于取消，删除只清理已归档历史，不删除工作文件。只有操作回执成功才可说明操作已完成。"
    "普通新工作通过pc_submit_task_intent交后台；定时安排可通过pc_manage_tasks的schedule操作创建或修改。"
)
BACKGROUND_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
SCRIPT_TERMINAL = frozenset(
    {"finished", "failed", "cancelled", "interrupted", "expired", "permission_denied"}
)


class TaskCatalog:
    """Keep presentation metadata separate from each real execution service.

    Args:
        runtime: Host task runtime, never a model-supplied database path.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        with runtime.service._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS pc_task_catalog (
                bot_id TEXT NOT NULL, session_key TEXT NOT NULL, sender_id TEXT NOT NULL,
                source TEXT NOT NULL, item_id TEXT NOT NULL, category TEXT,
                archived_at REAL, deleted_at REAL, revision INTEGER NOT NULL DEFAULT 1,
                saved TEXT,
                PRIMARY KEY(bot_id,session_key,sender_id,source,item_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS pc_task_management_ops (
                bot_id TEXT NOT NULL, session_key TEXT NOT NULL, sender_id TEXT NOT NULL,
                message_id TEXT NOT NULL, operation_key TEXT NOT NULL, receipt TEXT,
                PRIMARY KEY(bot_id,session_key,sender_id,message_id,operation_key))""")

    async def _records(self, scope):
        """Read all owned facts so pagination cannot hide old active work."""
        rt = self.runtime
        identity = (scope.bot_id, scope.session_key, scope.sender_id)
        items, secrets, unavailable = [], [], []
        if (
            not all(isinstance(v, str) and v for v in identity)
            or scope.session_key.split(":", 1)[0] != scope.bot_id
        ):
            raise ValueError("invalid_scope")
        with closing(
            sqlite3.connect(rt.service.path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            labels = {
                (row["source"], row["item_id"]): dict(row)
                for row in db.execute(
                    "SELECT * FROM pc_task_catalog WHERE bot_id=? AND session_key=? AND sender_id=?",
                    identity,
                )
            }
            for row in db.execute(
                "SELECT task_id,goal,status,error_kind,revision,created_at,next_run,metadata FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? ORDER BY created_at DESC,rowid DESC",
                identity,
            ):
                metadata = json.loads(row["metadata"])
                if metadata.get("history_deleted"):
                    continue
                active = row["status"] not in BACKGROUND_TERMINAL
                items.append(
                    {
                        "source": "background",
                        "item_id": row["task_id"],
                        "title": row["goal"],
                        "status": row["status"],
                        "error_kind": row["error_kind"],
                        "revision": row["revision"],
                        "created_at": row["created_at"],
                        "next_run": row["next_run"],
                        "active": active,
                        "category": "background" if active else "history",
                        "needs_attention": row["status"]
                        in {"waiting_user", "paused", "unknown_result"},
                    }
                )
            for row in db.execute(
                "SELECT s.id,s.path,s.status,s.run_at,s.error_code,c.id AS channel_id,c.metadata,c.status AS channel_status,c.expires_at FROM ic_scripts s JOIN ic_channels c ON s.channel_id=c.id WHERE c.bot_id=? AND c.session_key=? AND c.sender_id=? ORDER BY s.rowid DESC",
                identity,
            ):
                active = row["status"] not in SCRIPT_TERMINAL
                metadata = json.loads(row["metadata"])
                items.append(
                    {
                        "source": "script",
                        "item_id": row["id"],
                        "title": row["path"],
                        "status": row["status"],
                        "next_run": row["run_at"],
                        "active": active,
                        "category": "short_term" if active else "history",
                        "channel_id": row["channel_id"],
                        "source_task_id": metadata.get("source_task_id"),
                        "error_kind": row["error_code"],
                        "needs_attention": active
                        and (
                            row["channel_status"] != "open"
                            or row["expires_at"] <= rt.service.clock()
                        ),
                    }
                )
            secrets = [
                row[0] for row in db.execute("SELECT token FROM ic_channels") if row[0]
            ]
        if rt.cron_manager is None:
            unavailable.append("schedule")
        else:
            try:
                jobs = await rt.cron_manager.list_jobs()
            except Exception:
                jobs = []
                unavailable.append("schedule")
            for job in jobs:
                payload = getattr(job, "payload", None)
                if (
                    not isinstance(payload, dict)
                    or payload.get("session") != scope.session_key
                    or str(payload.get("sender_id", "")) != scope.sender_id
                ):
                    continue
                once = bool(getattr(job, "run_once", False))
                status = str(getattr(job, "status", "scheduled"))
                terminal = (
                    once
                    and status in {"completed", "failed", "cancelled"}
                    and not getattr(job, "next_run_time", None)
                )
                enabled = bool(getattr(job, "enabled", False))
                items.append(
                    {
                        "source": "schedule",
                        "item_id": str(job.job_id),
                        "title": str(job.name),
                        "note": payload.get("note", ""),
                        "status": status,
                        "enabled": enabled,
                        "run_once": once,
                        "cron_expression": getattr(job, "cron_expression", None),
                        "next_run": str(
                            getattr(job, "next_run_time", None)
                            or payload.get("run_at")
                            or ""
                        ),
                        "active": not terminal,
                        "category": "history"
                        if terminal
                        else "short_term"
                        if once
                        else "long_term",
                        "needs_attention": not terminal
                        and (not enabled or bool(getattr(job, "last_error", None))),
                        "error_kind": getattr(job, "last_error", None),
                    }
                )
        known = {(item["source"], item["item_id"]) for item in items}
        for key, label in labels.items():
            if key not in known and label["saved"] and not label["deleted_at"]:
                items.append(json.loads(label["saved"]))
        for item in items:
            label = labels.get((item["source"], item["item_id"]), {})
            item["archived"] = bool(label.get("archived_at")) and not item["active"]
            item["deleted"] = bool(label.get("deleted_at")) and not item["active"]
            item["catalog_revision"] = label.get("revision", 0)
            if (
                item["active"]
                and item["source"] != "background"
                and label.get("category") in {"long_term", "short_term"}
            ):
                item["category"] = label["category"]
            # The directory exposes no raw metadata, arguments or channel tokens.
            for key, value in item.items():
                if isinstance(value, str):
                    for secret in secrets:
                        value = value.replace(secret, "[密钥已隐藏]")
                    item[key] = rt._safe_text(value, 4000)
        return [item for item in items if not item["deleted"]], unavailable

    async def read(
        self, scope, *, category="active", offset=0, limit=20, include_archived=False
    ):
        """Return complete counts with explicitly paged, current-state records.

        Args:
            scope: Trusted bot, session and sender identity.
            category: active, all, or one of the four defined categories.
            offset: Nonnegative record offset.
            limit: Page size from one to fifty.
            include_archived: Include archived history in this page.

        Returns:
            Classified facts, exact counts and source completeness.
        """
        if (
            category not in {*TASK_DEFINITIONS, "active", "all"}
            or type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
            or type(include_archived) is not bool
        ):
            raise ValueError("invalid_catalog_query")
        items, unavailable = await self._records(scope)
        counts = {
            key: sum(item["category"] == key for item in items)
            for key in TASK_DEFINITIONS
        }
        filtered = [
            item
            for item in items
            if (include_archived or not item["archived"])
            and (
                category == "all"
                or category == "active"
                and item["active"]
                or item["category"] == category
            )
        ]
        return {
            "ok": True,
            "definitions": TASK_DEFINITIONS,
            "counts": counts,
            "active_total": sum(
                counts[key] for key in ("long_term", "short_term", "background")
            ),
            "archived_count": sum(item["archived"] for item in items),
            "total": len(filtered),
            "items": filtered[offset : offset + limit],
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(filtered) else None,
            "observed_at": self.runtime.service.clock(),
            "complete": not unavailable,
            "unavailable_sources": unavailable,
        }

    async def change(self, scope, source, item_id, action, *, category=None):
        """Change presentation only; never cancel or reschedule real work.

        Args:
            scope: Host-bound identity.
            source: background, schedule or script.
            item_id: Actual ID from a current directory response.
            action: classify, archive or unarchive.
            category: long_term or short_term, for future work only.

        Returns:
            Factual operation receipt without implying execution success.
        """
        items, unavailable = await self._records(scope)
        item = next(
            (
                item
                for item in items
                if item["source"] == source and item["item_id"] == item_id
            ),
            None,
        )
        if item is None:
            return {
                "ok": False,
                "code": "source_unavailable" if source in unavailable else "not_found",
            }
        if action == "classify" and (
            source == "background"
            or category not in {"long_term", "short_term"}
            or not item["active"]
        ):
            return {"ok": False, "code": "only_future_schedules_can_be_classified"}
        if action == "archive" and item["active"]:
            return {
                "ok": False,
                "code": "active_work_cannot_be_archived",
                "status": item["status"],
            }
        if action not in {"classify", "archive", "unarchive"}:
            return {"ok": False, "code": "invalid_action"}
        identity = (scope.bot_id, scope.session_key, scope.sender_id, source, item_id)
        with self.runtime.service._transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO pc_task_catalog(bot_id,session_key,sender_id,source,item_id) VALUES(?,?,?,?,?)",
                identity,
            )
            if action == "classify":
                db.execute(
                    "UPDATE pc_task_catalog SET category=?,revision=revision+1 WHERE bot_id=? AND session_key=? AND sender_id=? AND source=? AND item_id=?",
                    (category, *identity),
                )
            else:
                archived = self.runtime.service.clock() if action == "archive" else None
                db.execute(
                    "UPDATE pc_task_catalog SET archived_at=?,revision=revision+1 WHERE bot_id=? AND session_key=? AND sender_id=? AND source=? AND item_id=?",
                    (archived, *identity),
                )
        return {
            "ok": True,
            "changed": True,
            "source": source,
            "item_id": item_id,
            "action": action,
            "status": item["status"],
            "category": category or item["category"],
        }

    async def delete(self, scope, source, item_id):
        """Erase archived terminal content while retaining minimal replay keys.

        Args:
            scope: Host-bound identity.
            source: Actual record source from the directory.
            item_id: Archived record identifier.

        Returns:
            A deletion receipt, or the concrete remaining dependency.
        """
        rt = self.runtime
        identity = (scope.bot_id, scope.session_key, scope.sender_id, source, item_id)
        items, unavailable = await self._records(scope)
        item = next(
            (v for v in items if v["source"] == source and v["item_id"] == item_id),
            None,
        )
        if item is not None and item["active"]:
            return {"ok": False, "code": "active_work_cannot_be_deleted"}
        # A cron record needs its scheduler removed before erasing its history.
        if source == "schedule":
            if unavailable:
                return {"ok": False, "code": "schedule_reader_unavailable"}
            with rt.service._transaction() as db:
                related = db.execute(
                    "SELECT 1 FROM pt_tasks WHERE bot_id=? AND session_key=? AND sender_id=? AND json_extract(metadata,'$.schedule_rule_id')=? AND status NOT IN ('succeeded','failed','cancelled')",
                    (*identity[:3], item_id),
                ).fetchone()
                if related:
                    return {"ok": False, "code": "active_related_work"}
            if item is not None and item.get("archived"):
                await rt.cron_manager.delete_job(item_id)
        with rt.service._transaction() as db:
            label = db.execute(
                "SELECT * FROM pc_task_catalog WHERE bot_id=? AND session_key=? AND sender_id=? AND source=? AND item_id=?",
                identity,
            ).fetchone()
            if label and label["deleted_at"]:
                return {"ok": True, "code": "already_deleted", "changed": False}
            if item is None:
                return {"ok": False, "code": "not_found"}
            if not label or not label["archived_at"]:
                return {"ok": False, "code": "archive_required"}
            if source == "background":
                row = db.execute(
                    "SELECT status,lease_token FROM pt_tasks WHERE task_id=? AND bot_id=? AND session_key=? AND sender_id=?",
                    (item_id, *identity[:3]),
                ).fetchone()
                if (
                    not row
                    or row["status"] not in BACKGROUND_TERMINAL
                    or row["lease_token"] is not None
                ):
                    return {"ok": False, "code": "active_work_cannot_be_deleted"}
                if db.execute(
                    "SELECT 1 FROM pt_outbox WHERE task_id=? AND state IN ('pending','sending','delivery_unknown')",
                    (item_id,),
                ).fetchone():
                    return {"ok": False, "code": "pending_delivery"}
                if db.execute(
                    "SELECT 1 FROM ic_channels WHERE bot_id=? AND session_key=? AND sender_id=? AND json_extract(metadata,'$.source_task_id')=? AND status!='closed' AND expires_at>?",
                    (*identity[:3], item_id, rt.service.clock()),
                ).fetchone():
                    return {"ok": False, "code": "active_related_work"}
                for table in ("pt_steps", "pt_attempts", "pt_corrections", "pt_outbox"):
                    db.execute(f"DELETE FROM {table} WHERE task_id=?", (item_id,))
                db.execute(
                    "UPDATE pt_tasks SET goal='[已删除历史任务]',workdir='',metadata=?,result=NULL,error_kind=NULL,revision=revision+1 WHERE task_id=?",
                    (json.dumps({"history_deleted": True}), item_id),
                )
            elif source == "script":
                row = db.execute(
                    "SELECT status FROM ic_scripts WHERE id=? AND channel_id=?",
                    (item_id, item["channel_id"]),
                ).fetchone()
                if not row or row["status"] not in SCRIPT_TERMINAL:
                    return {"ok": False, "code": "active_work_cannot_be_deleted"}
                if db.execute(
                    "SELECT 1 FROM ic_script_events WHERE script_id=? AND callback_status IN ('pending','sending')",
                    (item_id,),
                ).fetchone():
                    return {"ok": False, "code": "pending_script_feedback"}
                db.execute(
                    "UPDATE ic_scripts SET path='[已删除历史脚本]',arguments='[]',stdout='',stderr='',error_code=NULL WHERE id=?",
                    (item_id,),
                )
                db.execute(
                    "UPDATE ic_script_events SET receipt='{}' WHERE script_id=?",
                    (item_id,),
                )
            elif source != "schedule":
                return {"ok": False, "code": "invalid_source"}
            db.execute(
                "UPDATE pc_task_catalog SET deleted_at=?,saved=NULL,revision=revision+1 WHERE bot_id=? AND session_key=? AND sender_id=? AND source=? AND item_id=?",
                (rt.service.clock(), *identity),
            )
        return {
            "ok": True,
            "changed": True,
            "action": "delete",
            "source": source,
            "item_id": item_id,
            "note": "历史正文已删除；仅保留最小防重放标记，工作文件未删除。",
        }
