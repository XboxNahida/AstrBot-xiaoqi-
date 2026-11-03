"""Manage a single shared, non-executing task-tool upload directory."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import stat
import tempfile
from pathlib import Path

from . import page_api_tasks
from .page_api_tasks import _authenticated

MAX_TOOL_BYTES = 8 * 1024 * 1024


def _shared_tools_root(runtime):
    """Reject redirected paths before host-owned upload operations."""
    root = Path(runtime.workspace) / "tools"
    for path in (root, *root.parents):
        if path.exists() and (
            path.is_symlink()
            or getattr(path.lstat(), "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise PermissionError("Shared tools directory must not be redirected")
    root.mkdir(parents=True, exist_ok=True)
    return root


class SharedTaskToolsPageMixin:
    """Require dashboard authentication; uploaded files never execute here."""

    @_authenticated
    async def get_shared_task_tools(self, username):
        root = _shared_tools_root(self.plugin._persistent_task_runtime)
        files = []
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                continue
            if path.name.startswith(".upload-"):
                continue
            files.append(
                {"name": path.name, "size": info.st_size, "modified": info.st_mtime}
            )
            if len(files) >= 1000:
                break
        return self._ok(
            {
                "directory": str(root),
                "shared": True,
                "files": files,
                "max_bytes": MAX_TOOL_BYTES,
            }
        )

    @_authenticated
    async def upload_shared_task_tool(self, username):
        values = await page_api_tasks.request.get_json(silent=True) or {}
        name, encoded = values.get("name"), values.get("content_base64")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 180
            or name.startswith(".")
            or name.rstrip(" .") != name
            or re.search(r'[\\/:*?"<>|\x00-\x1f]', name)
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name)
            or not isinstance(encoded, str)
            or len(encoded) > ((MAX_TOOL_BYTES + 2) // 3) * 4
        ):
            raise ValueError("Invalid tool file")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Invalid tool bytes") from None
        if len(data) > MAX_TOOL_BYTES:
            raise ValueError("Tool file is too large")
        root = _shared_tools_root(self.plugin._persistent_task_runtime)
        target = root / name
        if target.exists() and (
            not target.is_file()
            or target.is_symlink()
            or getattr(target.lstat(), "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise PermissionError("Tool target must be a plain file")
        if target.exists() and values.get("replace") is not True:
            return self._error("已有同名文件，请明确勾选覆盖后再上传", status_code=409)

        def save():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".upload-", dir=root, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        await asyncio.to_thread(save)
        return self._ok(
            {"name": name, "size": len(data), "shared": True, "executed": False}
        )
