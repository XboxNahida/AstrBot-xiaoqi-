"""智能陪伴独立配置，不覆盖原模型、人设或密钥。"""

import copy
import json
import os
import uuid
from pathlib import Path

import config_manager


class SmartSettingsStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(config_manager.CONFIG_FILE).with_name("smart_settings.json")

    @staticmethod
    def defaults():
        return {
            "enabled": False, "events_enabled": True, "normalize_title": True,
            "debounce_enabled": True, "debounce_seconds": 45,
            "dedup_enabled": True, "dedup_minutes": 20,
            "cooldown_enabled": True, "cooldown_seconds": 900,
            "meeting_enabled": True, "dnd_enabled": False,
            "dnd_schedule_enabled": False, "dnd_start": "23:00", "dnd_end": "08:00",
            "icebreaker_enabled": False, "icebreaker_minutes": 120,
            "separate_model": False, "profile_id": "",
            "context_enabled": True, "context_chars": 400,
            "short_output": True, "max_output_tokens": 256,
            "low_reasoning": False, "reasoning_mode": "off",
            "count_limit_enabled": True, "hourly_limit": 3, "daily_limit": 12,
            "token_limit_enabled": False, "daily_tokens": 20000,
            "backoff_enabled": True, "backoff_minutes": 15,
            "voice_rules_enabled": True,
            "voice_sources": {key: {"enabled": True, "probability": p} for key, p in (
                ("user", 100), ("manual", 100), ("passive", 30), ("icebreaker", 50), ("greeting", 20),
                ("cloud", 20), ("reminder", 100))},
            "device_filter_enabled": False, "allowed_devices": [],
            "voice_interval_enabled": False, "voice_interval_seconds": 300,
            "voice_cache_enabled": True, "clean_speech": True,
            "short_speech": False, "speech_chars": 100,
            "user_interrupt": True, "volume": 70,
            "asr_filter_enabled": False, "asr_trim_enabled": False,
            "asr_threshold": 400, "asr_confirm": False,
            "asr_limit_enabled": False, "asr_daily_count": 60, "asr_daily_seconds": 1800,
        }

    @staticmethod
    def validate(cfg):
        defaults = SmartSettingsStore.defaults()
        for key, value in defaults.items():
            if type(cfg.get(key)) is not type(value):
                raise ValueError("智能配置类型不正确：" + key)
        bounds = {"debounce_seconds": (1, 600), "dedup_minutes": (1, 1440),
                  "cooldown_seconds": (1, 3600), "icebreaker_minutes": (1, 1440),
                  "context_chars": (50, 4000), "max_output_tokens": (64, 4096),
                  "hourly_limit": (1, 100), "daily_limit": (1, 1000),
                  "daily_tokens": (1000, 10000000), "backoff_minutes": (1, 1440),
                  "voice_interval_seconds": (1, 3600), "speech_chars": (20, 500),
                  "volume": (0, 100), "asr_threshold": (1, 10000),
                  "asr_daily_count": (1, 1000), "asr_daily_seconds": (1, 86400)}
        for key, (low, high) in bounds.items():
            if not low <= cfg[key] <= high:
                raise ValueError("智能配置数值超出范围：" + key)
        for key in ("dnd_start", "dnd_end"):
            try:
                hour, minute = map(int, cfg[key].split(":"))
                if not 0 <= hour < 24 or not 0 <= minute < 60:
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ValueError("免打扰时间请填写HH:mm") from exc
        if cfg["reasoning_mode"] not in ("off", "low"):
            raise ValueError("思考强度请选择关闭或低")
        if not all(isinstance(i, str) for i in cfg["allowed_devices"]):
            raise ValueError("请选择有效音频输出设备")
        for source in defaults["voice_sources"]:
            entry = cfg["voice_sources"].get(source, {})
            if type(entry.get("enabled")) is not bool or type(entry.get("probability")) is not int or not 0 <= entry["probability"] <= 100:
                raise ValueError("播报概率应为0至100：" + source)

    def load(self):
        cfg = self.defaults()
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            cfg.update(data)
        self.validate(cfg)
        return cfg

    def save(self, cfg):
        self.validate(cfg)
        stored = copy.deepcopy(cfg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(stored, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
