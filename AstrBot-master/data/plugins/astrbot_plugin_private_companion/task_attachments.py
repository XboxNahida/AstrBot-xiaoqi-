"""将真实消息附件复制到本账号工作区，文件内容不构成指令或授权。"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from astrbot.core.message.components import File, Reply

from .task_ordinary_read import TaskOrdinaryReader


@contextmanager
def _locked_directories(path):
    """Windows复制期间禁止祖先被改名替换，防止宿主越界写入。"""
    handles = []
    kernel = None
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        for directory in (*reversed(path.parents), path):
            if not directory.exists():
                directory.mkdir()
            if kernel:
                handle = kernel.CreateFileW(str(directory), 0x80, 1, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise OSError(ctypes.get_last_error(), "attachment_directory_unavailable")
                handles.append(handle)
            info = directory.lstat()
            if directory.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("redirected_attachment_path")
        yield
    finally:
        if kernel:
            for handle in reversed(handles):
                kernel.CloseHandle(handle)


def _copy(source, workspace, source_roots, authorize):
    source = Path(os.path.abspath(source))
    if not any(source.is_relative_to(Path(root).resolve()) for root in source_roots):
        raise PermissionError("attachment_source_not_allowed")
    if authorize() is not True:
        raise PermissionError("attachment_write_not_authorized")
    checker = TaskOrdinaryReader(current_workspace=lambda _: workspace, authorize=lambda _: True, sensitive_roots=(), redact=lambda text, _: text)
    checker._checked(source)
    checker._checked(workspace)
    suffix = source.suffix if len(source.suffix) <= 16 and source.suffix[1:].isalnum() else ""
    target = workspace / ("input_" + uuid4().hex + suffix)
    total, digest = 0, hashlib.sha256()
    with _locked_directories(workspace):
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        try:
            checker._handle_path(fd, source)
            if os.fstat(fd).st_size > 512 * 1024 * 1024:
                raise ValueError("attachment_too_large")
            with target.open("xb") as output:
                while chunk := os.read(fd, 1024 * 1024):
                    total += len(chunk)
                    if total > 512 * 1024 * 1024 or authorize() is not True:
                        raise PermissionError("attachment_limit_or_authorization_changed")
                    digest.update(chunk)
                    output.write(chunk)
            checker._handle_path(fd, source)
            if authorize() is not True:
                raise PermissionError("attachment_write_not_authorized")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(fd)
    return {"path": str(target), "size": total, "sha256": digest.hexdigest(), "source_path": str(source)}


async def stage_event_files(event, workspace, source_roots, authorize):
    """只接受真实File组件，包括真实引用链；不解析模型提供的路径文本。"""
    components = list(getattr(event.message_obj, "message", ()) or ())
    files, seen = [], set()
    cancelled = threading.Event()
    def allowed():
        return not cancelled.is_set() and authorize() is True
    try:
        for part in components:
            candidates = part.chain or () if isinstance(part, Reply) else (part,)
            for item in candidates:
                if not isinstance(item, File):
                    continue
                source = await item.get_file()
                if not source or source in seen:
                    continue
                seen.add(source)
                copying = asyncio.create_task(asyncio.to_thread(_copy, source, Path(workspace), source_roots, allowed))
                try:
                    record = await asyncio.shield(copying)
                except asyncio.CancelledError:
                    cancelled.set()
                    # 不让取消遗留宿主复制线程；分块检查授权后及时退出。
                    result = await asyncio.gather(copying, return_exceptions=True)
                    if isinstance(result[0], dict):
                        files.append(result[0])
                    raise
                record["name"] = str(item.name or Path(source).name)
                files.append(record)
    except BaseException:
        if files:
            with _locked_directories(Path(workspace)):
                for record in files:
                    Path(record["path"]).unlink(missing_ok=True)
        raise
    return files
