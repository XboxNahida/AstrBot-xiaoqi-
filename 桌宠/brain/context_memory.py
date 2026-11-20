import os
import json
import time
import re
from typing import List, Dict, Optional
from app_paths import MEMORY_FILE
from config_manager import ConfigManager

class ContextMemoryManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        self.config_mgr = ConfigManager()
        self.memory_file = MEMORY_FILE
        self.max_total = 40  # Keep up to 40 recent interactions across sessions
        self.history: List[Dict[str, any]] = []
        self.load()

    def load(self):
        """Loads persistent interaction memory from local memory.json."""
        if not self.config_mgr.get("enable_persistent_memory", True):
            return

        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data[-self.max_total:]
                        print(f"[Memory] Loaded {len(self.history)} historical memories from {self.memory_file}", flush=True)
            except Exception as e:
                print(f"[Memory] Error loading {self.memory_file}: {e}", flush=True)

    def save(self):
        """Persists current interaction memory to local memory.json."""
        if not self.config_mgr.get("enable_persistent_memory", True):
            return

        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Memory] Error saving {self.memory_file}: {e}", flush=True)

    def record_critique(self, scene: str, window_title: str, text: str, pet_name: str = "哈Gemi"):
        if not text or not text.strip():
            return
        # Strip emotion tags like [shocked] so memory context is clean dialogue
        clean_text = re.sub(r'^\s*\[[a-zA-Z]+\]\s*', '', text).strip()
        t_now = time.time()
        self.history.append({
            "scene": scene,
            "title": window_title.strip() if window_title else "",
            "text": f"{pet_name}: {clean_text}",
            "time": t_now,
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_now))
        })
        if len(self.history) > self.max_total:
            self.history.pop(0)

        self.save()

    def record_user_comment(self, scene: str, window_title: str, user_comment: str):
        if not user_comment or not user_comment.strip():
            return
        t_now = time.time()
        self.history.append({
            "scene": scene,
            "title": window_title.strip() if window_title else "",
            "text": f"主人: {user_comment.strip()}",
            "time": t_now,
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t_now))
        })
        if len(self.history) > self.max_total:
            self.history.pop(0)

        self.save()

    def get_recent_context(self, scene: str, window_title: str, max_items: int = 2) -> List[str]:
        """
        Extract up to `max_items` previous critiques/dialogues for the current window or scene.
        Extremely lightweight: ~20-35 tokens total, strictly caps token expenditure
        while giving the AI continuity without repeating itself!
        """
        now = time.time()
        matched = []
        curr_title = window_title.strip() if window_title else ""

        # Search backward from newest to oldest
        for item in reversed(self.history):
            item_title = item.get("title", "")
            item_time = item.get("time", 0)

            # 1. Exact or partial match on window title (even across sessions/restarts!)
            if curr_title and (curr_title in item_title or item_title in curr_title):
                # Valid for up to 48 hours for the same specific video or game window
                if now - item_time <= 172800:
                    if item["text"] not in matched:
                        matched.append(item["text"])
            # 2. Or same specialized scene within recent active session (last 30 minutes)
            elif scene in ["video", "game"] and item.get("scene") == scene:
                if now - item_time <= 1800:
                    if item["text"] not in matched:
                        matched.append(item["text"])

            if len(matched) >= max_items:
                break

        # Return chronological order (oldest to newest)
        return list(reversed(matched))

    def get_all_memories(self) -> List[Dict[str, any]]:
        """Returns all persistent memories for viewing in Settings center."""
        return list(self.history)

    def clear(self):
        """Clears all in-memory and on-disk historical memories."""
        self.history.clear()
        try:
            if os.path.exists(self.memory_file):
                with open(self.memory_file, "w", encoding="utf-8") as f:
                    f.write("[]\n")
        except Exception as e:
            print(f"[Memory] Error clearing {self.memory_file}: {e}", flush=True)
