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

from astrbot.core.message.components import File, Image, Plain, Reply

from .task_ordinary_read import TaskOrdinaryReader


def describe_event_input(event):
    """Index real message components without downloading or reading their contents.

    Args:
        event: Trusted inbound event supplied by the platform.

    Returns:
        Ordered text and attachment facts with distinct current/quoted origins.
    """
    message = getattr(event, "message_obj", None)
    message_id = str(getattr(message, "message_id", "") or "")
    sender = getattr(event, "get_sender_id", None)
    result = {"message_id": message_id, "sender_id": str(sender() or "") if callable(sender) else "", "timestamp": str(getattr(message, "timestamp", "") or ""), "items": []}
    attachment_index = 0
    image_counts = {"current": 0, "quoted": 0}
    for position, part in enumerate(getattr(message, "message", ()) or (), 1):
        quoted = isinstance(part, Reply)
        for child_position, item in enumerate((part.chain or ()) if quoted else (part,), 1):
            kind = "file" if isinstance(item, File) else "image" if isinstance(item, Image) else "text" if isinstance(item, Plain) else ""
            if not kind:
                continue
            source = "quoted" if quoted else "current"
            row = {"position": f"{position}.{child_position}" if quoted else str(position), "source": source,
                   "message_id": str(part.id or "") if quoted else message_id, "type": kind}
            if kind == "text":
                row["text"] = str(item.text or "")[:8000]
            else:
                attachment_index += 1
                row["attachment_index"] = attachment_index
                if kind == "file":
                    row["name"] = str(item.name or "未命名文件")[:512]
                else:
                    image_counts[source] += 1
                    row["image_index"] = image_counts[source]
            result["items"].append(row)
    return result


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
    if os.name == "nt":
        # 只展开8.3别名，不解析符号链接；下方仍校验目录重解析和真实句柄。
        get_long = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        get_long.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
        get_long.restype = ctypes.c_ulong
        buffer = ctypes.create_unicode_buffer(32768)
        size = get_long(str(source), buffer, len(buffer))
        if not 0 < size < len(buffer):
            raise OSError(ctypes.get_last_error(), "attachment_source_unavailable")
        source = Path(buffer.value)
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
    files, seen = [], {}
    indexed = {row["position"]: row for row in describe_event_input(event)["items"]}
    cancelled = threading.Event()
    def allowed():
        return not cancelled.is_set() and authorize() is True
    try:
        for position, part in enumerate(components, 1):
            candidates = part.chain or () if isinstance(part, Reply) else (part,)
            for child_position, item in enumerate(candidates, 1):
                if not isinstance(item, File):
                    continue
                source = await item.get_file()
                if not source:
                    continue
                location = f"{position}.{child_position}" if isinstance(part, Reply) else str(position)
                if source in seen:
                    files.append({**seen[source], **indexed[location], "name": str(item.name or Path(source).name)})
                    continue
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
                record.update(indexed[location])
                seen[source] = record
                files.append(record)
    except BaseException:
        if files:
            with _locked_directories(Path(workspace)):
                for record in files:
                    Path(record["path"]).unlink(missing_ok=True)
        raise
    return files
