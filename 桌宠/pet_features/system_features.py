"""本地系统业务：纯数据输入，不依赖 Qt 或模型。"""
import random
import time
from datetime import datetime, timedelta
from typing import ClassVar


def event(identifier, source, text, action="reminder", priority=40):
    return {"id": identifier, "source": source, "text": text, "action": action,
            "priority": priority, "ttl": 120, "manual": False}


class PhraseBook:
    DEFAULTS: ClassVar[dict] = {"idle": ["休息一下，伸个懒腰吧。"],
                "return": ["欢迎回来！"], "rest": ["用了一阵子，记得休息哦。"]}

    def __init__(self, choice=None):
        self.choice = choice or random.choice

    def pick(self, category, custom=None):
        values = (custom or {}).get(category, self.DEFAULTS.get(category, []))
        values = [values] if isinstance(values, str) else values
        lines = [line.strip() for line in (values or [])
                 if isinstance(line, str) and line.strip()]
        return self.choice(lines) if lines else ""


class SystemMonitor:
    def __init__(self, sample=None, clock=None):
        self.sample = sample
        self.clock = clock or time.monotonic
        self.state = {}

    def check(self, sample=None, now=None, cfg=None):
        cfg = cfg or {}
        if not cfg.get("enabled", False):
            self.state.clear()
            return []
        now = self.clock() if now is None else now
        sample = self.sample() if sample is None and self.sample else sample or {}
        result = []
        for metric, threshold in (("cpu", 90), ("memory", 95), ("gpu", 80)):
            if not cfg.get(metric + "_enabled", True):
                self.state.pop(metric, None)
                continue
            start, fired, last = self.state.get(metric, (None, False, float("-inf")))
            value = sample.get(metric)
            if value is None or value < cfg.get(metric + "_threshold", threshold):
                self.state[metric] = (None, False, last)
                continue
            start = now if start is None or now < start else start
            if (not fired and now - start >= cfg.get("sustain_seconds", cfg.get("duration", 30))
                    and now - last >= cfg.get("cooldown_seconds", cfg.get("cooldown", 600))):
                label, unit = {"cpu": ("处理器（CPU）使用率", "%"),
                               "memory": ("系统内存占用率", "%"),
                               "gpu": ("显卡（GPU）温度", "℃")}[metric]
                limit = cfg.get(metric + "_threshold", threshold)
                result.append(event(f"system:{metric}:{now}", "system",
                                    f"{label}持续偏高：{value:g}{unit}（阈值{limit:g}{unit}）", "warning", 60))
                fired, last = True, now
            self.state[metric] = start, fired, last
        return result


class IdleTracker:
    """独立闲置/返回开关，采样缺失时不猜测用户状态。"""
    def __init__(self, clock=None, phrases=None):
        self.clock = clock or time.monotonic
        self.phrases = phrases or PhraseBook()
        self.last_idle = None
        self.last_event = {}

    def check(self, idle_seconds=None, now=None, cfg=None):
        cfg = cfg or {}
        now = self.clock() if now is None else now
        if not cfg.get("idle_enabled", False) and not cfg.get("return_enabled", False):
            self.last_idle = None
            self.last_event.clear()
            return []
        if idle_seconds is None:
            return []
        previous, self.last_idle = self.last_idle, idle_seconds
        category = ""
        if (cfg.get("return_enabled", False) and previous is not None
                and previous >= cfg.get("return_minutes", 5) * 60 and idle_seconds < previous):
            category = "return"
        elif cfg.get("idle_enabled", False) and idle_seconds >= cfg.get("idle_minutes", 5) * 60:
            category = "idle"
        if not category:
            return []
        last = self.last_event.get(category, float("-inf"))
        if now >= last and now - last < cfg.get("interval_minutes", 30) * 60:
            return []
        text = self.phrases.pick(category, cfg.get("items", {}))
        if not text:
            return []
        self.last_event[category] = now
        return [event(f"{category}:{now}", "phrases", text, "greeting" if category == "return" else "idle")]


class ApplicationTracker:
    """字符串输入只表示前台切换；传入running快照才判定真正启动和退出。"""
    def __init__(self, clock=None):
        self.clock = clock or datetime.now
        self.previous = None
        self.foreground = ""
        self.running = set()
        self.daily = {}
        self.continuous = 0
        self.due = {}

    def stats(self):
        return {day: dict(values) for day, values in self.daily.items()}

    def update(self, process, title="", now=None, cfg=None):
        cfg = cfg or {}
        now = self.clock() if now is None else now
        if not cfg.get("enabled", False):
            self.previous, self.foreground, self.running = None, "", set()
            self.continuous, self.due = 0, {}
            return []
        snapshot = process if isinstance(process, dict) else {}
        name = str(snapshot.get("foreground", process if not snapshot else "") or "").lower()
        present = {str(p).lower() for p in snapshot.get("running", [])}
        has_snapshot = "running" in snapshot
        gap = (now - self.previous).total_seconds() if self.previous else 0
        if (0 < gap <= cfg.get("max_gap", 120) and not snapshot.get("idle", False)
                and cfg.get("track_time", True)):
            cursor = self.previous
            while self.foreground and cursor < now:
                end = min(now, datetime.combine(cursor.date() + timedelta(days=1),
                                                datetime.min.time(), tzinfo=now.tzinfo))
                day = self.daily.setdefault(cursor.date().isoformat(), {})
                day[self.foreground] = day.get(self.foreground, 0) + (end - cursor).total_seconds()
                cursor = end
            self.continuous += gap
        elif gap < 0 or gap > cfg.get("max_gap", 120):
            self.continuous, self.due = 0, {}
        changed = name != self.foreground
        if changed:
            self.continuous, self.due = 0, {}
        events = []
        for index, rule in enumerate(cfg.get("rules", [])):
            if not rule.get("enabled", False):
                continue
            exe = rule.get("process", "").lower()
            rid = str(rule.get("id", index))
            if has_snapshot:
                kind = "start" if exe in present - self.running else "exit" if exe in self.running - present else ""
            else:
                kind = "start" if changed and exe == name else ""
            text = rule.get(kind + "_text", "") if kind else ""
            if text:
                events.append(event(f"application:{rid}:{kind}:{now.isoformat()}", "application", text))
            if name != exe or (rule.get("title") and rule["title"].casefold() not in title.casefold()):
                continue
            if not cfg.get("break_enabled", True):
                continue
            minutes = max(1, float(rule.get("minutes", cfg.get("break_minutes", 60))))
            repeat_value = rule.get("repeat", 0)
            repeat = minutes if repeat_value is True else max(0, float(repeat_value))
            used = self.continuous / 60
            count = (1 + int((used - minutes) // repeat) if repeat else 1) if used >= minutes else 0
            if count > self.due.get(rid, 0):
                self.due[rid] = count
                text = rule.get("text") or PhraseBook().pick("rest", {"rest": rule.get("lines")})
                if text:
                    text = text.replace("{app}", exe).replace("{minutes}", str(int(used)))
                    events.append(event(f"application:{rid}:rest:{now.isoformat()}", "application", text))
        self.previous, self.foreground = now, name
        if has_snapshot:
            self.running = present
        return events


class ReminderScheduler:
    def __init__(self, clock=None):
        self.clock = clock or datetime.now
        self.previous = None
        self.intervals = {}
        self.seen = {}

    def check(self, now=None, cfg=None):
        cfg = cfg or {}
        now = self.clock() if now is None else now
        if not cfg.get("enabled", False):
            self.previous, self.intervals = None, {}
            return []
        events = []
        active = set()
        for index, rule in enumerate(cfg.get("rules", [])):
            rid = str(rule.get("id", index))
            if not rule.get("enabled", False):
                self.intervals.pop(rid, None)
                continue
            active.add(rid)
            kind = rule.get("when", "daily")
            due = False
            if kind == "interval":
                last = self.intervals.setdefault(rid, now)
                due = (now - last).total_seconds() >= max(1, float(rule.get("every", 60))) * 60
                if due or now < last:
                    self.intervals[rid] = now
            elif self.previous and now >= self.previous and kind in ("daily", "weekly"):
                try:
                    hour, minute = map(int, rule.get("time", "").split(":"))
                    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                except (ValueError, TypeError):
                    continue
                if candidate > now:
                    candidate -= timedelta(days=1)
                if kind == "weekly":
                    for _ in range(7):
                        if candidate.weekday() in rule.get("days", []):
                            break
                        candidate -= timedelta(days=1)
                    else:
                        continue
                mark = candidate.isoformat()
                due = self.previous < candidate <= now and self.seen.get(rid) != mark
                if due:
                    self.seen[rid] = mark
            if due:
                text = rule.get("text") or PhraseBook().pick("reminder", {"reminder": rule.get("lines", [])})
                if text:
                    events.append(event(f"reminder:{rid}:{now.isoformat()}", "reminder", text))
        self.intervals = {k: v for k, v in self.intervals.items() if k in active}
        self.seen = {k: v for k, v in self.seen.items() if k in active}
        self.previous = now
        if len(events) > 1:
            return [event(f"reminder:summary:{now.isoformat()}", "reminder",
                          "待办提醒：" + "；".join(item["text"] for item in events))]
        return events
