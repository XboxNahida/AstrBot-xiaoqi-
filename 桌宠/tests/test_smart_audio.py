"""假合成、假设备与假播放器，禁止访问网络和实际发声。"""

import threading
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication

from smart_config import SmartSettingsStore
from tts.edge_speaker import EdgeSpeaker

_APP = QApplication.instance() or QApplication([])


class FakeOutput:
    def __init__(self):
        self.rows = [{"id": "aa", "name": "耳机", "default": True, "available": True}]
        self.played = []
        self.stops = 0

    def devices(self):
        assert QThread.currentThread() == _APP.thread()
        return self.rows

    def play(self, path, device_id, volume):
        assert QThread.currentThread() == _APP.thread()
        self.played.append((Path(path).read_bytes(), device_id, volume))

    def stop(self):
        assert QThread.currentThread() == _APP.thread()
        self.stops += 1


def spin(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from smart_audio import SmartSpeaker

    original = EdgeSpeaker(enabled=True)
    original._temp_dir = str(tmp_path)
    generated = []
    monkeypatch.setattr(original, "_stop_playback", lambda: None)
    monkeypatch.setattr(original, "_play_audio", lambda path: pytest.fail("禁止原MCI双播"))

    def generate(text, path):
        generated.append(text)
        Path(path).write_bytes(text.encode("utf-8"))

    monkeypatch.setattr(original, "_generate_edge_tts", generate)
    backend = FakeOutput()
    cfg = SmartSettingsStore.defaults()
    speaker = SmartSpeaker(original, cfg, tmp_path, backend=backend)
    yield speaker, original, cfg, backend, generated
    speaker.shutdown()
    spin(lambda: not speaker.worker.is_alive())


@pytest.mark.parametrize("source", ["user", "manual", "passive", "icebreaker", "greeting", "cloud", "reminder"])
@pytest.mark.parametrize("probability", [0, 100])
def test_probability_once_per_source(rig, source, probability):
    speaker, original, cfg, backend, generated = rig
    cfg["voice_sources"][source]["probability"] = probability
    speaker.apply_config(cfg)
    speaker.set_source(source)
    speaker.speak_async("保留完整气泡，独立抽签")
    if probability:
        assert spin(lambda: bool(backend.played))
        assert generated == ["保留完整气泡，独立抽签"]
    else:
        assert not spin(lambda: bool(generated), 0.05)
    assert original.enabled


def test_filter_denies_empty_missing_or_failed_device_and_allows_selected_default(rig):
    speaker, _, cfg, backend, generated = rig
    cfg["device_filter_enabled"] = True
    speaker.apply_config(cfg)
    speaker.speak_async("未选择")
    cfg["allowed_devices"] = ["bb"]
    speaker.apply_config(cfg)
    speaker.speak_async("默认不匹配")
    assert not spin(lambda: bool(generated), 0.05)
    cfg["allowed_devices"] = ["aa"]
    speaker.apply_config(cfg)
    speaker.speak_async("匹配")
    assert spin(lambda: bool(backend.played))
    assert backend.played == [("匹配".encode(), "aa", 70)]


def test_mute_cancels_inflight_result_even_if_reenabled(rig, monkeypatch):
    speaker, original, _, backend, _ = rig
    entered, release = threading.Event(), threading.Event()

    def slow(text, path):
        entered.set()
        assert release.wait(3)
        Path(path).write_bytes(b"old")

    monkeypatch.setattr(original, "_generate_edge_tts", slow)
    speaker.speak_async("旧合成")
    assert spin(entered.is_set)
    speaker.enabled = False
    speaker.enabled = True
    release.set()
    assert not spin(lambda: bool(backend.played), 0.1)


def test_default_change_discards_inflight_and_does_not_fallback(rig, monkeypatch):
    speaker, original, cfg, backend, _ = rig
    cfg.update(device_filter_enabled=True, allowed_devices=["aa"])
    speaker.apply_config(cfg)
    entered, release = threading.Event(), threading.Event()

    def slow(text, path):
        entered.set()
        assert release.wait(3)
        Path(path).write_bytes(b"old")

    monkeypatch.setattr(original, "_generate_edge_tts", slow)
    speaker.speak_async("拔耳机前")
    assert spin(entered.is_set)
    backend.rows = [{"id": "bb", "name": "扬声器", "default": True, "available": True}]
    speaker.devices_changed()
    release.set()
    assert not spin(lambda: bool(backend.played), 0.1)
    speaker.speak_async("拔耳机后")
    assert not spin(lambda: bool(backend.played), 0.05)


def test_rules_switch_and_blocker_do_not_override_original_mute(rig):
    speaker, _, cfg, backend, _ = rig
    cfg["voice_sources"]["user"]["probability"] = 0
    cfg["voice_rules_enabled"] = False
    speaker.apply_config(cfg)
    speaker.set_blocker(lambda: True)
    speaker.speak_async("免打扰")
    assert not spin(lambda: bool(backend.played), 0.03)
    speaker.set_blocker(lambda: False)
    speaker.speak_async("规则关闭")
    assert spin(lambda: bool(backend.played))
    speaker.update_config(enabled=False)
    speaker.speak_async("总静音")
    assert not speaker.enabled
    assert len(backend.played) == 1


def test_cache_reuses_synthesis_and_voice_change_invalidates(rig):
    speaker, _, _, backend, generated = rig
    speaker.speak_async("重复文字")
    assert spin(lambda: len(backend.played) == 1)
    assert spin(lambda: speaker._active is None)
    speaker.speak_async("重复文字")
    assert spin(lambda: len(backend.played) == 2)
    assert generated == ["重复文字"]
    speaker.update_config(voice="zh-CN-XiaoxiaoNeural")
    speaker.speak_async("重复文字")
    assert spin(lambda: len(backend.played) == 3)
    assert generated == ["重复文字", "重复文字"]


def test_clean_short_speech_does_not_modify_original_text(rig):
    speaker, _, cfg, backend, generated = rig
    cfg.update(short_speech=True, speech_chars=20)
    speaker.apply_config(cfg)
    original = "[happy] 看看[说明](https://example.com)，https://example.com，" + "完整文字" * 20
    speaker.speak_async(original)
    assert spin(lambda: bool(backend.played))
    assert len(generated[0]) <= 20
    assert "happy" not in generated[0] and "https" not in generated[0]
    assert original.endswith("完整文字" * 20)


def test_user_interrupts_autonomous_without_accumulating_jobs(rig, monkeypatch):
    speaker, original, cfg, backend, generated = rig
    cfg["voice_sources"]["passive"]["probability"] = 100
    speaker.apply_config(cfg)
    entered, release = threading.Event(), threading.Event()

    def slow(text, path):
        generated.append(text)
        if text == "第一条自主":
            entered.set()
            assert release.wait(3)
        Path(path).write_bytes(text.encode())

    monkeypatch.setattr(original, "_generate_edge_tts", slow)
    speaker.set_source("passive")
    speaker.speak_async("第一条自主")
    assert spin(entered.is_set)
    speaker.set_source("passive")
    speaker.speak_async("不积压自主")
    speaker.set_source("user")
    speaker.speak_async("用户主动")
    release.set()
    assert spin(lambda: bool(backend.played))
    assert generated == ["第一条自主", "用户主动"]
    assert backend.played == [("用户主动".encode(), "aa", 70)]


def test_pre_synthesis_and_pre_playback_recheck_device(rig):
    speaker, _, cfg, backend, generated = rig
    cfg.update(device_filter_enabled=True, allowed_devices=["aa"])
    speaker.apply_config(cfg)
    speaker.speak_async("提交后设备消失")
    backend.rows = []
    assert not spin(lambda: bool(generated), 0.08)
    assert backend.played == []


def test_qt_device_ids_and_explicit_output_no_actual_play(monkeypatch, tmp_path):
    import smart_audio

    class Device:
        def __init__(self, key):
            self.key = key

        def id(self):
            return self.key

        def description(self):
            return "测试端点"

        def isNull(self):
            return False

    device = Device(b"\x00\xab\xff")
    monkeypatch.setattr(smart_audio.QMediaDevices, "audioOutputs", lambda: [device])
    monkeypatch.setattr(smart_audio.QMediaDevices, "defaultAudioOutput", lambda: device)
    assert smart_audio.audio_devices() == [{"id": "00abff", "name": "测试端点", "default": True, "available": True}]
    calls = []

    class Output:
        def __init__(self, selected, parent):
            calls.append(("construct_device", selected.id()))

        def setDevice(self, selected):
            calls.append(("device", selected.id()))

        def setVolume(self, volume):
            calls.append(("volume", volume))

    class Player:
        def __init__(self, parent):
            pass

        def setAudioOutput(self, output):
            calls.append(("output", output))

        def setSource(self, source):
            calls.append(("source", source.toLocalFile()))

        def play(self):
            calls.append(("play",))

        def stop(self):
            calls.append(("stop",))

    monkeypatch.setattr(smart_audio, "QMediaPlayer", Player)
    monkeypatch.setattr(smart_audio, "QAudioOutput", Output)
    backend = smart_audio.QtOutput()
    backend.play(str(tmp_path / "fake.mp3"), "00abff", 33)
    assert ("construct_device", b"\x00\xab\xff") in calls
    assert ("device", b"\x00\xab\xff") in calls
    assert ("volume", 0.33) in calls
    assert calls[-1] == ("play",)
    monkeypatch.setattr(smart_audio.QMediaDevices, "defaultAudioOutput", lambda: Device(b"other"))
    with pytest.raises(RuntimeError):
        backend.play("fake.mp3", "00abff", 33)


def test_background_submission_cancelled_before_main_queue_consumed(rig):
    speaker, _, _, backend, generated = rig
    thread = threading.Thread(target=speaker.speak_async, args=("旧线程排队消息",))
    thread.start()
    thread.join(1)
    speaker.enabled = False
    speaker.enabled = True
    assert not spin(lambda: bool(generated), 0.08)
    assert backend.played == []


def test_device_poll_and_blocker_stop_current_playback(rig):
    speaker, _, _, backend, _ = rig
    speaker.speak_async("播放中")
    assert spin(lambda: bool(backend.played))
    old = backend.stops
    backend.rows = []
    assert spin(lambda: backend.stops > old)
    assert not speaker._playing


def test_audio_device_enumeration_off_thread_fails_closed():
    from smart_audio import audio_devices

    results = []
    thread = threading.Thread(target=lambda: results.append(audio_devices()))
    thread.start()
    thread.join(1)
    assert results == [[]]


def test_cache_is_persistent_and_bounded(rig, tmp_path):
    speaker, _, _, backend, generated = rig
    cache = tmp_path / "voice_cache"
    cache.mkdir()
    for index in range(105):
        (cache / f"old-{index}.mp3").write_bytes(b"old")
    speaker.speak_async("持久缓存")
    assert spin(lambda: len(backend.played) == 1)
    assert len(list(cache.glob("*.mp3"))) == 100
    assert spin(lambda: speaker._active is None)
    speaker.speak_async("持久缓存")
    assert spin(lambda: len(backend.played) == 2)
    assert generated == ["持久缓存"]


def test_user_interrupt_stops_playing_autonomous_before_new_synthesis(rig, monkeypatch):
    speaker, original, cfg, backend, _ = rig
    cfg["voice_sources"]["passive"]["probability"] = 100
    speaker.apply_config(cfg)
    speaker.set_source("passive")
    speaker.speak_async("自主播放中")
    assert spin(lambda: bool(backend.played))
    assert spin(lambda: speaker._active is None)
    old = backend.stops
    entered, release = threading.Event(), threading.Event()

    def slow(text, path):
        entered.set()
        assert release.wait(3)
        Path(path).write_bytes(b"new")

    monkeypatch.setattr(original, "_generate_edge_tts", slow)
    speaker.speak_async("用户打断")
    try:
        assert backend.stops > old
        assert spin(entered.is_set)
    finally:
        release.set()


def test_probability_drawn_once_and_playback_recheck_does_not_redraw(rig):
    speaker, _, cfg, backend, _ = rig
    draws = []
    speaker._random = lambda: draws.append(1) or 0.1
    cfg["voice_sources"]["user"]["probability"] = 50
    speaker.apply_config(cfg)
    speaker.speak_async("一次抽签")
    assert spin(lambda: bool(backend.played))
    assert draws == [1]


def test_queued_audio_result_cannot_play_after_device_change_or_stop(rig, tmp_path):
    speaker, _, cfg, backend, _ = rig
    cfg.update(device_filter_enabled=True, allowed_devices=["aa"])
    speaker.apply_config(cfg)
    audio = tmp_path / "queued.mp3"
    audio.write_bytes(b"old")
    job = {"device": "aa", "generation": speaker._generation}
    speaker._ready.emit((job, str(audio)))
    speaker.stop()
    assert not spin(lambda: bool(backend.played), 0.05)
    job["generation"] = speaker._generation
    speaker._ready.emit((job, str(audio)))
    backend.rows = [{"id": "bb", "default": True, "available": True}]
    assert not spin(lambda: bool(backend.played), 0.05)


def test_device_query_failure_and_zero_volume_deny_synthesis(rig, monkeypatch):
    speaker, _, cfg, backend, generated = rig

    def fail():
        raise OSError("模拟设备读取失败")

    with monkeypatch.context() as scoped:
        scoped.setattr(backend, "devices", fail)
        speaker.speak_async("设备失败不得回退")
        assert not spin(lambda: bool(generated), 0.02)
    cfg["volume"] = 0
    speaker.apply_config(cfg)
    speaker.speak_async("音量零不得合成")
    assert not spin(lambda: bool(generated), 0.02)


def test_disabled_cache_repeats_synthesis_and_source_resets_to_user(rig):
    speaker, _, cfg, backend, generated = rig
    cfg["voice_cache_enabled"] = False
    cfg["voice_sources"]["passive"]["probability"] = 0
    speaker.apply_config(cfg)
    speaker.set_source("passive")
    speaker.speak_async("重复")
    speaker.speak_async("重复")
    assert spin(lambda: len(backend.played) == 1)
    assert spin(lambda: speaker._active is None)
    speaker.speak_async("重复")
    assert spin(lambda: len(backend.played) == 2)
    assert generated == ["重复", "重复"]
