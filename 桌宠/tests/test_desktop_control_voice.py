from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_speech_input import voice_setup as voice_setup  # noqa: PLC0414
from tests.test_speech_input import wait_result


def test_explicit_voice_target_preserves_default_chat(voice_setup, monkeypatch):
    voice, _original, _status, replies, _streams = voice_setup
    tasks = []
    monkeypatch.setattr('speech_input.transcribe', lambda *args: '点击保存')
    voice.toggle_for(tasks.append)
    voice.finish()
    wait_result(voice)
    assert tasks == ['点击保存'] and not replies
    voice.toggle()
    voice.finish()
    wait_result(voice)
    assert replies[0][0] == '点击保存'


def test_switching_record_button_cannot_retarget_existing_recording(voice_setup, monkeypatch):
    voice, _original, _status, _replies, _streams = voice_setup
    first, second = [], []
    monkeypatch.setattr('speech_input.transcribe', lambda *args: '旧任务语音')
    voice.toggle_for(first.append)
    voice.toggle_for(second.append)
    wait_result(voice)
    assert first == ['旧任务语音'] and not second


def test_cancel_discards_explicit_voice_destination(voice_setup, monkeypatch):
    voice, _original, _status, replies, _streams = voice_setup
    tasks = []
    monkeypatch.setattr('speech_input.transcribe', lambda *args: '新聊天')
    voice.toggle_for(tasks.append)
    voice.cancel()
    voice.toggle()
    voice.finish()
    wait_result(voice)
    assert not tasks and replies[0][0] == '新聊天'


def test_voice_failure_and_cancel_emit_settled(voice_setup, monkeypatch):
    voice, _original, _status, _replies, _streams = voice_setup
    done = []
    voice.settled.connect(done.append)
    def fail(*_args):
        raise ValueError('识别失败')
    monkeypatch.setattr('speech_input.transcribe', fail)
    voice.toggle_for(lambda _text: None)
    voice.finish()
    wait_result(voice)
    assert done == [False]
    voice.toggle_for(lambda _text: None)
    voice.cancel()
    assert done == [False, False]
    monkeypatch.setattr('speech_input.check_ready', fail)
    voice.toggle_for(lambda _text: None)
    assert done == [False, False, False]
