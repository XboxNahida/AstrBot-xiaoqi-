import os
import time
from typing import List, Dict
from PyQt6.QtCore import QObject, pyqtSignal
from app_paths import BASE_DIR

LOG_FILE = os.path.join(BASE_DIR, "desktop_pet.log")

class LogManager(QObject):
    log_added = pyqtSignal(str, str, str)  # timestamp, level, text

    _instance = None

    def __init__(self):
        super().__init__()
        self.logs: List[Dict[str, str]] = []
        self.max_logs = 500
        self.log_file = LOG_FILE

    def log(self, level: str, message: str):
        t_str = time.strftime("%H:%M:%S")
        entry = {
            "time": t_str,
            "level": level.upper(),
            "message": message
        }
        self.logs.append(entry)
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

        line = f"[{t_str}] [{level.upper()}] {message}"
        print(line, flush=True)

        # Append to log file on disk
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

        self.log_added.emit(t_str, level.upper(), message)

    def get_all_logs(self) -> List[Dict[str, str]]:
        return list(self.logs)

    def clear(self):
        self.logs.clear()
        try:
            with open(self.log_file, "w", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] [INFO] 日志已清空\n")
        except Exception:
            pass

    def info(self, msg: str):
        self.log("INFO", msg)

    def critique(self, msg: str):
        self.log("CRITIQUE", msg)

    def scene(self, msg: str):
        self.log("SCENE", msg)

    def memory(self, msg: str):
        self.log("MEMORY", msg)

    def error(self, msg: str):
        self.log("ERROR", msg)


def get_log_manager() -> LogManager:
    """Return the one shared Qt logger without reinitializing a QObject."""
    # PyQt6 6.12 不支持这里的 QObject 自定义 __new__ 单例；在工厂中缓存。
    if LogManager._instance is None:
        LogManager._instance = LogManager()
    return LogManager._instance
