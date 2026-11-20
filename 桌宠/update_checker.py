import re
import os
import sys
import threading
import requests
import webbrowser
import zipfile
import tempfile
import subprocess
import shutil
from PyQt6.QtCore import QObject, pyqtSignal

GITHUB_REPO_OWNER = "LYW199911"
GITHUB_REPO_NAME = "DesktopGeminiGirl"
API_LATEST_RELEASE = f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/releases/latest"

def parse_version_tuple(v_str: str) -> tuple:
    """Parses version strings like 'v1.5.0', '1.5.0-dynamic', 'v1.4.2' into comparable integer tuples."""
    if not v_str:
        return (0, 0, 0)
    clean = re.sub(r'^[vV]', '', str(v_str).strip())
    clean = clean.split('-')[0].split('+')[0]
    parts = clean.split('.')
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])

def is_newer_version(current_ver: str, remote_ver: str) -> bool:
    """Returns True if remote_ver is strictly newer than current_ver."""
    curr = parse_version_tuple(current_ver)
    remote = parse_version_tuple(remote_ver)
    return remote > curr

def extract_update_zip(zip_path: str, extract_dir: str) -> str:
    """Extracts zip archive and returns the directory containing the executable payload."""
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(extract_dir)

    # Search for folder directly containing the executable
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            if f.endswith('.exe') or f in ('Ha-Gemi', 'Ha-Gemi-Dynamic'):
                return root
    return extract_dir

def launch_auto_installer(src_dir: str, dst_dir: str, exe_name: str, pid: int = None) -> bool:
    """Spawns an independent detached updater script to overwrite dst_dir from src_dir and restarts the app."""
    if pid is None:
        pid = os.getpid()

    if sys.platform == "win32":
        updater_dir = os.path.join(tempfile.gettempdir(), "HaGemi_Installer")
        os.makedirs(updater_dir, exist_ok=True)
        updater_bat = os.path.join(updater_dir, "hagemi_auto_updater.bat")

        # Use clean ASCII for Windows CMD native compatibility across all system locales
        bat_script = f"""@echo off
title HaGemi Auto Updater
echo [HaGemi Updater] Waiting for application (PID: {pid}) to exit...

:wait_loop
tasklist /fi "PID eq {pid}" 2>nul | findstr "{pid}" >nul
if %ERRORLEVEL%==0 (
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

timeout /t 1 /nobreak >nul
echo [HaGemi Updater] Updating files...
robocopy "{src_dir}" "{dst_dir}" /E /IS /IT /XF config.json memory.json >nul 2>&1

echo [HaGemi Updater] Restarting HaGemi...
start "" /D "{dst_dir}" "{exe_name}"

timeout /t 2 /nobreak >nul
rd /s /q "{src_dir}" 2>nul
(goto) 2>nul & del "%~f0" & exit
"""
        try:
            with open(updater_bat, "w", encoding="ascii", errors="ignore") as f:
                f.write(bat_script)
            subprocess.Popen(['cmd.exe', '/c', 'start', '/min', '', updater_bat], shell=False)
            return True
        except Exception as e:
            print(f"[UpdateChecker] Launch Windows updater failed: {e}", flush=True)
            return False

    elif sys.platform == "darwin":
        updater_sh = os.path.join(tempfile.gettempdir(), "hagemi_auto_updater.sh")
        sh_script = f"""#!/bin/bash
PID={pid}
SRC="{src_dir}"
DST="{dst_dir}"
EXE="{exe_name}"

while kill -0 "$PID" 2>/dev/null; do
    sleep 1
done
sleep 1

rsync -av --exclude="config.json" --exclude="memory.json" "$SRC/" "$DST/"
open "$DST/$EXE" 2>/dev/null || "$DST/$EXE" &
rm -rf "$SRC"
rm -- "$0"
"""
        try:
            with open(updater_sh, "w", encoding="utf-8") as f:
                f.write(sh_script)
            os.chmod(updater_sh, 0o755)
            subprocess.Popen(["bash", updater_sh])
            return True
        except Exception as e:
            print(f"[UpdateChecker] Launch macOS updater failed: {e}", flush=True)
            return False

    return False

class UpdateChecker(QObject):
    check_finished = pyqtSignal(dict, bool, str)  # (release_data, is_newer, error_msg)
    download_progress = pyqtSignal(int)          # 0 ~ 100
    download_finished = pyqtSignal(str)          # downloaded file path
    download_error = pyqtSignal(str)             # error message
    extract_finished = pyqtSignal(str)           # extracted payload directory
    extract_error = pyqtSignal(str)              # extraction error message

    def __init__(self, current_version: str = "v1.5.0", parent=None):
        super().__init__(parent)
        self.current_version = current_version
        self.latest_data = {}
        self.is_downloading = False

    def check_async(self):
        """Asynchronously queries GitHub Releases in a daemon thread."""
        def worker():
            try:
                headers = {
                    "User-Agent": "HaGemi-Update-Checker",
                    "Accept": "application/vnd.github.v3+json"
                }
                resp = requests.get(API_LATEST_RELEASE, headers=headers, timeout=6)
                if resp.status_code == 200:
                    data = resp.json()
                    self.latest_data = data
                    remote_tag = data.get("tag_name", "")
                    newer = is_newer_version(self.current_version, remote_tag)
                    self.check_finished.emit(data, newer, "")
                else:
                    self.check_finished.emit({}, False, f"GitHub 返回状态码: {resp.status_code}")
            except Exception as e:
                self.check_finished.emit({}, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def download_asset_async(self, download_url: str, dest_dir: str, filename: str):
        """Streams asset download with progress updates."""
        if self.is_downloading:
            return
        self.is_downloading = True

        def worker():
            try:
                os.makedirs(dest_dir, exist_ok=True)
                dest_path = os.path.join(dest_dir, filename)
                temp_path = dest_path + ".tmp"

                headers = {"User-Agent": "HaGemi-Update-Checker"}
                resp = requests.get(download_url, headers=headers, stream=True, timeout=15)
                resp.raise_for_status()

                total_size = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            percent = int((downloaded / total_size) * 100)
                            self.download_progress.emit(percent)

                if os.path.exists(dest_path):
                    try:
                        os.remove(dest_path)
                    except Exception:
                        pass
                os.rename(temp_path, dest_path)
                self.is_downloading = False
                self.download_finished.emit(dest_path)
            except Exception as e:
                self.is_downloading = False
                self.download_error.emit(str(e))

        threading.Thread(target=worker, daemon=True).start()

    def extract_asset_async(self, zip_path: str, extract_root: str):
        """Asynchronously extracts zip package and emits extract_finished across threads safely."""
        def worker():
            try:
                src_dir = extract_update_zip(zip_path, extract_root)
                self.extract_finished.emit(src_dir)
            except Exception as e:
                self.extract_error.emit(str(e))

        threading.Thread(target=worker, daemon=True).start()
