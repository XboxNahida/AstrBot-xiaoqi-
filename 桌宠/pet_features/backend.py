"""串行后台业务分发，Qt主线程只接收纯数据。"""
import datetime as dt
import json
import time
from contextlib import contextmanager
from pathlib import Path

from .backend_state import BackendState
from .events import feed_event
from .media import MediaClient, lyric_at
from .system_features import (
    ApplicationTracker,
    IdleTracker,
    PhraseBook,
    ReminderScheduler,
    SystemMonitor,
)
from .system_windows import (
    idle_seconds,
    sample_applications,
    sample_system,
    trim_working_sets,
)
from .weather import WeatherClient


def result(text="", events=None, **extra):
    return dict(text=text, events=events or [], **extra)


class _BaseBackend:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.weather, self.media = WeatherClient(), MediaClient()
        self.system, self.apps = SystemMonitor(), ApplicationTracker()
        self.reminders, self.idle = ReminderScheduler(), IdleTracker()
        self.last = {}
        self.last_song = None
        self.last_lyric = ""
        self.snapshot = {}
        self.log_reader = None
        self.usage = None
        self.account = None
        self.local_state = BackendState(self.root / "features_state.sqlite")
        self.local_state.restore(self.apps, self.reminders)
        self.closed = False
        self.cancelled = lambda: False

    @contextmanager
    def _isolate(self, key, status):
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - 后台插件故障边界，保留其他模块结果。
            # 不暴露网络凭据/窗口标题等异常正文，不重试，不丢掉其他模块事件。
            status[key] = {"available": False, "error": "模块执行失败：" + type(exc).__name__}

    def _due(self, key, seconds, now):
        if now - self.last.get(key, float("-inf")) < seconds:
            return False
        self.last[key] = now
        return True

    def poll(self, cfg):
        now = time.time()
        events, status = [], {}
        if self.cancelled():
            return self._finish_poll(events, status)
        with self._isolate("system", status):
            if cfg["system"]["enabled"] and self._due("system", max(1, cfg["system"]["sample_seconds"]), now):
                data = sample_system(cfg=cfg["system"])
                status["system"] = data
                events += self.system.check(data, now, cfg["system"])
            if not cfg["system"]["enabled"]:
                self.system.check(cfg=cfg["system"])
        if self.cancelled():
            return self._finish_poll(events, status)
        with self._isolate("apps", status):
            if cfg["apps"]["enabled"] and self._due("apps", 5, now):
                data = sample_applications()
                baseline = self.apps.previous is None
                new = self.apps.update(data, data["title"], dt.datetime.now(), cfg["apps"])
                events += [] if baseline else new
            if not cfg["apps"]["enabled"]:
                self.apps.update("", cfg=cfg["apps"])
        if self.cancelled():
            return self._finish_poll(events, status)
        with self._isolate("reminders", status):
            events += self.reminders.check(dt.datetime.now(), cfg["reminders"])
        if self.cancelled():
            return self._finish_poll(events, status)
        with self._isolate("phrases", status):
            if not cfg["phrases"]["enabled"]:
                self.idle.check(cfg={})
            if cfg["phrases"]["enabled"] and self._due("phrases", 5, now):
                pcfg = dict(cfg["phrases"])
                pcfg["items"] = self._phrases(pcfg["items"])
                events += self.idle.check(idle_seconds(), now, pcfg)
        if self.cancelled():
            return self._finish_poll(events, status)
        with self._isolate("media", status):
            if cfg["media"]["enabled"] and self._due("media", 2, now):
                data = self.media.snapshot()
                status["media"] = data
                song = (data.get("title"), data.get("artist")) if data.get("playing") else None
                if song and song != self.last_song:
                    events.append(self._event("media", f"正在播放：{song[0]} · {song[1]}", "sing" if cfg["media"]["animation_enabled"] else ""))
                self.last_song = song
                if song and cfg["media"]["lyrics_enabled"] and not self.cancelled():
                    lyrics = self.media.lyrics(*song)
                    status["lyric_lines"] = lyrics.get("lines", [])
                    row = lyric_at(lyrics.get("lines", []), data.get("position"), cfg["media"]["offset_seconds"])
                    status["lyrics"] = row
                    if row and row["text"] != self.last_lyric and cfg["media"]["show_lyrics"]:
                        events.append(self._event("media", row["text"], ""))
                        self.last_lyric = row["text"]
        for key in ("weather", "logs", "balance"):
            if self.cancelled():
                break
            handler = getattr(self, "_poll_" + key, None)
            if handler:
                with self._isolate(key, status):
                    extra = handler(cfg, now)
                    events += extra.get("events", [])
                    status.update(extra.get("status", {}))
        return self._finish_poll(events, status)

    def _finish_poll(self, events, status):
        with self._isolate("state", status):
            self.local_state.save(self.apps, self.reminders)
        self.snapshot.update(status)
        return result(events=events, status=status)

    @staticmethod
    def _phrases(items):
        output = {}
        for item in items:
            if item.get("enabled", True):
                output.setdefault(item.get("category", "idle"), []).append(item.get("text", ""))
        return output

    @staticmethod
    def _event(source, text, action="reminder", manual=False):
        return {"id": source + ":" + str(time.time_ns()), "source": source, "text": text, "action": action,
                    "priority": 40, "ttl": 60, "manual": manual, "created": time.time()}

    def _weather_result(self, cfg, forecast=False):
        cities = [c for c in cfg["weather"]["cities"] if c.get("enabled", True)]
        if not cities:
            raise ValueError("请先搜索并保存城市")
        rows = []
        self.weather.cache_seconds = cfg["weather"]["cache_seconds"]
        for city in cities:
            if self.cancelled():
                break
            data = self.weather.forecast(city) if forecast else self.weather.current(city)
            if not data["available"]:
                rows.append(city["name"] + "：" + data.get("error", "暂不可用"))
            elif forecast:
                hourly = data["data"]["hourly"]
                future = [h for h in hourly if h["time"] >= dt.datetime.now().strftime("%Y-%m-%dT%H:%M")][:8]
                rain = data["data"]["tomorrow_rain"]
                rows.append(city["name"] + "分时天气：\n" + "\n".join(f"{h['time']} {h['temperature']}℃ 降雨{h['probability']}%" for h in future)
                            + ("\n明日可能降雨，请留意带伞。" if rain else "\n明日暂无明显降雨。"))
            else:
                w = data["data"]
                rows.append(f"{city['name']}：{w.get('description', '')} {w.get('temperature_2m', '未知')}℃，"
                    + (f"体感{w['apparent_temperature']}℃，" if w.get('apparent_temperature') is not None else "")
                    + f"湿度{w.get('relative_humidity_2m', '未知')}%。{w.get('wind_description', '')}"
                    + f"（{data.get('source', '天气服务')} {w.get('observed_time', '')}）")
            if data["available"] and data.get("stale"):
                stamp = data.get("updated_at")
                fetched = dt.datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M") if stamp is not None else "未知时间"
                rows[-1] = f"【缓存数据，上次获取：{fetched}】\n" + rows[-1] + "\n本次更新失败：" + data.get("error", "天气服务暂不可用")
        return "\n".join(rows)

    def _poll_weather(self, cfg, now):
        c = cfg["weather"]
        if not c["enabled"] or not c["cities"]:
            return {}
        stamp = dt.datetime.now()
        if c["report_enabled"] and stamp.strftime("%H:%M") == c["report_time"] and self.last.get("weather_day") != stamp.date():
            self.last["weather_day"] = stamp.date()
            return result(events=[self._event("weather", self._weather_result(cfg, c["rain_enabled"]), "weather")])
        return {}

    def perform(self, action, cfg, argument=None):
        if action.startswith("feed:"):
            if not cfg["feeding"]["enabled"]:
                return result("喂食互动已关闭")
            event = feed_event(action.split(":", 1)[1])
            return result(event["text"], [event])
        if action == "weather:search":
            return result(cities=self.weather.search(argument))
        if action == "weather:locate":
            return result(cities=[self.weather.locate()])
        if action in ("weather:current", "weather:forecast"):
            text = self._weather_result(cfg, action.endswith("forecast"))
            return result(text, [self._event("weather", text, "weather", True)])
        if action == "system:sample":
            data = sample_system(cfg=cfg["system"])
            return result("系统状态：" + json.dumps(data, ensure_ascii=False))
        if action == "apps:stats":
            return result("每日前台使用秒数：\n" + json.dumps(self.apps.stats(), ensure_ascii=False, indent=2))
        if action == "reminders:preview":
            return result("提醒预览", [self._event("reminders", "这是一条本地提醒预览。", "reminder", True)])
        if action == "phrases:preview":
            text = PhraseBook().pick("idle", self._phrases(cfg["phrases"]["items"]))
            return result(text, [self._event("phrases", text, "idle", True)])
        if action == "tools:trim":
            return result("工作集回收：" + json.dumps(trim_working_sets(), ensure_ascii=False))
        if action.startswith("media:"):
            return self._media_action(action, cfg)
        handler = getattr(self, "extended_action", None)
        if handler:
            return handler(action, cfg, argument)
        raise ValueError("该操作尚未装配：" + action)

    def _media_action(self, action, cfg):
        if action == "media:refresh":
            data = self.media.snapshot()
            text = json.dumps(data, ensure_ascii=False, indent=2)
            if cfg["media"]["lyrics_enabled"] and data.get("available") and not self.cancelled():
                lyrics = self.media.lyrics(data["title"], data["artist"])
                text += "\n" + (lyrics.get("plain") or "\n".join(i["text"] for i in lyrics.get("lines", [])))
            return result(text)
        data = self.media.command(action.split(":")[1])
        return result("媒体操作完成" if data.get("ok") else data.get("error", "媒体操作不可用"))

    def close(self):
        if self.closed:
            return
        self.local_state.save(self.apps, self.reminders)
        self.local_state.close()
        self.closed = True
        self.media.close()
        self.close_extended()
        for name in ("log_reader", "usage", "account"):
            obj = getattr(self, name, None)
            if obj and hasattr(obj, "close"):
                obj.close()


# 延迟组合避免扩展模块与基础result工厂的导入环。
from .backend_extended import ExtendedBackend


class FeaturesBackend(ExtendedBackend, _BaseBackend):
    pass
