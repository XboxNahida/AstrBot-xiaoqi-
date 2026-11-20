"""语音模型配置与内存音频请求，不使用真实模型或麦克风。"""

import base64
import io
import json
import wave

import pytest

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414


def test_speech_profiles_roundtrip_encrypts_keys_and_preserves_original(original_config, tmp_path):
    from speech_models import SpeechSettingsStore

    before = dict(original_config.config)
    store = SpeechSettingsStore(tmp_path / "speech.json")
    cfg = store.load()
    assert cfg["engine"] == "tencent"
    cfg["engine"] = "openai"
    cfg["profiles"][0].update(key="asr-test-secret", model="whisper-1", name="测试语音")
    store.save(cfg)
    assert "asr-test-secret" not in store.path.read_text(encoding="utf-8")
    assert store.load() == cfg
    assert original_config.config == before


def test_custom_transcription_sends_wav_model_and_optional_key(monkeypatch):
    from speech_models import transcribe

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Reply", (), {"status_code": 200, "json": lambda self: {"text": "  你好桌宠  "}})()

    monkeypatch.setattr("speech_models.requests.post", post)
    cfg = {"engine": "openai", "active_profile": "a", "profiles": [
        {"id": "a", "base_url": "http://127.0.0.1:8000/v1/audio/transcriptions", "key": "abc", "model": "whisper-1"},
    ]}
    assert transcribe(b"\x01\x00" * 160, cfg) == "你好桌宠"
    url, request = calls[0]
    assert url == cfg["profiles"][0]["base_url"]
    assert request["headers"] == {"Authorization": "Bearer abc"}
    assert request["data"]["model"] == "whisper-1"
    assert request["allow_redirects"] is False
    filename, wav_data, mime = request["files"]["file"]
    assert filename.endswith(".wav") and mime == "audio/wav"
    with wave.open(io.BytesIO(wav_data), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
        assert wav.readframes(160) == b"\x01\x00" * 160


def test_tencent_engine_still_uses_original_recognizer(monkeypatch):
    from speech_models import transcribe

    monkeypatch.setattr("perception.tencent_asr.transcribe_pcm", lambda pcm, sid, key: f"{sid}/{key}/{len(pcm)}")
    assert transcribe(b"12", {"engine": "tencent"}, "test-id", "test-key") == "test-id/test-key/2"


@pytest.mark.parametrize("reply,status", [({}, 200), ({"text": ""}, 200), ({"text": 123}, 200), ({"text": "ok"}, 401)])
def test_invalid_response_is_not_success(monkeypatch, reply, status):
    from speech_models import SpeechSettingsStore, transcribe

    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0]["model"] = "asr"
    monkeypatch.setattr("speech_models.requests.post", lambda *a, **k: type("Reply", (), {"status_code": status, "json": lambda self: reply})())
    with pytest.raises(ValueError):
        transcribe(b"12", cfg)


def test_invalid_config_keeps_original_file(tmp_path):
    from speech_models import SpeechSettingsStore

    store = SpeechSettingsStore(tmp_path / "speech.json")
    store.path.write_text(json.dumps({"engine": "unknown"}), encoding="utf-8")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.load()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("suffix", ["", "/chat/completions", "/audio/transcriptions"])
def test_legacy_mimo_uses_official_json_protocol(monkeypatch, suffix):
    from speech_models import SpeechSettingsStore, transcribe

    calls = []
    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0].update(base_url="https://api.xiaomimimo.com/v1" + suffix,
                              model="mimo-v2.5-asr", key="synthetic-secret")

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return type("Reply", (), {"status_code": 200, "json": lambda self: {
            "choices": [{"message": {"content": "  测试识别  "}}]}})()

    monkeypatch.setattr("speech_models.requests.post", post)
    assert transcribe(b"\x01\x00" * 160, cfg) == "测试识别"
    assert len(calls) == 1
    url, request = calls[0]
    assert url == "https://api.xiaomimimo.com/v1/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer synthetic-secret"}
    assert request["allow_redirects"] is False
    assert "files" not in request
    payload = request["json"]
    assert payload["model"] == "mimo-v2.5-asr" and payload["stream"] is False
    audio = payload["messages"][0]["content"][0]
    assert audio["type"] == "input_audio"
    encoded = audio["input_audio"]["data"].removeprefix("data:audio/wav;base64,")
    with wave.open(io.BytesIO(base64.b64decode(encoded)), "rb") as wav:
        assert wav.readframes(160) == b"\x01\x00" * 160


@pytest.mark.parametrize("status", [302, 401, 404, 429, 500])
def test_mimo_errors_are_sanitized_and_never_retried(monkeypatch, status):
    from speech_models import SpeechSettingsStore, transcribe

    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0].update(protocol="mimo", model="mimo-v2.5-asr", key="private-key")
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return type("Reply", (), {"status_code": status, "text": "private-key audio-body"})()

    monkeypatch.setattr("speech_models.requests.post", post)
    with pytest.raises(ValueError) as error:
        transcribe(b"12", cfg)
    assert str(status) in str(error.value) and "/chat/completions" in str(error.value)
    assert "private-key" not in str(error.value) and "audio-body" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("reply", [{}, {"choices": []}, {"choices": [None]},
                                   {"choices": [{"message": {"content": ""}}]},
                                   {"choices": [{"message": {"content": 123}}]}])
def test_mimo_invalid_reply_is_not_success(monkeypatch, reply):
    from speech_models import SpeechSettingsStore, transcribe

    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0].update(protocol="mimo", model="mimo-v2.5-asr")
    monkeypatch.setattr("speech_models.requests.post", lambda *a, **k: type("Reply", (), {
        "status_code": 200, "json": lambda self: reply})())
    with pytest.raises(ValueError, match="没有返回识别文字"):
        transcribe(b"12", cfg)


def test_mimo_real_loopback_http_and_explicit_openai_override():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from speech_models import SpeechSettingsStore, speech_protocol, transcribe

    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers["Authorization"], body))
            response = json.dumps({"choices": [{"message": {"content": "本机贯通"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    profile = cfg["profiles"][0]
    profile.update(protocol="mimo", model="mimo-v2.5-asr", key="loopback-only",
                   base_url=f"http://127.0.0.1:{server.server_port}/v1/chat/completions")
    try:
        assert transcribe(b"\x00\x00" * 160, cfg) == "本机贯通"
        assert len(calls) == 1
        assert calls[0][0] == "/v1/chat/completions"
        assert calls[0][1] == "Bearer loopback-only"
        assert calls[0][2]["messages"][0]["role"] == "user"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    profile.update(base_url="https://api.xiaomimimo.com/v1", protocol="openai")
    assert speech_protocol(profile) == "openai"
    profile.pop("protocol")
    profile["base_url"] = "https://api.xiaomimimo.com.example.invalid/v1"
    assert speech_protocol(profile) == "openai"


def test_mimo_network_error_traceback_does_not_expose_request(monkeypatch):
    import traceback

    import requests

    from speech_models import SpeechSettingsStore, transcribe

    cfg = SpeechSettingsStore.defaults()
    cfg["engine"] = "openai"
    cfg["profiles"][0].update(protocol="mimo", model="mimo-v2.5-asr")

    def post(*args, **kwargs):
        raise requests.ConnectionError("private-key audio-body")

    monkeypatch.setattr("speech_models.requests.post", post)
    with pytest.raises(ValueError) as error:
        transcribe(b"12", cfg)
    rendered = "".join(traceback.format_exception(error.value))
    assert "private-key" not in rendered and "audio-body" not in rendered
