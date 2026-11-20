"""用假合成/播放检查静音与退出，不访问网络或声卡。"""

import threading
from pathlib import Path

import pytest

from tts.edge_speaker import EdgeSpeaker


@pytest.mark.parametrize("reenable", [False, True])
def test_stop_cancels_inflight_and_queued_speech(tmp_path, monkeypatch, reenable):
    speaker = EdgeSpeaker(enabled=True)
    speaker._temp_dir = str(tmp_path)
    entered, release = threading.Event(), threading.Event()
    generated, played = [], []

    def generate(text, path):
        generated.append(text)
        entered.set()
        assert release.wait(3)
        Path(path).write_bytes(b"fake-audio")

    monkeypatch.setattr(speaker, "_generate_edge_tts", generate)
    monkeypatch.setattr(speaker, "_play_audio", lambda path: played.append(path))
    monkeypatch.setattr(speaker, "_stop_playback", lambda: None, raising=False)
    workers = []
    original_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = original_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(threading, "Thread", thread)
    try:
        speaker.speak_async("正在合成的一条")
        assert entered.wait(2)
        speaker.speak_async("排队等待的一条")
        speaker.update_config(enabled=False)
        speaker.stop()
        if reenable:
            speaker.update_config(enabled=True)
        release.set()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()
        assert played == []
        assert generated == ["正在合成的一条"]
    finally:
        release.set()


def test_new_speech_after_stop_can_play(tmp_path, monkeypatch):
    speaker = EdgeSpeaker(enabled=True)
    speaker._temp_dir = str(tmp_path)
    played = threading.Event()
    monkeypatch.setattr(speaker, "_generate_edge_tts", lambda text, path: Path(path).write_bytes(b"fake-audio"))
    monkeypatch.setattr(speaker, "_play_audio", lambda path: played.set())
    monkeypatch.setattr(speaker, "_stop_playback", lambda: None, raising=False)
    speaker.stop()
    speaker.speak_async("停止后的新消息")
    assert played.wait(2)
