"""Windows辅助不录音、不切换窗口，测试后释放原生Hook。"""

import sys
import time
from types import SimpleNamespace

import pytest


def test_microphone_registry_detects_active_desktop_app(monkeypatch):
    import smart_windows

    entries = {"": {}, "NonPackaged": {}, "NonPackaged\\app.exe": {"LastUsedTimeStart": 120, "LastUsedTimeStop": 0}}

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_key(root, path):
        path = path.split("microphone", 1)[-1].lstrip("\\") if isinstance(root, int) else root.path + ("\\" if root.path else "") + path
        if path not in entries:
            raise FileNotFoundError
        return Key(path)

    def query(key, name):
        if name not in entries[key.path]:
            raise FileNotFoundError
        return entries[key.path][name], 11

    def enum(key, index):
        prefix = key.path + ("\\" if key.path else "")
        children = [p[len(prefix):] for p in entries if p.startswith(prefix) and p != key.path and "\\" not in p[len(prefix):]]
        if index >= len(children):
            raise OSError
        return children[index]

    registry = SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=open_key, QueryValueEx=query, EnumKey=enum)
    monkeypatch.setitem(sys.modules, "winreg", registry)
    assert smart_windows.microphone_active() is True
    entries["NonPackaged\\app.exe"]["LastUsedTimeStop"] = 130
    assert smart_windows.microphone_active() is False
    entries.clear()
    assert smart_windows.microphone_active() is False


@pytest.mark.skipif(sys.platform != "win32", reason="需要Windows原生事件Hook")
def test_native_watcher_start_stop_releases_thread_and_hook():
    from PyQt6.QtWidgets import QApplication

    from smart_windows import ForegroundWatcher

    app = QApplication.instance() or QApplication([])
    watcher = ForegroundWatcher()
    events = []
    watcher.changed.connect(lambda: events.append(True))
    try:
        assert watcher.start() is True
        assert watcher.running is True
        pump = watcher._pump
        thread = watcher._thread
        pump.callback(None, 3, None, 0, 0, 0, 0)
        for _ in range(10):
            app.processEvents()
            if events:
                break
            time.sleep(0.01)
        assert events
        assert watcher.start() is True
        assert watcher._thread is thread
    finally:
        watcher.stop()
    assert not thread.is_alive() and not pump.hook
    assert watcher.running is False
    watcher.stop()
    assert watcher.start() is True
    watcher.stop()


def test_microphone_permission_failure_is_conservative(monkeypatch):
    import smart_windows

    def denied(*args):
        raise PermissionError

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(HKEY_CURRENT_USER=1, OpenKey=denied))
    assert smart_windows.microphone_active() is False
