"""Windows只读采样；工作集回收仅由显式函数调用执行。"""
import ctypes
import os
import shutil
import subprocess
from ctypes import wintypes


def _psutil(module=None):
    if module is not None:
        return module
    import psutil
    return psutil


def nvidia_temperature():
    """只查询NVIDIA命令行温度；无工具/驱动时明确不可用。"""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=2, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        return None
    values = [float(value.strip()) for value in result.stdout.splitlines() if value.strip()]
    return max(values) if values else None


def sample_system(module=None, gpu_sample=None, cfg=None):
    module = _psutil(module)
    result = {"cpu": None, "memory": None, "gpu": None, "errors": []}
    cfg = cfg or {}
    for key, read in (("cpu", lambda: module.cpu_percent(interval=None)),
                      ("memory", lambda: module.virtual_memory().percent)):
        if not cfg.get(key + "_enabled", True):
            continue
        try:
            result[key] = float(read())
        except Exception as exc:  # noqa: BLE001 - 插件采样边界，失败状态返回调用方
            result["errors"].append(f"{key}:{type(exc).__name__}")
    if cfg.get("gpu_enabled", False) or gpu_sample is not None:
        try:
            value = (gpu_sample or nvidia_temperature)()
            result["gpu"] = None if value is None else float(value)
        except Exception as exc:  # noqa: BLE001 - 可选驱动不可用不阻断CPU采样
            result["errors"].append(f"gpu:{type(exc).__name__}")
    return result


def foreground_info():
    if os.name != "nt":
        return {"pid": None, "title": "", "available": False}
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        return {"pid": None, "title": "", "available": False}
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    text = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
    user.GetWindowTextW(hwnd, text, len(text))
    return {"pid": pid.value, "title": text.value, "available": bool(pid.value)}


def idle_seconds():
    if os.name != "nt":
        return None
    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetTickCount.restype = wintypes.DWORD
    data = LastInput()
    data.cbSize = ctypes.sizeof(data)
    if not user.GetLastInputInfo(ctypes.byref(data)):
        return None
    return ((kernel.GetTickCount() - data.dwTime) & 0xFFFFFFFF) / 1000


def sample_applications(module=None):
    module = _psutil(module)
    foreground = foreground_info()
    running, name, errors = [], "", 0
    try:
        for proc in module.process_iter(["pid", "name"]):
            try:
                exe = (proc.info.get("name") or "").lower()
                running.append(exe)
                if proc.info["pid"] == foreground["pid"]:
                    name = exe
            except Exception:  # noqa: BLE001 - 进程可在枚举期间消失或拒绝访问
                errors += 1
    except Exception:  # noqa: BLE001 - 系统枚举失败向调用方返回状态
        errors += 1
    idle = idle_seconds()
    return {"foreground": name, "running": running, "title": foreground["title"],
            "idle_seconds": idle, "idle": idle is not None and idle >= 120,
            "available": foreground["available"], "errors": errors}


CRITICAL = {"system", "system idle process", "registry", "smss.exe", "csrss.exe",
            "wininit.exe", "services.exe", "lsass.exe", "winlogon.exe", "svchost.exe",
            "dwm.exe", "fontdrvhost.exe", "secure system", "memory compression"}


def _trim_pid(pid):
    if os.name != "nt":
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
    psapi.EmptyWorkingSet.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x0100 | 0x0400, False, pid)
    if not handle:
        return False
    try:
        return bool(psapi.EmptyWorkingSet(handle))
    finally:
        kernel.CloseHandle(handle)


def trim_working_sets(module=None, *, own_pid=None, foreground=None, trim=None):
    """手动工具；前台不可知时全部跳过，绝不绕过保护猜测目标。"""
    module = _psutil(module)
    own_pid = os.getpid() if own_pid is None else own_pid
    foreground = foreground_info()["pid"] if foreground is None else foreground
    trim = trim or _trim_pid
    result = {"success": 0, "skipped": 0, "failed": 0}
    try:
        for proc in module.process_iter(["pid", "name"]):
            try:
                pid = proc.info["pid"]
                name = (proc.info.get("name") or "").lower()
                if foreground is None or pid in (0, 4, own_pid, foreground) or not name or name in CRITICAL:
                    result["skipped"] += 1
                    continue
                result["success" if trim(pid) else "failed"] += 1
            except Exception:  # noqa: BLE001 - 单一进程失败不影响其余计数
                result["failed"] += 1
    except Exception:  # noqa: BLE001 - 枚举失败纳入失败计数
        result["failed"] += 1
    return result
