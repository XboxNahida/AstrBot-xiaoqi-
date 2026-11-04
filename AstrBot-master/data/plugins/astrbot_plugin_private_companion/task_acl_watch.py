"""Collect recursive name/security changes without discarding changed paths."""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from pathlib import Path


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


class WindowsAclWatch:
    """Keep asynchronous native watches armed between verification calls.

    Args:
        roots: Exact protected directories selected by the trusted host.
    """

    def __init__(self, roots):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = wintypes.HANDLE
        pointer = ctypes.c_void_p
        for function, args, result in (
            (
                self.kernel.CreateFileW,
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    pointer,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    handle,
                ],
                handle,
            ),
            (
                self.kernel.CreateEventW,
                [pointer, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
                handle,
            ),
            (
                self.kernel.ReadDirectoryChangesW,
                [
                    handle,
                    pointer,
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    pointer,
                    ctypes.POINTER(_Overlapped),
                    pointer,
                ],
                wintypes.BOOL,
            ),
            (
                self.kernel.GetOverlappedResult,
                [
                    handle,
                    ctypes.POINTER(_Overlapped),
                    ctypes.POINTER(wintypes.DWORD),
                    wintypes.BOOL,
                ],
                wintypes.BOOL,
            ),
            (self.kernel.WaitForSingleObject, [handle, wintypes.DWORD], wintypes.DWORD),
            (self.kernel.ResetEvent, [handle], wintypes.BOOL),
            (
                self.kernel.CancelIoEx,
                [handle, ctypes.POINTER(_Overlapped)],
                wintypes.BOOL,
            ),
            (self.kernel.CloseHandle, [handle], wintypes.BOOL),
        ):
            function.argtypes, function.restype = args, result
        self.states = []
        self.validated = False
        try:
            for root in dict.fromkeys(Path(p).absolute() for p in roots):
                native = self.kernel.CreateFileW(
                    str(root), 1, 7, None, 3, 0x42000000, None
                )
                if not native or native == ctypes.c_void_p(-1).value:
                    raise PermissionError(
                        "Protected ACL monitor could not be established"
                    )
                event = self.kernel.CreateEventW(None, True, False, None)
                if not event:
                    self.kernel.CloseHandle(native)
                    raise PermissionError("Protected ACL monitor event failed")
                state = (
                    root,
                    native,
                    event,
                    ctypes.create_string_buffer(65536),
                    _Overlapped(event=event),
                )
                self.states.append(state)
                self._arm(state)
        except BaseException:
            self.close()
            raise

    def _arm(self, state):
        root, native, event, buffer, overlapped = state
        overlapped.internal = overlapped.internal_high = 0
        overlapped.offset = overlapped.offset_high = 0
        if not self.kernel.ResetEvent(event) or not self.kernel.ReadDirectoryChangesW(
            native,
            buffer,
            len(buffer),
            True,
            1 | 2 | 0x100,
            None,
            ctypes.byref(overlapped),
            None,
        ):
            raise PermissionError("Protected ACL monitoring failed")

    def take_changes(self):
        """Return paths, or None after overflow so the caller must scan all roots.

        Returns:
            Tuple of changed paths; an empty tuple means no queued changes.

        Raises:
            PermissionError: Notification state cannot be trusted.
        """
        paths = set()
        overflow = False
        for state in self.states:
            root, native, event, buffer, overlapped = state
            status = self.kernel.WaitForSingleObject(event, 0)
            if status == 0x102:
                continue
            if status != 0:
                raise PermissionError("Protected ACL monitoring state is unknown")
            count = wintypes.DWORD()
            ok = self.kernel.GetOverlappedResult(
                native, ctypes.byref(overlapped), ctypes.byref(count), False
            )
            if not ok and ctypes.get_last_error() != 1022:
                raise PermissionError("Protected ACL notification could not be read")
            if not ok or count.value == 0:
                overflow = True
            else:
                offset = 0
                raw = buffer.raw[: count.value]
                while True:
                    if offset + 12 > len(raw):
                        raise PermissionError("Truncated ACL notification")
                    following, action, length = struct.unpack_from("III", raw, offset)
                    if length % 2 or offset + 12 + length > len(raw):
                        raise PermissionError("Invalid ACL notification")
                    relative = Path(
                        raw[offset + 12 : offset + 12 + length].decode("utf-16-le")
                    )
                    if (
                        relative.is_absolute()
                        or relative.drive
                        or ".." in relative.parts
                    ):
                        raise PermissionError("ACL notification escaped protected root")
                    if action not in {1, 2, 3, 4, 5}:
                        raise PermissionError("Unknown ACL notification action")
                    paths.add(root / relative)
                    if not following:
                        break
                    if following < 12 + length:
                        raise PermissionError("Invalid ACL notification offset")
                    offset += following
            self._arm(state)
        return None if overflow else tuple(sorted(paths))

    def changed(self):
        """Retain the previous read-only diagnostic boolean interface."""
        return self.take_changes() != ()

    def close(self):
        """Cancel pending native reads before releasing their buffers and handles."""
        for _, native, event, _, overlapped in self.states:
            self.kernel.CancelIoEx(native, ctypes.byref(overlapped))
            count = wintypes.DWORD()
            self.kernel.GetOverlappedResult(
                native, ctypes.byref(overlapped), ctypes.byref(count), True
            )
            self.kernel.CloseHandle(event)
            self.kernel.CloseHandle(native)
        self.states.clear()
