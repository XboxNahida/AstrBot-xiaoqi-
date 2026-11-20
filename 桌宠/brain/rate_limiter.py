import time
from typing import Optional

class CritiqueRateLimiter:
    def __init__(self, cooldown_seconds: float = 45.0):
        self.cooldown_seconds = cooldown_seconds
        self.last_critique_time = 0.0
        self.last_title = ""
        self.last_scene = ""

    def should_trigger_auto(self, scene: str, window_title: str) -> bool:
        now = time.time()
        # Cooldown check
        if now - self.last_critique_time < self.cooldown_seconds:
            return False

        # 无论是否同一窗口，遵守用户填写的巡逻间隔。
        return True

    def record_critique(self, scene: str, window_title: str):
        self.last_critique_time = time.time()
        self.last_title = window_title
        self.last_scene = scene

    def reset_cooldown(self):
        self.last_critique_time = 0.0
