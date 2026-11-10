"""Expose the current bot's reaction library without selecting or sending for it."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path

import jsonschema
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image as PILImage

from astrbot.core.agent.tool import FunctionTool
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageChain

from .reaction_asset_library import get_reaction_asset_library
from .task_execution import actor_workspace
from .task_file_delivery import TaskArtifactReader


class ReactionLibraryTool(FunctionTool):
    """Bind library discovery, original images and delivery to one host scope."""

    def __init__(self, runtime, event=None):
        super().__init__(
            name="pc_reaction_library",
            description=(
                "查看当前机器人的表情包库。groups列分组；list按query/group分页；get按asset_ids"
                "查看原图并取得当前用户工作区中的原文件path，可交给改图或后台脚本；send按asset_ids"
                "向当前会话发送多张。source默认meme_manager，沿用当前机器人/人格的分组库；"
                "companion是网页独立素材库。先列出真实ID再get/send，不猜路径。"
                "没有固定单张限制，按语境决定是否发送及张数；按每张真实回执汇报，不附加固定话术。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["groups", "list", "get", "send"],
                    },
                    "source": {"type": "string", "enum": ["meme_manager", "companion"]},
                    "query": {"type": "string", "maxLength": 160},
                    "group": {"type": "string", "maxLength": 160},
                    "page": {"type": "integer", "minimum": 1},
                    "page_size": {"type": "integer", "minimum": 1, "maximum": 120},
                    "asset_ids": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 100},
                        "minItems": 1,
                        "maxItems": 50,
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )
        self.runtime, self.event = runtime, event
        self.scope = runtime.scope(event) if event is not None else None

    def _catalog(self, event, source):
        """Enumerate only the selected pack or enabled companion assets.

        Args:
            event: Host-bound frontend or restored task event.
            source: Explicit library kind, never a directory supplied by a model.

        Returns:
            Pack identity and available image metadata with checked source paths.
        """
        items = []
        if source == "companion":
            library = get_reaction_asset_library(self.runtime.plugin)
            if library is None:
                return "companion", items
            scope = "group" if event.get_group_id() else "private"
            page = 1
            while True:
                result = library.list_items(
                    status="enabled", scope=scope, page=page, page_size=120
                )
                for row in result["items"]:
                    path = library._path_for(row)
                    if path is not None:
                        items.append(
                            {
                                "id": "companion:" + row["id"],
                                "name": row["filename"],
                                "group": "companion",
                                "description": " ".join(
                                    [
                                        row["description"],
                                        row["visible_text"],
                                        *row["tags"],
                                        *row["emotions"],
                                        *row["intents"],
                                    ]
                                ),
                                "_path": path,
                            }
                        )
                if page >= result["pages"]:
                    break
                page += 1
            return "companion", items
        try:
            from meme_manager.backend.pack_resolver import resolve_pack_context
        except ImportError:
            return "unavailable", items
        persona = str(
            getattr(event, "private_companion_persona_id", "")
            or getattr(event, "_private_companion_emotion_persona_id", "")
        )
        pack = resolve_pack_context(
            session_id=event.session_id or event.unified_msg_origin,
            persona_id=persona,
            bot_id=event.get_platform_id(),
        )
        root = TaskArtifactReader._checked(None, Path(pack["memes_dir"]).absolute())
        if not root.is_dir():
            return str(pack["pack_id"]), items
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                name
                for name in dirs
                if not (Path(parent) / name).is_symlink()
                and not getattr((Path(parent) / name).lstat(), "st_file_attributes", 0)
                & 0x400
            )
            for name in sorted(files):
                path = Path(parent) / name
                if path.suffix.lower() not in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".bmp",
                }:
                    continue
                try:
                    TaskArtifactReader._checked(None, path)
                except PermissionError:
                    continue
                relative = path.relative_to(root).as_posix()
                group = relative.split("/", 1)[0] if "/" in relative else "default"
                identifier = hashlib.sha256(
                    (str(pack["pack_id"]) + ":" + relative).encode()
                ).hexdigest()
                items.append(
                    {
                        "id": identifier,
                        "name": name,
                        "group": group,
                        "description": str(
                            pack.get("category_mapping", {}).get(group, "")
                        ),
                        "_path": path,
                    }
                )
        return str(pack["pack_id"]), items

    def _original(self, actor, row):
        """Snapshot one checked original into this subject's own inputs directory.

        Args:
            actor: Host-provided account, never model fields.
            row: An entry enumerated from the selected library.

        Returns:
            Snapshot path, original bytes, and a bounded PNG visual preview.

        Raises:
            PermissionError: A link or redirected file was encountered.
            ValueError: The entry is not a supported image.
        """
        reader = self.runtime._file_delivery().reader
        path = reader._checked(row["_path"])
        with path.open("rb") as stream:
            reader._handle_path(stream.fileno(), path)
            raw = stream.read(20 * 1024 * 1024 + 1)
            reader._handle_path(stream.fileno(), path)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("image_size_limit_20_mib")
        with PILImage.open(io.BytesIO(raw)) as original:
            if original.width * original.height > 40_000_000:
                raise ValueError("image_dimensions_too_large")
            preview = original.convert("RGBA")
            preview.thumbnail((1024, 1024))
            output = io.BytesIO()
            preview.save(output, format="PNG")
        digest = hashlib.sha256(raw).hexdigest()
        root = actor_workspace(self.runtime.workspace, actor)
        target = root / "inputs" / "reactions" / (digest + path.suffix.lower())
        reader._checked(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        reader._checked(target)
        try:
            with target.open("xb") as stream:
                reader._handle_path(stream.fileno(), target)
                stream.write(raw)
                stream.flush()
                reader._handle_path(stream.fileno(), target)
        except FileExistsError:
            with target.open("rb") as stream:
                reader._handle_path(stream.fileno(), target)
                if stream.read(len(raw) + 1) != raw:
                    raise PermissionError("cached_image_changed")
        return target, output.getvalue()

    async def call(self, context, **kwargs):
        """Return library facts or per-image adapter receipts for the bound scope."""
        try:
            jsonschema.validate(kwargs, self.parameters)
            event = self.event or context.context.event
            if self.event is not None:
                if (
                    event not in self.runtime._events
                    or self.runtime.scope(event) != self.scope
                    or not self.runtime._trusted_event(event)
                ):
                    raise PermissionError("unregistered_event")
            else:
                job = getattr(event, "_private_companion_script_source_job", None)
                if job is None or not self.runtime.authorize_job(job):
                    raise PermissionError("task_authorization_required")
            actor = self.runtime.actor(event)
            pack, rows = await asyncio.to_thread(
                self._catalog, event, kwargs.get("source", "meme_manager")
            )
            action = kwargs["action"]
            if action == "groups":
                groups = {}
                for row in rows:
                    groups.setdefault(
                        row["group"],
                        {
                            "name": row["group"],
                            "description": row["description"],
                            "count": 0,
                        },
                    )["count"] += 1
                return json.dumps(
                    {"ok": True, "pack_id": pack, "groups": list(groups.values())},
                    ensure_ascii=False,
                )
            if action == "list":
                query = kwargs.get("query", "").casefold()
                rows = [
                    r
                    for r in rows
                    if (not kwargs.get("group") or r["group"] == kwargs["group"])
                    and all(
                        word
                        in (
                            r["name"] + " " + r["description"] + " " + r["group"]
                        ).casefold()
                        for word in query.split()
                    )
                ]
                page, size = kwargs.get("page", 1), kwargs.get("page_size", 40)
                return json.dumps(
                    {
                        "ok": True,
                        "pack_id": pack,
                        "total": len(rows),
                        "page": page,
                        "pages": max(1, (len(rows) + size - 1) // size),
                        "items": [
                            {k: v for k, v in r.items() if k != "_path"}
                            for r in rows[(page - 1) * size : page * size]
                        ],
                    },
                    ensure_ascii=False,
                )
            ids = kwargs.get("asset_ids")
            if not ids:
                raise ValueError("asset_ids_required")
            catalog = {r["id"]: r for r in rows}
            results, visuals = [], []
            for identifier in ids:
                row = catalog.get(identifier)
                if row is None:
                    results.append(
                        {
                            "id": identifier,
                            "ok": False,
                            "sent": False,
                            "code": "asset_not_available",
                        }
                    )
                    continue
                try:
                    path, preview = await asyncio.to_thread(self._original, actor, row)
                    result = {
                        "id": identifier,
                        "ok": True,
                        "path": str(path),
                        "name": row["name"],
                    }
                    if action == "send":
                        sent = await self.runtime.plugin.context.send_message(
                            event.unified_msg_origin,
                            MessageChain([Image.fromFileSystem(str(path))]),
                        )
                        result.update(
                            ok=sent is True, sent=sent is True, receipt_level="adapter"
                        )
                    else:
                        visuals.append(
                            ImageContent(
                                type="image",
                                mimeType="image/png",
                                data=base64.b64encode(preview).decode(),
                            )
                        )
                    results.append(result)
                except (OSError, ValueError, PermissionError) as exc:
                    results.append(
                        {
                            "id": identifier,
                            "ok": False,
                            "sent": False,
                            "code": type(exc).__name__,
                        }
                    )
            ok = all(r["ok"] for r in results)
            text = json.dumps(
                {"ok": ok, "success": ok, "pack_id": pack, "items": results},
                ensure_ascii=False,
            )
            if action == "get" and visuals:
                return CallToolResult(
                    content=[TextContent(type="text", text=text), *visuals],
                    isError=not ok,
                )
            return text
        except (
            jsonschema.ValidationError,
            OSError,
            ValueError,
            PermissionError,
        ) as exc:
            return json.dumps(
                {"ok": False, "success": False, "code": type(exc).__name__}
            )
