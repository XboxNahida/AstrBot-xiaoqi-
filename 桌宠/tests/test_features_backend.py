"""扩展业务真实分发不调用语言模型，关闭后停止采样。"""
import datetime as dt

import pytest

from pet_features.backend import FeaturesBackend
from pet_features.config import defaults


def test_cancel_during_media_snapshot_never_starts_lyrics(tmp_path):
    backend = FeaturesBackend(tmp_path)
    cfg = defaults()
    cfg["media"].update(enabled=True, lyrics_enabled=True)
    cancelled = [False]
    backend.cancelled = lambda: cancelled[0]
    def snapshot():
        cancelled[0] = True
        return {"available": True, "playing": True, "title": "测试", "artist": "歌手"}
    backend.media.snapshot = snapshot
    backend.media.lyrics = lambda *_args: (_ for _ in ()).throw(AssertionError("取消后不联网"))
    try:
        data = backend.poll(cfg)
        assert not data["status"].get("media", {}).get("error")
        cancelled[0] = False
        backend.perform("media:refresh", cfg)
    finally:
        backend.close()


@pytest.mark.parametrize("module", ["system", "apps", "weather"])
def test_poll_isolates_errors_and_preserves_events(tmp_path, monkeypatch, module):
    import pet_features.backend as impl
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    cfg["system"]["enabled"] = cfg["apps"]["enabled"] = True
    monkeypatch.setattr(impl, "sample_system", lambda **kw: {})
    monkeypatch.setattr(impl, "sample_applications", lambda: {"title": "秘密窗口"})
    calls = []
    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("采样失败")
    if module == "weather":
        backend._poll_weather = broken
    else:
        monkeypatch.setattr(impl, "sample_" + ("applications" if module == "apps" else module), broken)
    backend.reminders.check = lambda *args: [{"id": "提醒"}]
    backend._poll_balance = lambda *args: {"events": [{"id": "余额"}]}
    try:
        out = backend.poll(cfg)
        assert [e["id"] for e in out["events"]] == ["提醒", "余额"]
        assert out["status"][module]["error"]
        assert len(calls) == 1
        if module != "weather":
            backend.poll(cfg)
            assert len(calls) == 1  # 失败仍遵守原采样周期，无立即重试。
    finally:
        backend.close()


def test_backend_restores_local_state_without_closed_app_time(tmp_path):
    backend = FeaturesBackend(tmp_path)
    stamp = dt.datetime(2026, 11, 16, 10)
    backend.apps.daily = {"2026-11-16": {"editor.exe": 30}}
    backend.apps.previous = stamp
    backend.apps.foreground = "editor.exe"
    backend.reminders.previous = stamp
    backend.reminders.seen = {"daily": stamp.isoformat()}
    backend.reminders.intervals = {"water": stamp}
    backend.close()
    restored = FeaturesBackend(tmp_path)
    try:
        assert restored.apps.stats() == {"2026-11-16": {"editor.exe": 30}}
        assert restored.apps.previous is None
        assert restored.reminders.previous == stamp
        assert restored.reminders.seen == {"daily": stamp.isoformat()}
        assert restored.reminders.intervals == {"water": stamp}
    finally:
        restored.close()


def test_cancel_stops_later_sampling_and_preserves_events(tmp_path, monkeypatch):
    import pet_features.backend as impl
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    cfg["system"]["enabled"] = cfg["apps"]["enabled"] = True
    monkeypatch.setattr(impl, "sample_system", lambda **kw: {})
    def first(*args):
        backend.cancelled = lambda: True
        return [{"id": "先完成"}]
    backend.system.check = first
    monkeypatch.setattr(impl, "sample_applications", lambda: pytest.fail("取消后仍采样"))
    try:
        assert backend.poll(cfg)["events"] == [{"id": "先完成"}]
    finally:
        backend.close()


def test_disabled_clears_cursor_but_retains_daily_dedup_persistently(tmp_path, monkeypatch):
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    backend.reminders.previous = dt.datetime(2026, 11, 16, 9)
    backend.reminders.seen = {"rule": "2026-11-16T09:00:00"}
    backend.poll(cfg)
    backend.close()
    restored = FeaturesBackend(tmp_path)
    try:
        assert restored.reminders.previous is None
        assert restored.reminders.seen == {"rule": "2026-11-16T09:00:00"}
    finally:
        restored.close()


def test_reminder_restart_keeps_same_day_dedup_and_interval(tmp_path):
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    cfg["reminders"].update(enabled=True, rules=[
        {"id": "daily", "enabled": True, "when": "daily", "time": "10:00", "text": "提醒"},
        {"id": "water", "enabled": True, "when": "interval", "every": 60, "text": "喝水"}])
    before, now = dt.datetime(2026, 11, 16, 9, 59), dt.datetime(2026, 11, 16, 10)
    backend.reminders.check(before, cfg["reminders"])
    assert backend.reminders.check(now, cfg["reminders"])
    backend.close()
    restored = FeaturesBackend(tmp_path)
    try:
        assert not restored.reminders.check(now + dt.timedelta(seconds=5), cfg["reminders"])
        assert restored.reminders.check(now + dt.timedelta(hours=1), cfg["reminders"])
    finally:
        restored.close()


def test_weather_cancel_skips_remaining_cities(tmp_path):
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    cfg["weather"]["cities"] = [{"name": "甲"}, {"name": "乙"}]
    calls = []
    def sample(city):
        calls.append(city)
        backend.cancelled = lambda: True
        return {"available": False, "error": "离线"}
    backend.weather.current = sample
    try:
        assert "甲" in backend._weather_result(cfg)
        assert len(calls) == 1
    finally:
        backend.close()


def test_backend_feed_weather_and_disabled_poll(tmp_path):
    cfg = defaults()
    backend = FeaturesBackend(tmp_path)
    calls = []
    backend.weather.current = lambda city: calls.append(city) or {"available": True, "stale": False, "data": {"temperature_2m": 20}}
    try:
        result = backend.perform("feed:fish", cfg)
        assert result["events"][0]["manual"]
        assert result["text"]
        assert not backend.poll(cfg)["events"]
        assert not calls
        cfg["weather"]["cities"] = [{"id": 1, "name": "测试城", "latitude": 1, "longitude": 2}]
        result = backend.perform("weather:current", cfg)
        assert "20" in result["text"]
        assert len(calls) == 1
    finally:
        backend.close()


@pytest.mark.parametrize("forecast", [False, True])
def test_weather_cached_current_and_forecast_show_timestamp_and_failed_refresh(tmp_path, forecast):
    backend, cfg = FeaturesBackend(tmp_path), defaults()
    cfg["weather"]["cities"] = [{"id": 1, "name": "测试城"}]
    stamp = dt.datetime(2026, 11, 17, 8, 30).timestamp()
    payload = {"available": True, "stale": True, "updated_at": stamp, "source": "测试源",
               "error": "天气连接超时，请稍后重试。", "data": {
                   "temperature_2m": 20, "hourly": [], "tomorrow_rain": []}}
    backend.weather.current = backend.weather.forecast = lambda _: payload
    try:
        data = backend.perform("weather:forecast" if forecast else "weather:current", cfg)
        assert "缓存数据" in data["text"] and "11-17 08:30" in data["text"]
        assert "本次更新失败" in data["text"] and "连接超时" in data["text"]
        assert data["events"][0]["text"] == data["text"]
        assert data["events"][0]["manual"]
    finally:
        backend.close()
