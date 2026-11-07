"""Fixed local reads of ordinary files, with no command or write capability."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from pathlib import Path


class TaskOrdinaryReader:
    """Separate broad ordinary-file reads from restricted execution identities.

    Args:
        current_workspace: Resolve a host-bound actor's own work directory.
        authorize: Recheck that the real actor still has task read access.
        sensitive_roots: Host-owned private data and credential directories.
        redact: Remove configured secret values before returning any text.
    """

    def __init__(self, *, current_workspace, authorize, sensitive_roots, redact, readable_source_roots=()):
        self.current_workspace = current_workspace
        self.authorize = authorize
        self.sensitive = tuple(Path(p).resolve() for p in sensitive_roots)
        self.redact = redact
        self.readable_source_roots = tuple(Path(p).resolve() for p in readable_source_roots)

    def check_access(self, actor):
        """Check current identity and revocation without requiring a shell login."""
        if self.authorize(actor) is not True:
            raise PermissionError("task_authorization_required")

    def _checked(self, path):
        path = Path(os.path.abspath(path))
        for part in path.parts[1:]:
            low = part.casefold()
            if (
                ":" in part or part.rstrip(" .") != part
                or low in {".ssh", ".aws", ".azure", ".codex"}
                or low in {"ntuser.dat", "login data", "web data", "local state"}
                or low.startswith((".interaction-", "id_rsa", "id_ed25519"))
                or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", low)
            ):
                raise PermissionError("sensitive_path")
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        if os.name == "nt" and path.is_relative_to(windows / "System32/config"):
            raise PermissionError("sensitive_path")
        if any(path.is_relative_to(root) for root in self.sensitive) and not any(
            path.is_relative_to(root) for root in self.readable_source_roots
        ):
            raise PermissionError("sensitive_path")
        for entry in (*reversed(path.parents), path):
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("redirected_path")
        return path

    def _handle_path(self, fd, expected):
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            get_name = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            get_name.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            get_name.restype = wintypes.DWORD
            output = ctypes.create_unicode_buffer(32768)
            size = get_name(msvcrt.get_osfhandle(fd), output, len(output), 0)
            if not 0 < size < len(output):
                raise PermissionError("cannot_verify_open_file")
            final = output.value.removeprefix("\\\\?\\")
            if final.casefold() != str(expected).casefold():
                raise PermissionError("redirected_path")
            self._checked(Path(final))
        before, opened = expected.stat(), os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
            raise PermissionError("linked_or_changed_file")
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("regular_text_file_required")

    def _clean(self, text):
        if "\0" in text:
            raise ValueError("utf8_text_required")
        return self.redact(text, 12000)

    def _query(self, actor, action, path, offset):
        raw = str(path)
        if raw.startswith(("\\", "//")) or ":" in raw.replace("\\", "/")[2:]:
            raise PermissionError("local_file_path_required")
        target = Path(raw)
        if not target.is_absolute():
            target = self.current_workspace(actor) / target
        target = self._checked(target)
        next_offset = None
        if action == "read":
            fd = os.open(target, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                self._handle_path(fd, target)
                if os.fstat(fd).st_size > 131072:
                    raise ValueError("text_file_too_large")
                raw_bytes = bytearray()
                while len(raw_bytes) <= 131072:
                    block = os.read(fd, min(8192, 131073 - len(raw_bytes)))
                    if not block:
                        break
                    raw_bytes.extend(block)
                if len(raw_bytes) > 131072:
                    raise ValueError("text_file_too_large")
                self._handle_path(fd, target)
                try:
                    stdout = self._clean(raw_bytes.decode("utf-8-sig"))
                except UnicodeDecodeError as exc:
                    raise ValueError("utf8_text_required") from exc
            finally:
                os.close(fd)
        else:
            before = target.stat()
            if not stat.S_ISDIR(before.st_mode):
                raise NotADirectoryError(target)
            entries = []
            with os.scandir(target) as children:
                for index, child in enumerate(children):
                    if index >= 100000:
                        raise ValueError("directory_too_large")
                    try:
                        self._checked(Path(child.path))
                        info = os.stat(child.path, follow_symlinks=False)
                        if not stat.S_ISDIR(info.st_mode) and info.st_nlink != 1:
                            continue
                    except (PermissionError, FileNotFoundError):
                        continue
                    entries.append({"name": child.name, "directory": stat.S_ISDIR(info.st_mode), "size": info.st_size})
            self._checked(target)
            after = target.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise PermissionError("changed_directory")
            entries.sort(key=lambda item: item["name"].casefold())
            page = []
            for item in entries[offset:offset + 50]:
                if len(json.dumps([*page, item], ensure_ascii=False)) > 7000:
                    break
                page.append(item)
            stdout = json.dumps(page, ensure_ascii=False)
            if offset + len(page) < len(entries):
                next_offset = offset + len(page)
        return {"success": True, "code": "ordinary_read_ok", "path": str(target), "exit_code": 0, "stdout": stdout, "stderr": "", "next_offset": next_offset}

    async def call(self, actor, *, action="list", path=".", offset=0):
        """Read text or list a local directory; recheck grants before and after.

        Args:
            actor: Trusted caller, never taken from model arguments.
            action: read or list, with no command or write action.
            path: Absolute local path or a path relative to the caller's directory.
            offset: Zero-based directory pagination offset.

        Returns:
            Bounded, sanitized text or a JSON directory page.
        """
        self.check_access(actor)
        if action not in {"read", "list"} or type(offset) is not int or not 0 <= offset <= 100000:
            raise ValueError("invalid_read_query")
        result = await asyncio.to_thread(self._query, actor, action, path, offset)
        self.check_access(actor)
        return result
