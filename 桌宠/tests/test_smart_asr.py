"""本地过滤实际拦截ASR提交，且裁剪后时长计费。"""

from types import SimpleNamespace

from smart_config import SmartSettingsStore
from smart_policy import SmartPolicy
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_speech_input import voice_setup as voice_setup  # noqa: PLC0414
from tests.test_speech_input import wait_result


def attach_smart(original, path, **updates):
    cfg = SmartSettingsStore.defaults()
    cfg.update(updates)
    original.smart = SimpleNamespace(cfg=cfg, policy=SmartPolicy(cfg, path))
    return original.smart


def test_silence_is_filtered_before_any_asr_request(voice_setup, tmp_path, monkeypatch):
    voice, original, status, replies, _ = voice_setup
    smart = attach_smart(original, tmp_path / "state.json", asr_filter_enabled=True)
    calls = []
    monkeypatch.setattr("speech_input.transcribe", lambda *args: calls.append(args) or "不应发生")
    voice.toggle()
    voice.chunks = [b"\0\0" * 16000]
    voice.finish()
    assert not calls and not replies and not voice.busy
    assert smart.policy.state["asr_filtered"] == 1
    assert "静音" in status[-1]


def test_trimmed_audio_is_submitted_once_and_limit_blocks_next(voice_setup, tmp_path, monkeypatch):
    voice, original, _, _, _ = voice_setup
    smart = attach_smart(original, tmp_path / "state.json", asr_trim_enabled=True,
                         asr_limit_enabled=True, asr_daily_count=1)
    calls = []
    monkeypatch.setattr("speech_input.transcribe", lambda pcm, *args: calls.append(pcm) or "测试")
    voice.toggle()
    original_pcm = b"\0\0" * 16000 + b"\x10\x27" * 3200 + b"\0\0" * 16000
    voice.chunks = [original_pcm]
    voice.finish()
    wait_result(voice)
    assert len(calls) == 1 and len(calls[0]) < len(original_pcm)
    assert 0.2 <= smart.policy.state["asr_seconds"] < 1
    voice.toggle()
    voice.chunks = [original_pcm]
    voice.finish()
    assert len(calls) == 1 and not voice.busy


def test_recording_stops_speech_and_declined_confirmation_is_free(voice_setup, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    voice, original, _, _, _ = voice_setup
    stopped = []
    original.speaker = SimpleNamespace(stop=lambda: stopped.append(True))
    smart = attach_smart(original, tmp_path / "state.json", asr_confirm=True)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    voice.toggle()
    assert stopped
    voice.finish()
    assert smart.policy.state["asr_calls"] == 0 and not voice.busy
