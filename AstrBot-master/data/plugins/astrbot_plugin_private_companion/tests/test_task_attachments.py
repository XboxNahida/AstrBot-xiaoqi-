"""真实附件导入的权限、链接及取消回归。"""

import asyncio
import ctypes
import os
import threading
from types import SimpleNamespace

import pytest

from astrbot.core.message.components import File
from astrbot_plugin_private_companion import task_attachments as attachments


@pytest.mark.skipif(os.name != "nt", reason="Windows短路径兼容")
def test_windows_short_filename_is_same_upload_without_relaxing_boundaries(tmp_path):
    source = tmp_path / "长文件名的上传资料.txt"
    source.write_bytes(b"actual-upload")
    get_short = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
    get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    get_short.restype = ctypes.c_ulong
    buffer = ctypes.create_unicode_buffer(32768)
    assert get_short(str(source), buffer, len(buffer))
    copied = attachments._copy(buffer.value, tmp_path / "workspace", [tmp_path], lambda: True)
    from pathlib import Path
    assert Path(copied["path"]).read_bytes() == b"actual-upload"
    with pytest.raises(PermissionError):
        attachments._copy(buffer.value, tmp_path / "workspace", [tmp_path / "different"], lambda: True)


def message(*paths):
    return SimpleNamespace(message_obj=SimpleNamespace(message=[File(name=p.name, file=str(p)) for p in paths]))


@pytest.mark.asyncio
async def test_source_outside_upload_root_is_rejected(tmp_path):
    source = tmp_path / "private.zip"
    source.write_bytes(b"private")
    with pytest.raises(PermissionError):
        await attachments.stage_event_files(message(source), tmp_path / "tasks", (tmp_path / "uploads",), lambda: True)
    assert not (tmp_path / "tasks").exists()


@pytest.mark.asyncio
async def test_revoked_import_cleans_partial_copy(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    checks = 0

    def authorize():
        nonlocal checks
        checks += 1
        return checks < 3

    workspace = tmp_path / "tasks"
    with pytest.raises(PermissionError):
        await attachments.stage_event_files(message(source), workspace, (tmp_path,), authorize)
    assert list(workspace.iterdir()) == []
    assert source.exists()


@pytest.mark.asyncio
async def test_hardlinked_upload_is_rejected(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(b"private")
    (tmp_path / "link.zip").hardlink_to(source)
    with pytest.raises(PermissionError):
        await attachments.stage_event_files(message(source), tmp_path / "tasks", (tmp_path,), lambda: True)
    assert not list((tmp_path / "tasks").glob("input_*"))


@pytest.mark.asyncio
async def test_cancelled_import_waits_for_copy_cleanup(tmp_path, monkeypatch):
    source = tmp_path / "input.zip"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = attachments._copy

    def delayed(*args):
        started.set()
        assert release.wait(3)
        try:
            return original(*args)
        finally:
            finished.set()

    monkeypatch.setattr(attachments, "_copy", delayed)
    workspace = tmp_path / "tasks"
    task = asyncio.create_task(attachments.stage_event_files(message(source), workspace, (tmp_path,), lambda: True))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 3)
    assert not list(workspace.glob("input_*"))


@pytest.mark.asyncio
async def test_later_import_failure_cleans_earlier_inputs(tmp_path):
    source = tmp_path / "input.zip"
    source.write_bytes(b"first")
    upload = tmp_path / "uploads"
    upload.mkdir()
    source = upload / "input.zip"
    source.write_bytes(b"first")
    missing = tmp_path / "outside.zip"
    missing.write_bytes(b"outside")
    workspace = tmp_path / "tasks"
    with pytest.raises(PermissionError):
        await attachments.stage_event_files(message(source, missing), workspace, (upload,), lambda: True)
    assert list(workspace.iterdir()) == []
