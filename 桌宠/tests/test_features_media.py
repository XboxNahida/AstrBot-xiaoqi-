"""媒体与歌词只使用注入的合成接口。"""
from pet_features.media import MediaClient, calibrate_offset, lyric_at, parse_lrc


def test_lrc_multiple_timestamps_word_timing_and_offset():
    rows = parse_lrc("[offset:100]\n[00:01.00][00:03.00]你好\n[00:04.00]<00:04.00>世<00:04.50>界")
    assert [r["time"] for r in rows] == [1.1, 3.1, 4.1]
    assert rows[-1]["words"][-1] == {"time": 4.6, "text": "界"}
    assert lyric_at(rows, 3.3)["text"] == "你好"
    assert lyric_at(rows, 0) is None
    assert calibrate_offset(3.0, 3.4, 100) == 500


def test_winrt_waits_have_timeout_and_do_not_poison_worker():
    import asyncio

    import pytest

    from pet_features.media import _WindowsMedia
    backend = _WindowsMedia.__new__(_WindowsMedia)
    backend._loop = asyncio.new_event_loop()
    backend.timeout = .01
    async def hung(*_args):
        await asyncio.Event().wait()
    backend._snapshot = hung
    backend._command = hung
    try:
        with pytest.raises(TimeoutError):
            backend.snapshot()
        with pytest.raises(TimeoutError):
            backend.command("pause")
        assert not asyncio.all_tasks(backend._loop)
    finally:
        backend.close()


def test_media_unavailable_and_control_validation():
    class Backend:
        def snapshot(self):
            return {"available": True, "title": "歌", "artist": "人", "playing": False,
                    "position": None, "duration": None}
        def command(self, action):
            return action == "play"
        def close(self):
            self.closed = True
    backend = Backend()
    client = MediaClient(backend=backend)
    assert client.snapshot()["position"] is None
    assert client.command("play")["ok"]
    assert not client.command("删除")["ok"]
    client.close()
    assert backend.closed
    assert not client.snapshot()["available"]


def test_lyrics_cache_and_instrumental_and_failure():
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        return {"syncedLyrics": "[00:01]你好", "plainLyrics": "你好", "instrumental": False}
    client = MediaClient(backend=object(), fetch=fetch)
    result = client.lyrics("歌曲", "歌手")
    assert result["available"] and result["lines"][0]["time"] == 1
    assert client.lyrics("歌曲", "歌手") == result
    assert len(calls) == 1
    assert "artist_name=" in calls[0]
    bad = MediaClient(backend=object(), fetch=lambda *_: (_ for _ in ()).throw(OSError("离线")))
    assert not bad.lyrics("歌曲", "歌手")["available"]


def test_winrt_adapter_snapshot_controls_no_session_and_cleanup(monkeypatch):
    import sys
    import types
    from datetime import timedelta

    from pet_features.media import _WindowsMedia
    class Session:
        source_app_user_model_id = "合成播放器"
        async def try_get_media_properties_async(self):
            return types.SimpleNamespace(title="歌", artist="歌手", album_title="专辑")
        def get_timeline_properties(self):
            return types.SimpleNamespace(start_time=timedelta(seconds=2),
                                         end_time=timedelta(seconds=62), position=timedelta(seconds=12))
        def get_playback_info(self):
            return types.SimpleNamespace(playback_status=4)
        async def try_pause_async(self):
            return True
    sessions = [Session()]
    class Manager:
        @classmethod
        async def request_async(cls):
            return cls()
        def get_current_session(self):
            return sessions[0]
    module = types.ModuleType("winrt.windows.media.control")
    module.GlobalSystemMediaTransportControlsSessionManager = Manager
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", module)
    backend = _WindowsMedia()
    client = MediaClient(backend=backend)
    assert client.snapshot()["position"] == 10
    assert client.snapshot()["playing"]
    assert client.command("pause")["ok"]
    sessions[0] = None
    assert not client.snapshot()["available"]
    assert not client.command("pause")["ok"]
    client.close()
    assert backend._loop.is_closed()
