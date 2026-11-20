"""Windows系统媒体及LRCLIB歌词；开关与后台调度由调用方负责。"""
import asyncio
import copy
import re
from urllib.parse import urlencode

from .weather import _fetch_json

_STAMP = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_WORD = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>([^<]*)")


def parse_lrc(text, offset_ms=0):
    """解析标准/增强LRC；时间单位秒，正偏移表示延后显示。"""
    match = re.search(r"\[offset:([+-]?\d+)\]", text or "", re.IGNORECASE)
    offset = (offset_ms + (int(match[1]) if match else 0)) / 1000
    rows = []
    for line in (text or "").splitlines():
        stamps = list(_STAMP.finditer(line))
        if not stamps:
            continue
        content = _STAMP.sub("", line).strip()
        words = [{"time": int(m[1]) * 60 + float(m[2]) + offset, "text": m[3]}
                 for m in _WORD.finditer(content)]
        plain = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", content)
        for stamp in stamps:
            rows.append({"time": int(stamp[1]) * 60 + float(stamp[2]) + offset,
                         "text": plain, "words": words})
    return sorted(rows, key=lambda row: row["time"])


def lyric_at(lines, position, offset_seconds=0):
    if position is None:
        return None
    return next((line for line in reversed(lines)
                 if line["time"] + offset_seconds <= position), None)


def calibrate_offset(line_seconds, observed_seconds, old_offset_ms=0):
    return round(old_offset_ms + (observed_seconds - line_seconds) * 1000)


class _WindowsMedia:
    """同一后台线程持有自己的事件循环，不创建常驻线程。"""
    def __init__(self, timeout=5):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager,
        )
        self._manager_type = GlobalSystemMediaTransportControlsSessionManager
        self._loop = asyncio.new_event_loop()
        self._manager = None
        self.timeout = timeout

    async def _session(self):
        if self._manager is None:
            self._manager = await self._manager_type.request_async()
        return self._manager.get_current_session()

    async def _snapshot(self):
        session = await self._session()
        if session is None:
            return {"available": False, "error": "没有系统媒体会话"}
        props = await session.try_get_media_properties_async()
        timeline = session.get_timeline_properties()
        playback = session.get_playback_info()
        duration = (timeline.end_time - timeline.start_time).total_seconds()
        position = (timeline.position - timeline.start_time).total_seconds()
        status = playback.playback_status
        # WinRT枚举规范：Playing=4；不靠枚举字符串猜测。
        return {"available": True, "title": props.title, "artist": props.artist,
                "album": props.album_title, "source": session.source_app_user_model_id,
                "playing": int(status) == 4,
                "position": position if duration > 0 else None,
                "duration": duration if duration > 0 else None, "error": ""}

    def snapshot(self):
        return self._loop.run_until_complete(asyncio.wait_for(self._snapshot(), self.timeout))

    async def _command(self, action):
        session = await self._session()
        if session is None:
            return False
        method = {"play": "try_play_async", "pause": "try_pause_async",
                  "toggle": "try_toggle_play_pause_async", "next": "try_skip_next_async",
                  "previous": "try_skip_previous_async"}[action]
        return bool(await getattr(session, method)())

    def command(self, action):
        return self._loop.run_until_complete(asyncio.wait_for(self._command(action), self.timeout))

    def close(self):
        self._manager = None
        self._loop.close()


class MediaClient:
    def __init__(self, *, backend=None, fetch=None, timeout=8):
        self._backend = backend
        self.fetch = fetch or _fetch_json
        self.timeout = timeout
        self._cache = {}
        self._closed = False

    def _get_backend(self):
        if self._closed:
            raise RuntimeError("媒体客户端已关闭")
        if self._backend is None:
            self._backend = _WindowsMedia()
        return self._backend

    def snapshot(self):
        try:
            return self._get_backend().snapshot()
        except (ImportError, OSError, RuntimeError, ValueError, AttributeError) as exc:
            return {"available": False, "title": "", "artist": "", "playing": False,
                    "position": None, "duration": None, "error": str(exc)}

    def command(self, action):
        if action not in {"play", "pause", "toggle", "next", "previous"}:
            return {"ok": False, "error": "不支持的媒体操作"}
        try:
            ok = bool(self._get_backend().command(action))
            return {"ok": ok, "error": "" if ok else "媒体会话不支持此操作或没有活动会话"}
        except (ImportError, OSError, RuntimeError, ValueError, AttributeError) as exc:
            return {"ok": False, "error": str(exc)}

    def lyrics(self, title, artist):
        key = (str(title).strip(), str(artist).strip())
        if not key[0]:
            return {"available": False, "lines": [], "error": "歌曲名为空"}
        if key in self._cache:
            return copy.deepcopy(self._cache[key])
        try:
            params = urlencode({"track_name": key[0], "artist_name": key[1]})
            raw = self.fetch("https://lrclib.net/api/get?" + params, self.timeout)
            synced = raw.get("syncedLyrics") or ""
            plain = raw.get("plainLyrics") or ""
            instrumental = bool(raw.get("instrumental"))
            result = {"available": bool(synced or plain or instrumental), "lines": parse_lrc(synced),
                      "plain": plain, "instrumental": instrumental, "error": ""}
            if not result["available"]:
                result["error"] = "未找到歌词"
            else:
                if len(self._cache) >= 128:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[key] = result
            return copy.deepcopy(result)
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            return {"available": False, "lines": [], "plain": "", "instrumental": False, "error": str(exc)}

    def close(self):
        self._closed = True
        if self._backend is not None and hasattr(self._backend, "close"):
            self._backend.close()
        self._cache.clear()
