"""无模型调用的消抖、去重、持久计数及免打扰状态机。"""

import datetime
import json
import logging
import re
import sqlite3
import threading
import uuid
from functools import wraps
from pathlib import Path


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def scene_key(info, normalize=True):
    title = info.get("title", "").casefold()
    if normalize:
        title = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b|[（(]\d+[)）]|\b\d{1,3}%", "", title)
        title = " ".join(title.split())
    return (info.get("process", "").casefold(), info.get("scene", ""), title)


def dnd_active(cfg, hhmm=None):
    if cfg["dnd_enabled"]:
        return True
    if not cfg["dnd_schedule_enabled"]:
        return False
    hhmm = hhmm or datetime.datetime.now().strftime("%H:%M")
    start, end = cfg["dnd_start"], cfg["dnd_end"]
    return start <= hhmm < end if start < end else hhmm >= start or hhmm < end


class SmartPolicy:
    def __init__(self, cfg, path):
        self.lock = threading.RLock()
        self.cfg, self.path = cfg, Path(path)
        self.current, self.since, self.consumed = None, 0, False
        self.last_call, self.failed_at = 0, 0
        self.state = {"calls": [], "tokens": 0, "unknown_usage": 0, "day": "", "scenes": {},
                      "asr_calls": 0, "asr_seconds": 0, "asr_filtered": 0,
                      "input_tokens": 0, "output_tokens": 0, "requests_by_kind": {}, "last_request": {}}
        if self.path.exists():
            self.state.update(json.loads(self.path.read_text(encoding="utf-8")))
            self.last_call = self.state.get("last_auto_call", max(self.state["calls"], default=0))
            self.failed_at = self.state.get("failed_at", 0)

    def _day(self, now):
        day = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if self.state["day"] != day:
            self.state.update(day=day, tokens=0, unknown_usage=0, asr_calls=0, asr_seconds=0, asr_filtered=0,
                              input_tokens=0, output_tokens=0, requests_by_kind={})
        self.state["calls"] = [t for t in self.state["calls"] if now - t < 86400]

    @_locked
    def save(self):
        self.state["failed_at"] = self.failed_at
        self.state["last_auto_call"] = self.last_call
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    @_locked
    def budget(self, now):
        self._day(now)
        cfg = self.cfg
        calls = self.state["calls"]
        if cfg["count_limit_enabled"]:
            if sum(0 <= now - t < 3600 for t in calls) >= cfg["hourly_limit"]:
                return False, "达到每小时模型调用上限"
            today = datetime.datetime.fromtimestamp(now).date()
            if sum(datetime.datetime.fromtimestamp(t).date() == today for t in calls) >= cfg["daily_limit"]:
                return False, "达到今日模型调用上限"
        if cfg["token_limit_enabled"]:
            if self.state["unknown_usage"]:
                return False, "服务未返回usage，Token预算保护已暂停模型调用"
            reserve = cfg["max_output_tokens"] if cfg["short_output"] else 4096
            if self.state["tokens"] + reserve > cfg["daily_tokens"]:
                return False, "达到今日Token预算（含输出预留）"
        return True, ""

    @_locked
    def observe(self, info, now):
        key = scene_key(info, self.cfg["normalize_title"])
        if key != self.current:
            self.current, self.since, self.consumed = key, now, False
        if not info.get("title") or info.get("is_self"):
            return False, "等待有效前台窗口"
        if self.consumed:
            return False, "当前状态已处理，等待新变化"
        if self.cfg["debounce_enabled"] and now - self.since < self.cfg["debounce_seconds"]:
            return False, "状态停留消抖中"
        return self.eligible(info, now)

    @_locked
    def eligible(self, info, now):
        cfg = self.cfg
        if dnd_active(cfg):
            return False, "免打扰中"
        key = "\n".join(scene_key(info, cfg["normalize_title"]))
        if cfg["dedup_enabled"] and now - self.state["scenes"].get(key, -1e12) < cfg["dedup_minutes"] * 60:
            return False, "20分钟场景去重中" if cfg["dedup_minutes"] == 20 else "场景去重中"
        if cfg["cooldown_enabled"] and self.last_call and now - self.last_call < cfg["cooldown_seconds"]:
            return False, "自主调用最小间隔未到"
        if cfg["backoff_enabled"] and self.failed_at and now - self.failed_at < cfg["backoff_minutes"] * 60:
            return False, "失败退避中"
        return self.budget(now)

    @_locked
    def reserve(self, info, now, *, kind="passive", check_budget=False):
        self._day(now)
        if check_budget:
            allowed, reason = self.budget(now)
            if not allowed:
                raise ValueError(reason)
        self.state["calls"].append(now)
        counts = self.state["requests_by_kind"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind in ("passive", "icebreaker"):
            self.last_call, self.consumed = now, True
            self.state["last_auto_call"] = now
            key = "\n".join(scene_key(info, self.cfg["normalize_title"]))
            self.state["scenes"][key] = now
            self.state["scenes"] = {k: v for k, v in self.state["scenes"].items() if now - v < 86400}
        self.save()

    def _record_completion(self, result, now):
        """完成即落盘；只保留数字与模型身份，不保存请求内容。"""
        try:
            values = {key: max(0, result[key]) for key in
                      ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
                      if type(result.get(key)) is int}
            values["usage_known"] = result.get("usage_known") is True
            for key in ("model", "provider"):
                value = result.get(key)
                values[key] = value[:200] if isinstance(value, str) else ""
            with sqlite3.connect(str(self.path.parent / "features_model_usage.sqlite"), timeout=10) as db:
                db.execute("CREATE TABLE IF NOT EXISTS completions (seq INTEGER PRIMARY KEY, "
                           "request_id TEXT UNIQUE, created REAL, data TEXT)")
                db.execute("INSERT INTO completions(request_id,created,data) VALUES (?,?,?)",
                           (uuid.uuid4().hex, now, json.dumps(values)))
        except (OSError, sqlite3.Error, ValueError, TypeError):
            # 可选账本失败不得改变模型结果或原预算保护。
            logging.getLogger(__name__).warning("模型数字用量账本暂不可写，本次原预算与回复继续")

    @_locked
    def complete(self, result, success, now):
        self._day(now)
        if result.get("usage_known"):
            self.state["tokens"] += max(0, int(result.get("total_tokens", 0)))
        else:
            self.state["unknown_usage"] += 1
        for key in ("input_tokens", "output_tokens"):
            if type(result.get(key)) is int:
                self.state[key] += max(0, result[key])
        details = result.get("request_info", {})
        last = {key: details[key] for key in ("system_chars", "prompt_chars", "context_chars", "user_chars", "image_base64_chars", "has_image")
                if type(details.get(key)) in (int, bool)}
        if details.get("kind") in ("manual", "user", "passive", "icebreaker", "summary"):
            last["kind"] = details["kind"]
        last.update({key: result[key] for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "usage_known")
                     if type(result.get(key)) in (int, bool)})
        if result.get("finish_reason") in ("stop", "length", "content_filter", "tool_calls", "STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER"):
            last["finish_reason"] = result["finish_reason"]
        self.state["last_request"] = last
        if not success:
            self.failed_at = now
        self.save()
        self._record_completion(result, now)

    @_locked
    def asr_allow(self, seconds, now):
        self._day(now)
        return not (self.cfg["asr_limit_enabled"] and (
            self.state["asr_calls"] >= self.cfg["asr_daily_count"]
            or self.state["asr_seconds"] + seconds > self.cfg["asr_daily_seconds"]
        ))

    @_locked
    def record_asr(self, seconds, now, filtered=False):
        self._day(now)
        if filtered:
            self.state["asr_filtered"] += 1
        else:
            self.state["asr_calls"] += 1
            self.state["asr_seconds"] += seconds
        self.save()
