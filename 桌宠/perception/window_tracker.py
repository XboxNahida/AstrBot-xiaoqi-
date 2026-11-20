import os
import sys
import subprocess
from typing import Tuple, Dict, Any, Optional

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
else:
    ctypes = None
    wintypes = None
    user32 = None
    kernel32 = None
    PROCESS_QUERY_LIMITED_INFORMATION = 0

# Self-ignore list: never critique our own pet, terminal, or launcher!
SELF_PROCESS_NAMES = [
    "python.exe", "pythonw.exe", "python", "python3", "ha-gemi", "hagemi"
]

SELF_TITLE_KEYWORDS = [
    "gemini desktop pet", "ha-gemi", "哈gemi", "辉星喵喵子"
]

GAME_PROCESS_KEYWORDS = [
    "steam", "genshin", "starrail", "honkai", "epicgames", "league", "riot",
    "valorant", "dota2", "cs2", "csgo", "minecraft", "javaw", "blackmyth", "wukong",
    "eldenring", "monsterhunter", "overwatch", "apex", "pubg", "tslgame",
    "wutheringwaves", "zenlesszonezero", "zzz", "endfield", "palworld",
    "cyberpunk2077", "baldursgate3", "bg3", "sekiro", "darktide", "helldivers",
    "residentevil", "hades"
]

VIDEO_PROCESS_NAMES = [
    "potplayer64.exe", "potplayer.exe", "vlc.exe", "mpv.exe", "bilibili.exe",
    "iina", "vlc", "quicktime player", "bilibili"
]

VIDEO_TITLE_KEYWORDS = [
    "哔哩哔哩", "bilibili", "youtube", "腾讯视频", "爱奇艺", "优酷",
    "douyin", "抖音", "tiktok", "twitch", "acfun", "niconico",
    "直播", "live", "电影", "动漫", "剧场版", "ep", "第", "集", "season",
    "番剧", "录播", "up主", "up:"
]

DEV_PROCESS_KEYWORDS = [
    "code.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "clion64.exe", "cursor.exe", "sublime_text.exe",
    "code", "cursor", "xcode", "pycharm", "intellij", "clion", "terminal", "iterm2", "warp", "sublime text"
]

CURRENT_PID = os.getpid()

def get_foreground_window_info() -> Dict[str, Any]:
    """Extract foreground window handle/id, title, process name, window rect and categorized scene."""
    if IS_WIN:
        return _get_win_window_info()
    elif IS_MAC:
        return _get_mac_window_info()
    else:
        return _get_linux_window_info()

def _get_win_window_info() -> Dict[str, Any]:
    """Windows implementation via Win32 API."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"hwnd": 0, "title": "", "process": "", "scene": "desktop", "rect": None, "is_self": False}

    # Window title
    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value.strip()

    # Process name & PID
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process_name = ""

    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h_process:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(size)):
            process_name = os.path.basename(buf.value).lower()
        kernel32.CloseHandle(h_process)

    # Check if this window belongs to our desk pet or our own terminal
    is_self = False
    if pid.value == CURRENT_PID:
        is_self = True
    elif process_name in SELF_PROCESS_NAMES:
        is_self = True
    elif any(kw in title.lower() for kw in SELF_TITLE_KEYWORDS):
        is_self = True

    # Window Rect
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    window_rect = {
        "left": rect.left,
        "top": rect.top,
        "width": max(0, rect.right - rect.left),
        "height": max(0, rect.bottom - rect.top)
    }

    scene = classify_scene(process_name, title)

    return {
        "hwnd": hwnd,
        "title": title,
        "process": process_name,
        "scene": scene,
        "rect": window_rect,
        "is_self": is_self
    }

def _get_mac_window_info() -> Dict[str, Any]:
    """macOS implementation via AppleScript / System Events (zero external dependencies)."""
    try:
        script = '''
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set appPid to unix id of frontApp
    set winTitle to ""
    set winBounds to ""
    try
        set frontWin to front window of frontApp
        set winTitle to name of frontWin
        set winPos to position of frontWin
        set winSize to size of frontWin
        set winBounds to (item 1 of winPos as text) & "," & (item 2 of winPos as text) & "," & (item 1 of winSize as text) & "," & (item 2 of winSize as text)
    end try
    return appName & "|||" & winTitle & "|||" & appPid & "|||" & winBounds
end tell
'''
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=1.2,
            creationflags=0x08000000 if IS_WIN else 0
        )
        if res.returncode == 0 and res.stdout.strip():
            parts = res.stdout.strip().split("|||")
            proc_name = parts[0].strip() if len(parts) > 0 else ""
            title = parts[1].strip() if len(parts) > 1 else ""
            pid_str = parts[2].strip() if len(parts) > 2 else "0"
            bounds_str = parts[3].strip() if len(parts) > 3 else ""

            pid = int(pid_str) if pid_str.isdigit() else 0
            rect = None
            if bounds_str:
                try:
                    b = [int(float(x.strip())) for x in bounds_str.split(",") if x.strip()]
                    if len(b) == 4:
                        rect = {"left": b[0], "top": b[1], "width": b[2], "height": b[3]}
                except Exception:
                    pass

            is_self = False
            if pid == CURRENT_PID:
                is_self = True
            elif proc_name.lower() in [s.lower() for s in SELF_PROCESS_NAMES]:
                is_self = True
            elif any(kw in title.lower() for kw in SELF_TITLE_KEYWORDS):
                is_self = True

            scene = classify_scene(proc_name, title)
            return {
                "hwnd": pid,
                "title": title,
                "process": proc_name,
                "scene": scene,
                "rect": rect,
                "is_self": is_self
            }
    except Exception as e:
        pass

    return {"hwnd": 0, "title": "", "process": "", "scene": "desktop", "rect": None, "is_self": False}

def _get_linux_window_info() -> Dict[str, Any]:
    """Linux fallback implementation."""
    return {"hwnd": 0, "title": "", "process": "", "scene": "desktop", "rect": None, "is_self": False}

def classify_scene(process_name: str, title: str) -> str:
    p_lower = process_name.lower()
    t_lower = title.lower()

    # 1. Video & Live Stream Check (Priority high so web video gets recognized)
    if p_lower in VIDEO_PROCESS_NAMES:
        return "video"
    for kw in VIDEO_TITLE_KEYWORDS:
        if kw in t_lower:
            return "video"

    # 2. Game Check
    for kw in GAME_PROCESS_KEYWORDS:
        if kw in p_lower:
            return "game"
    if any(k in t_lower for k in ["steam", "game", "游戏", "原神", "崩坏", "星穹铁道", "黑神话", "艾尔登法环"]):
        return "game"

    # 3. Dev / Work Check
    for kw in DEV_PROCESS_KEYWORDS:
        if kw in p_lower:
            return "work"
    if any(k in t_lower for k in ["visual studio", "vscode", "pycharm", "intellij", "terminal", "powershell", "cmd.exe", "iterm", "cursor"]):
        return "work"

    # 4. Fallback to General
    return "general"
