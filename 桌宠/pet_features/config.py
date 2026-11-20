"""扩展功能独立配置；不读取或覆盖原模型、语音和人设。"""
import copy
import json
import math
import os
import threading
import uuid
from pathlib import Path


def defaults():
    cfg = {
        "version": 1,
        "feeding": {"enabled": True},
        "weather": {"enabled": False, "cities": [], "cache_seconds": 1800,
                    "report_enabled": False, "report_time": "08:00", "rain_enabled": False},
        "media": {"enabled": False, "lyrics_enabled": False, "animation_enabled": False,
                  "offset_seconds": 0.0, "show_lyrics": False, "word_lyrics": False},
        "system": {"enabled": False, "cpu_threshold": 90, "memory_threshold": 95,
                   "gpu_threshold": 80, "sustain_seconds": 30, "cooldown_seconds": 600,
                   "sample_seconds": 10, "cpu_enabled": True, "memory_enabled": True, "gpu_enabled": True},
        "apps": {"enabled": False, "rules": [], "track_time": False,
                 "break_enabled": False, "break_minutes": 60},
        "reminders": {"enabled": False, "rules": []},
        "phrases": {"enabled": False, "idle_enabled": False, "return_enabled": False,
                    "idle_minutes": 30, "return_minutes": 10, "interval_minutes": 30, "items": []},
        "hotkeys": {"enabled": False, "rules": []},
        "agent": {"enabled": False, "directory": "", "animation_enabled": True, "text_enabled": False},
        "usage": {"enabled": False, "directory": "", "include_pet": True},
        "balance": {"enabled": False, "provider": "auto", "profile_id": "",
                    "auto_refresh": False, "refresh_minutes": 10, "currency": "CNY",
                    "manual_balance": 0.0, "prices": []},
        "behavior": {"enabled": False, "mode": "stationary", "speed": 30,
                     "snap": False, "flip": False, "facing": "auto", "locked": False,
                     "click_through": False, "layer": "top", "opacity": 100},
        "interaction": {"enabled": True, "fling_enabled": True, "bounce_enabled": True,
                        "rope_enabled": True, "gravity_enabled": False, "sling_enabled": True,
                        "click_enabled": True, "trail_enabled": True, "auto_sleep": False,
                        "sleep_minutes": 10, "gravity": 1000, "restitution": 0.65,
                        "friction": 0.985, "sling_power": 8.0},
        "resources": {"enabled": False, "selected": "", "items": [], "mirrored": False},
        "sound": {"enabled": False, "path": "", "volume": 40, "interval_ms": 800},
        "appearance": {"enabled": False, "background": "", "accent": "#ff4e88",
                       "opacity": 100, "title": "", "icon": "", "reduce_motion": False,
                       "remember_size": True, "width": 820, "height": 640, "performance": "original"},
        "cloud": {"enabled": False, "system": False, "weather": False, "usage": False},
    }
    for name, section in cfg.items():
        if isinstance(section, dict) and name not in ("appearance", "behavior", "interaction", "resources", "hotkeys", "cloud"):
            section.update(voice_enabled=False, voice_probability=0)
    return cfg


def _merge(base, extra):
    result = copy.deepcopy(base)
    for key, value in extra.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


class FeaturesStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    @staticmethod
    def validate(cfg):
        for name, expected in defaults().items():
            value = cfg.get(name)
            if type(value) is not type(expected):
                raise ValueError("扩展设置格式不正确：" + name)
            if not isinstance(expected, dict):
                continue
            for key, default in expected.items():
                entry = value.get(key)
                valid = type(entry) is type(default) or (type(default) is float and type(entry) in (float, int))
                if not valid:
                    raise ValueError("扩展设置类型不正确：" + name + "/" + key)
                if isinstance(entry, float) and not math.isfinite(entry):
                    raise ValueError("数字参数必须为有限值：" + name + "/" + key)
                if (key.endswith("probability") or key in ("opacity", "volume")) and not 0 <= entry <= 100:
                    raise ValueError("百分比须在0至100之间")
                if key.endswith(("seconds", "minutes", "_ms")) and (entry < 0 if key != "offset_seconds" else abs(entry) > 3600):
                    raise ValueError("时间参数超出范围")
                if key in ("rules", "items", "cities", "prices") and not all(isinstance(i, dict) for i in entry):
                    raise ValueError("列表条目格式不正确：" + name)
        for key, low, high in (("gravity", 0, 3000), ("restitution", 0, 1),
                               ("friction", 0.8, 1), ("sling_power", 1, 30),
                               ("sleep_minutes", 1, 120)):
            if not low <= cfg["interaction"][key] <= high:
                raise ValueError("交互参数超出范围：" + key)
        if cfg["behavior"]["mode"] not in ("stationary", "walk", "follow"):
            raise ValueError("请选择原地、散步或跟随")
        if cfg["behavior"]["layer"] not in ("top", "normal", "bottom"):
            raise ValueError("窗口层级不正确")

    def load(self):
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                if not isinstance(data, dict):
                    raise ValueError("配置应为对象")  # noqa: TRY004 - 配置错误统一ValueError并保留原文件。
                cfg = _merge(defaults(), data)
                self.validate(cfg)
                return cfg
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError("扩展配置读取失败，原文件已保留：" + str(exc)) from exc

    def save(self, cfg):
        self.validate(cfg)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temporary.open("x", encoding="utf-8") as target:
                    json.dump(cfg, target, ensure_ascii=False, indent=2)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def update_section(self, name, values):
        with self.lock:
            cfg = self.load()
            cfg[name] = _merge(cfg.get(name, {}), values)
            self.save(cfg)
            return cfg
