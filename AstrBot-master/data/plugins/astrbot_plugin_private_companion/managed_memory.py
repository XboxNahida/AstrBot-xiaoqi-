"""Gemini自行改写的短记忆：持久保存、软长度建议与宿主身份隔离。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import jsonschema

from astrbot.core.agent.tool import FunctionTool

SUGGESTED_CHARS = 800


def memory_key(bot_id, persona_id, kind, identity_id, group_id=""):
    """只接受宿主传入的真实范围，不通过昵称定位资料。"""
    values = [
        str(value or "").strip()
        for value in (bot_id, persona_id, kind, identity_id, group_id)
    ]
    bot, persona, kind, user, group = values
    if (
        not bot
        or not persona
        or kind not in {"private", "group_member", "group_shared"}
        or any(len(value) > 512 or any(ord(c) < 32 for c in value) for value in values)
        or (kind == "private" and (not user or group))
        or (kind == "group_member" and (not user or not group))
        or (kind == "group_shared" and (user or not group))
    ):
        raise ValueError("invalid_memory_scope")
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


class ManagedMemoryStore:
    """以版本比较实现原子改写；空笔记仍保留版本，防止旧种子复活。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS memory_notes ("
                "scope TEXT PRIMARY KEY,text TEXT NOT NULL,revision INTEGER NOT NULL,"
                "updated_at REAL NOT NULL,legacy TEXT NOT NULL DEFAULT '')"
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _note(row):
        text = row["text"] if row else ""
        return {
            "text": text,
            "revision": row["revision"] if row else 0,
            "updated_at": row["updated_at"] if row else None,
            "characters": len(text),
            "suggested_characters": SUGGESTED_CHARS,
            "legacy_available": bool(row and row["legacy"]),
        }

    def read(self, key):
        with closing(self._connect()) as db:
            return self._note(
                db.execute(
                    "SELECT * FROM memory_notes WHERE scope=?", (key,)
                ).fetchone()
            )

    def seed(self, key, text="", legacy=""):
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO memory_notes VALUES(?,?,1,?,?)",
                (key, text, time.time(), legacy),
            )
            return self._note(
                db.execute(
                    "SELECT * FROM memory_notes WHERE scope=?", (key,)
                ).fetchone()
            )

    def write(self, key, text, expected_revision):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM memory_notes WHERE scope=?", (key,)
            ).fetchone()
            note = self._note(current)
            if note["revision"] != expected_revision:
                return {"ok": False, "code": "revision_conflict", "note": note}
            if current:
                db.execute(
                    "UPDATE memory_notes SET text=?,revision=revision+1,updated_at=? WHERE scope=?",
                    (text, time.time(), key),
                )
            else:
                db.execute(
                    "INSERT INTO memory_notes VALUES(?,?,1,?,'')",
                    (key, text, time.time()),
                )
            note = self._note(
                db.execute(
                    "SELECT * FROM memory_notes WHERE scope=?", (key,)
                ).fetchone()
            )
            return {"ok": True, "code": "saved", "note": note}

    def legacy(self, key, offset=0, limit=4000):
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT legacy FROM memory_notes WHERE scope=?", (key,)
            ).fetchone()
        text = row[0] if row else ""
        return {
            "ok": True,
            "text": text[offset : offset + limit],
            "total_characters": len(text),
            "next_offset": offset + limit if offset + limit < len(text) else None,
        }


class ManagedMemoryTool(FunctionTool):
    """只有当前真实聊天可改写自己的笔记；不依赖后台任务授权。"""

    def __init__(self, runtime, event, store, scopes):
        super().__init__(
            name="pc_manage_memory",
            description=(
                "管理当前会话可见的简短记忆，不需要创建后台任务。action=read读取；"
                "write提交完整新笔记，替换当前文本，可自行新增、更正、合并、删减；text=''清空。"
                "写入必须提供最新expected_revision；冲突时读取返回的最新笔记后重新合并。"
                "target=self是当前用户本范围记忆；group仅群聊可用，保存群内公开共同经历。"
                "legacy按offset/next_offset分页读取尚待整理的旧资料，不会自动恢复旧资料。"
                "近期经历/话题、称呼、偏好、重要日期事件由你自主整理，本轮全部笔记尽量约800字，"
                "仅有新信息或需要更正、精简时写入，不必每轮调用。保存成功才可说已记住或忘记。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write", "legacy"]},
                    "target": {"type": "string", "enum": ["self", "group"]},
                    "text": {"type": "string"},
                    "expected_revision": {"type": "integer", "minimum": 0},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8000},
                },
                "additionalProperties": False,
            },
        )
        self.runtime, self.event, self.store = runtime, event, store
        self.scopes = dict(scopes)
        scope = runtime.scope(event)
        self.identity = (
            scope.bot_id,
            scope.session_key,
            scope.sender_id,
            event.get_group_id(),
        )
        self.persona = runtime._events[event].get("persona_id", "")

    async def call(self, context, **kwargs):
        rt, event = self.runtime, self.event
        scope = rt.scope(event)
        identity = (
            scope.bot_id,
            scope.session_key,
            scope.sender_id,
            event.get_group_id(),
        )
        if (
            event not in rt._events
            or not rt._trusted_event(event)
            or identity != self.identity
            or rt._events[event].get("persona_id", "") != self.persona
        ):
            return json.dumps({"ok": False, "code": "untrusted_memory_event"})
        try:
            jsonschema.validate(kwargs, self.parameters)
            target, action = kwargs.get("target", "self"), kwargs.get("action", "read")
            key = self.scopes.get(target)
            if key is None:
                raise ValueError("memory_target_unavailable")
            if action == "write":
                if "text" not in kwargs or "expected_revision" not in kwargs:
                    raise ValueError("memory_text_and_revision_required")
                text = rt._safe_text(
                    kwargs["text"], max(16000, len(kwargs["text"]) + 1)
                )
                result = await asyncio.to_thread(
                    self.store.write, key, text, kwargs["expected_revision"]
                )
            elif action == "legacy":
                result = await asyncio.to_thread(
                    self.store.legacy,
                    key,
                    kwargs.get("offset", 0),
                    kwargs.get("limit", 4000),
                )
            else:
                result = {
                    "ok": True,
                    "note": await asyncio.to_thread(self.store.read, key),
                }
            result["target"] = target
            return json.dumps(result, ensure_ascii=False)
        except (jsonschema.ValidationError, ValueError):
            return json.dumps({"ok": False, "code": "invalid_memory_arguments"})
        except sqlite3.Error:
            return json.dumps(
                {"ok": False, "code": "memory_store_unavailable", "retryable": True}
            )
