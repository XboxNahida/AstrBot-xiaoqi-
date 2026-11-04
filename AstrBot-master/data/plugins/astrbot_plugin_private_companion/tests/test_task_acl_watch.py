"""Verify native recursive notifications retain the changed paths."""

import ctypes
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows change notification test")
def test_native_watch_retains_create_rename_delete_and_directory_paths(tmp_path):
    from task_acl_watch import WindowsAclWatch

    watch = WindowsAclWatch((tmp_path,))
    try:
        assert watch.take_changes() == ()
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        directory = tmp_path / "new-directory"
        first.write_text("synthetic input", encoding="utf-8")
        first.rename(second)
        directory.mkdir()
        expected = {first, second, directory}
        observed = set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            changes = watch.take_changes()
            assert changes is not None
            observed.update(changes)
            if expected <= observed:
                break
            time.sleep(0.01)
        assert expected <= observed
        second.unlink()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            changes = watch.take_changes()
            assert changes is not None
            if second in changes:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Deletion notification was lost")
    finally:
        watch.close()
    assert not watch.states


@pytest.mark.skipif(os.name != "nt", reason="Windows change notification test")
def test_native_watch_reports_acl_only_changes_and_overflow(tmp_path):
    from task_acl_watch import WindowsAclWatch

    target = tmp_path / "acl-only.txt"
    target.write_text("synthetic input", encoding="utf-8")
    watch = WindowsAclWatch((tmp_path,))
    kernel = watch.kernel
    try:
        code = "$p='PATH';$acl=[IO.File]::GetAccessControl($p);$sid=New-Object Security.Principal.SecurityIdentifier('S-1-5-21-1-2-3-1001');$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Deny')));[IO.File]::SetAccessControl($p,$acl)".replace(
            "PATH", str(target).replace("'", "''")
        )
        ps = (
            Path(os.environ["SystemRoot"])
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        result = subprocess.run(
            [str(ps), "-NoProfile", "-NonInteractive", "-Command", code],
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            changes = watch.take_changes()
            assert changes is not None
            if target in changes:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Security notification was lost")

        class OverflowKernel:
            def __getattr__(self, name):
                return getattr(kernel, name)

            def GetOverlappedResult(self, handle, overlapped, count, wait):
                ok = kernel.GetOverlappedResult(handle, overlapped, count, wait)
                if ok:
                    ctypes.cast(
                        count, ctypes.POINTER(wintypes.DWORD)
                    ).contents.value = 0
                return ok

        watch.kernel = OverflowKernel()
        (tmp_path / "overflow.txt").write_text("synthetic input", encoding="utf-8")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if watch.take_changes() is None:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Notification overflow must require a full scan")
    finally:
        watch.kernel = kernel
        watch.close()
