"""语音适配器模拟录音与识别，验证退出/改配置后旧结果丢弃。"""

import sys
import time
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def voice_setup(original_config, tmp_path, monkeypatch):
    from speech_input import SpeechInputController
    from speech_models import SpeechSettingsStore

    store = SpeechSettingsStore(tmp_path / "speech.json")
    cfg = store.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0]["model"] = "test-asr"
    store.save(cfg)
    status, replies, streams = [], [], []
    original = SimpleNamespace(
        _is_analyzing=False, bridge=None, config_mgr=original_config, client_mode="dynamic",
        pet=SimpleNamespace(show=lambda: None, show_critique=status.append),
        handle_user_reply=lambda *args: replies.append(args),
        show_settings=lambda: None,
    )

    class Stream:
        def __init__(self, **kwargs):
            assert kwargs["samplerate"] == 16000 and kwargs["channels"] == 1
            self.kwargs, self.closed = kwargs, False
            streams.append(self)

        def start(self):
            self.kwargs["callback"](SimpleNamespace(copy=lambda: SimpleNamespace(tobytes=lambda: b"12" * 160)), 160, None, None)

        def stop(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    monkeypatch.setattr("speech_input.get_foreground_window_info", lambda: {"scene": "work", "title": "测试窗口"})
    voice = SpeechInputController(original, store)
    yield voice, original, status, replies, streams
    voice.shutdown()


def wait_result(voice):
    deadline = time.monotonic() + 2
    while voice.busy and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)


@pytest.mark.parametrize("mode", ["classic", "dynamic"])
def test_recording_transcription_returns_to_original_conversation(voice_setup, monkeypatch, mode):
    voice, original, _status, replies, streams = voice_setup
    original.client_mode = mode
    monkeypatch.setattr("speech_input.transcribe", lambda *args: "语音识别文字")
    voice.request_toggle.emit()
    assert voice.busy and original._voice_recording
    voice.request_toggle.emit()
    wait_result(voice)
    assert streams[0].closed and not voice.busy
    assert replies[0][0] == "语音识别文字"
    assert len(replies[0]) == (2 if mode == "classic" else 1)


@pytest.mark.parametrize("action", ["cancel", "shutdown"])
def test_queued_old_result_after_settings_change_or_shutdown_is_discarded(voice_setup, monkeypatch, action):
    voice, _original, _status, replies, streams = voice_setup
    monkeypatch.setattr("speech_input.transcribe", lambda *args: "旧识别结果")
    voice.toggle()
    voice.finish()
    # 后台发出结果后先取消/退出，再处理GUI排队信号。
    deadline = time.monotonic() + 2
    while voice.worker.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    getattr(voice, action)()
    _APP.processEvents()
    assert not replies
    assert streams[0].closed and not voice.busy


def test_recognition_failure_does_not_send_chat(voice_setup, monkeypatch):
    voice, _original, status, replies, _streams = voice_setup
    def fail(*args):
        raise ValueError("模型不支持语音")
    monkeypatch.setattr("speech_input.transcribe", fail)
    voice.toggle()
    voice.finish()
    wait_result(voice)
    assert not replies
    assert "模型不支持语音" in status[-1]
