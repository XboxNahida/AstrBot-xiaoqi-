"""真实Windows隐藏启动、原版窗口可见、单实例唤回与退出。"""

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def visible_settings_windows(pid):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows.argtypes = [callback_type, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    found = []

    def inspect(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, len(title))
            if "控制台与设置中心" in title.value:
                found.append(title.value)
        return True

    user32.EnumWindows(callback_type(inspect), None)
    return found


@pytest.mark.skipif(sys.platform != "win32", reason="Windows真实隐藏启动回归")
@pytest.mark.parametrize("mode", ["classic", "dynamic"])
def test_original_hidden_startup_visible_single_instance_and_stop(tmp_path, mode):
    root = Path(__file__).resolve().parents[1]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    command = [sys.executable, "-X", "utf8", "original_main.py", "--state-dir", str(tmp_path)]
    if mode == "dynamic":
        # 用户实际保存的逻辑坐标，在Round后1280宽桌面中几乎完全屏外。
        (tmp_path / "config.json").write_text(json.dumps({
            "pet_pos_x": 1226, "pet_pos_y": 531, "auto_mode": False,
            "enable_tts": False, "auto_check_update": False,
        }), encoding="utf-8")
    env = {**os.environ, "QT_QPA_PLATFORM": "windows"}
    process = subprocess.Popen(
        command + ["--mode", mode, "--snapshot-dir", str(tmp_path / "snapshots")],
        cwd=root, env=env, startupinfo=startup,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )
    try:
        until = time.monotonic() + 20
        snapshot = tmp_path / "snapshots" / f"{mode}-新增模型页.png"
        runtime_file = tmp_path / "companion_runtime.json"
        native_pid = process.pid
        while time.monotonic() < until and process.poll() is None:
            if runtime_file.exists():
                native_pid = json.loads(runtime_file.read_text(encoding="utf-8"))["pid"]
            if (tmp_path / "snapshots" / "窗口诊断.json").exists() and visible_settings_windows(native_pid):
                break
            time.sleep(0.05)
        assert process.poll() is None, process.communicate(timeout=2)
        assert snapshot.exists()
        assert visible_settings_windows(native_pid)
        diagnostic = json.loads((tmp_path / "snapshots" / "窗口诊断.json").read_text(encoding="utf-8"))
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        assert user32.IsWindowVisible(diagnostic["pet_hwnd"]), "Qt桌宠本体在Windows原生层仍隐藏"
        from PyQt6.QtCore import QRect
        assert QRect(*diagnostic["available_rect"]).contains(QRect(*diagnostic["pet_rect"]))
        reopen = subprocess.run(command, cwd=root, env=env, startupinfo=startup, capture_output=True, check=False, timeout=8)
        assert reopen.returncode == 0, reopen.stderr
        assert process.poll() is None
        assert visible_settings_windows(native_pid)
        stop = subprocess.run(command + ["--stop"], cwd=root, env=env, startupinfo=startup, capture_output=True, check=False, timeout=8)
        assert stop.returncode == 0, stop.stderr
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
        assert "Traceback" not in stderr
        assert "原版" in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
