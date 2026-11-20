from datetime import datetime, timedelta

from pet_features.system_features import (
    ApplicationTracker,
    PhraseBook,
    ReminderScheduler,
    SystemMonitor,
)


def test_monitor_duration_recovery_and_cooldown():
    monitor = SystemMonitor()
    cfg = {"enabled": True}
    assert monitor.check({"cpu": 95}, 0, cfg) == []
    assert monitor.check({"cpu": 95}, 29, cfg) == []
    assert len(monitor.check({"cpu": 95}, 30, cfg)) == 1
    assert monitor.check({"cpu": 95}, 700, cfg) == []
    monitor.check({"cpu": 10}, 701, cfg)
    monitor.check({"cpu": 95}, 702, cfg)
    assert len(monitor.check({"cpu": 95}, 732, cfg)) == 1


def test_disabled_never_samples_and_custom_phrases():
    def forbidden():
        raise AssertionError("关闭时不能采样")
    assert SystemMonitor(sample=forbidden).check(None, 0, {}) == []
    assert PhraseBook(choice=lambda values: values[0]).pick("idle", {"idle": ["你好"]}) == "你好"
    assert PhraseBook().pick("unknown", {}) == ""


def test_foreground_midnight_and_sleep():
    tracker = ApplicationTracker()
    cfg = {"enabled": True, "max_gap": 120, "rules": [
        {"id": "edit", "enabled": True, "process": "edit.exe", "minutes": 1,
         "text": "休息", "start_text": "开始", "exit_text": "结束"}]}
    start = datetime(2026, 11, 16, 23, 59, 30)
    assert tracker.update("edit.exe", "", start, cfg)[0]["text"] == "开始"
    assert tracker.update("edit.exe", "", start + timedelta(seconds=60), cfg)[0]["text"] == "休息"
    stats = tracker.stats()
    assert stats["2026-11-16"]["edit.exe"] == 30
    assert stats["2026-11-17"]["edit.exe"] == 30
    assert tracker.update("other.exe", "", start + timedelta(hours=2), cfg) == []
    assert tracker.stats() == stats


def test_process_snapshot_distinguishes_exit_from_switch():
    tracker = ApplicationTracker()
    cfg = {"enabled": True, "rules": [{"id": "a", "enabled": True,
        "process": "a.exe", "start_text": "启动", "exit_text": "退出"}]}
    now = datetime(2026, 1, 1)
    assert tracker.update({"foreground": "a.exe", "running": ["a.exe"]}, "", now, cfg)
    assert tracker.update({"foreground": "b.exe", "running": ["a.exe", "b.exe"]}, "", now, cfg) == []
    assert tracker.update({"foreground": "b.exe", "running": ["b.exe"]}, "", now, cfg)[0]["text"] == "退出"


def test_reminders_baseline_weekly_interval_resume_summary():
    scheduler = ReminderScheduler()
    now = datetime(2026, 11, 16, 9)
    cfg = {"enabled": True, "rules": [
        {"id": "d", "enabled": True, "when": "daily", "time": "09:01", "text": "喝水"},
        {"id": "w", "enabled": True, "when": "weekly", "days": [0], "time": "09:02", "text": "休息"},
        {"id": "i", "enabled": True, "when": "interval", "every": 1, "text": "活动"}]}
    assert scheduler.check(now, cfg) == []
    events = scheduler.check(now + timedelta(minutes=3), cfg)
    assert len(events) == 1 and all(word in events[0]["text"] for word in ["喝水", "休息", "活动"])
    assert scheduler.check(now + timedelta(minutes=3), cfg) == []
    assert len(scheduler.check(now + timedelta(minutes=4), cfg)) == 1


def test_daily_reminder_toggle_keeps_same_day_dedup_and_repeat_period():
    scheduler = ReminderScheduler()
    now = datetime(2026, 11, 16, 9)
    cfg = {"enabled": True, "rules": [{"id": "a", "enabled": True, "when": "daily", "time": "09:01", "text": "喝水"}]}
    scheduler.check(now, cfg)
    assert scheduler.check(now + timedelta(minutes=1), cfg)
    scheduler.check(now + timedelta(minutes=2), {"enabled": False})
    assert "a" in scheduler.seen
    tracker = ApplicationTracker()
    acfg = {"enabled": True, "max_gap": 10000, "rules": [{"id": "a", "enabled": True, "process": "a.exe", "minutes": 10, "repeat": True, "text": "休息"}]}
    tracker.update("a.exe", now=now, cfg=acfg)
    assert tracker.update("a.exe", now=now + timedelta(minutes=10), cfg=acfg)
    assert not tracker.update("a.exe", now=now + timedelta(minutes=11), cfg=acfg)


def test_windows_sampling_and_trim_are_explicit_and_protected():
    from pet_features.system_windows import sample_system, trim_working_sets
    class Proc:
        def __init__(self, pid, name):
            self.pid = pid
            self.info = {"pid": pid, "name": name}
    class FakePsutil:
        def cpu_percent(self, interval=None):
            assert interval is None
            return 12
        def virtual_memory(self):
            return type("Memory", (), {"percent": 34})()
        def process_iter(self, attrs):
            return [Proc(10, "pet.exe"), Proc(11, "front.exe"),
                    Proc(12, "lsass.exe"), Proc(13, "editor.exe")]
    module = FakePsutil()
    assert sample_system(module)["memory"] == 34
    calls = []
    result = trim_working_sets(module, own_pid=10, foreground=11,
                              trim=lambda pid: calls.append(pid) or True)
    assert calls == [13]
    assert result == {"success": 1, "skipped": 3, "failed": 0}


def test_idle_return_independent_switches_and_cooldown():
    from pet_features.system_features import IdleTracker
    tracker = IdleTracker(phrases=PhraseBook(choice=lambda values: values[0]))
    cfg = {"idle_enabled": True, "return_enabled": True,
           "idle_minutes": 1, "return_minutes": 2, "interval_minutes": 5,
           "items": {"idle": ["闲置"], "return": ["回来"]}}
    assert tracker.check(0, 0, cfg) == []
    assert tracker.check(60, 60, cfg)[0]["text"] == "闲置"
    assert tracker.check(120, 120, cfg) == []
    assert tracker.check(0, 121, cfg)[0]["text"] == "回来"
    assert tracker.check(0, 122, cfg) == []


def test_monitor_metric_switches_and_gap_reset():
    monitor = SystemMonitor()
    cfg = {"enabled": True, "cpu_enabled": False, "sustain_seconds": 2}
    monitor.check({"cpu": 100, "memory": 99}, 0, cfg)
    events = monitor.check({"cpu": 100, "memory": 99}, 2, cfg)
    assert len(events) == 1 and "memory" in events[0]["id"]
    assert set(events[0]) == {"id", "source", "text", "action", "priority", "ttl", "manual"}


def test_disabled_reminders_reenable_without_backfill():
    scheduler = ReminderScheduler()
    rule = {"id": "a", "enabled": True, "when": "interval", "every": 1, "text": "a"}
    now = datetime(2026, 1, 1)
    assert scheduler.check(now, {"enabled": True, "rules": [rule]}) == []
    scheduler.check(now + timedelta(minutes=2), {"enabled": False})
    assert scheduler.check(now + timedelta(hours=4), {"enabled": True, "rules": [rule]}) == []
