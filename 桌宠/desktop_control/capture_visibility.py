"""采集自身透明窗背后的桌面，窗口保持显示和任务栏位置。"""
import ctypes
import sys
from ctypes import wintypes


def _api():
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    return user


def exclude_capture(widget, *, passthrough=True):
    """成功时返回恢复令牌；不支持时由调用方使用原隐藏后备路径。"""
    if sys.platform != 'win32':
        return None
    try:
        user, handle = _api(), int(widget.winId())
        affinity = wintypes.DWORD()
        if not user.GetWindowDisplayAffinity(handle, ctypes.byref(affinity)):
            return None
        style = user.GetWindowLongPtrW(handle, -20)
        # 原角色、气泡与操作窗均为Qt透明分层窗；避免把其他窗临时变成分层窗。
        if not style & 0x80000:
            return None
        if not user.SetWindowDisplayAffinity(handle, 0x11):
            return None
        if not passthrough:
            return handle, affinity.value, None
        ctypes.set_last_error(0)
        result = user.SetWindowLongPtrW(handle, -20, style | 0x20)
        if not result and ctypes.get_last_error():
            user.SetWindowDisplayAffinity(handle, affinity.value)
            return None
        return handle, affinity.value, bool(style & 0x20)
    except (OSError, AttributeError):
        return None


def restore_capture(token):
    handle, affinity, was_transparent = token
    try:
        user = _api()
        if was_transparent is False:
            current = user.GetWindowLongPtrW(handle, -20)
            user.SetWindowLongPtrW(handle, -20, current & ~0x20)
        user.SetWindowDisplayAffinity(handle, affinity)
    except (OSError, AttributeError):
        pass  # 退出/销毁后的句柄无需恢复，不能阻断停止任务。
