"""只读Windows前台事件及麦克风占用提示；不操控窗口、不录音。"""

import ctypes
import sys
import threading
from ctypes import wintypes

from PyQt6.QtCore import QObject, pyqtSignal


class _WinEventPump:
    def __init__(self, changed):
        self.changed = changed
        self.hook = None
        self.thread_id = 0
        self.callback = None
        self.error = ""
        self.ready = threading.Event()
        self.stopped = threading.Event()

    def run(self):
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            self.thread_id = kernel32.GetCurrentThreadId()
            callback_type = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD,
                                             wintypes.HWND, wintypes.LONG, wintypes.LONG,
                                             wintypes.DWORD, wintypes.DWORD)

            def foreground_changed(*args):
                self.changed()

            self.callback = callback_type(foreground_changed)
            self.user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                                   wintypes.HMODULE, callback_type,
                                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
            self.user32.SetWinEventHook.restype = wintypes.HANDLE
            self.user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
            self.user32.UnhookWinEvent.restype = wintypes.BOOL
            self.user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            self.user32.GetMessageW.restype = wintypes.BOOL
            self.user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
            self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            message = wintypes.MSG()
            # 先创建线程消息队列，stop()才能可靠投递WM_QUIT。
            self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self.hook = self.user32.SetWinEventHook(3, 3, None, self.callback, 0, 0, 0)
            if not self.hook:
                raise OSError("注册前台窗口事件失败")
            self.ready.set()
            while not self.stopped.is_set():
                status = self.user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if status <= 0:
                    break
        except (OSError, AttributeError) as exc:
            self.error = str(exc)
        finally:
            if self.hook:
                self.user32.UnhookWinEvent(self.hook)
                self.hook = None
            self.thread_id = 0
            self.ready.set()

    def stop(self):
        self.stopped.set()
        if self.thread_id:
            self.user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)


class ForegroundWatcher(QObject):
    """前台切换只发无参数changed信号，标题变化由运行时轻轮询补充。"""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pump = None
        self._thread = None
        self.status = "未启动"

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive() and self._pump.hook)

    def start(self):
        if self._thread and self._thread.is_alive():
            return self.running
        if sys.platform != "win32":
            self.status = "前台事件仅支持Windows"
            return False
        self._pump = _WinEventPump(self.changed.emit)
        self._thread = threading.Thread(target=self._pump.run, daemon=True, name="智能陪伴前台事件")
        self._thread.start()
        if not self._pump.ready.wait(2) or not self.running:
            self.stop()
            self.status = self._pump.error or "前台事件启动超时"
            return False
        self.status = "监听中"
        return True

    def stop(self):
        if self._pump:
            self._pump.stop()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.status = "已停止" if not self._thread or not self._thread.is_alive() else "正在停止"


def microphone_active() -> bool:
    """ConsentStore是尽力而为的占用提示；驱动/应用漏报或权限失败返回False。"""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        def active(key):
            try:
                start = winreg.QueryValueEx(key, "LastUsedTimeStart")[0]
                stop = winreg.QueryValueEx(key, "LastUsedTimeStop")[0]
                if type(start) is int and type(stop) is int and start > 0 and (stop == 0 or start > stop):
                    return True
            except OSError:
                pass
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(key, name) as child:
                        if active(child):
                            return True
                except OSError:
                    continue
            return False

        path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as root:
            return active(root)
    except (ImportError, OSError, ValueError, TypeError):
        return False
