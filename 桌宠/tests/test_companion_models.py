import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from companion_models import DirectModels


@pytest.fixture
def model_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps({"data": [{"id": "test-model"}, {"id": "second"}]}).encode()
            )

        def do_POST(self):
            received.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.end_headers()
            result = {"choices": [{"message": {"content": "这是本地测试回复"}}]}
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", received
    server.shutdown()
    server.server_close()
    worker.join()


def test_models_and_chat_use_local_endpoint(model_server):
    url, received = model_server
    client = DirectModels(
        {"provider": "openai", "base_url": url, "model": "test-model", "key": ""}
    )
    assert client.models() == ["second", "test-model"]
    reply = client.chat(
        "你好",
        [{"role": "assistant", "content": "上一轮"}],
        system_prompt="本机测试人格",
    )
    assert reply == "这是本地测试回复"
    assert received[0]["messages"][0]["content"] == "本机测试人格"
    assert received[0]["messages"][-1]["content"] == "你好"


def test_missing_model_does_not_send_request(model_server):
    url, received = model_server
    client = DirectModels(
        {"provider": "openai", "base_url": url, "model": "", "key": ""}
    )
    with pytest.raises(ValueError, match="模型"):
        client.chat("你好")
    assert received == []


def test_http_failure_never_reports_success_or_echoes_server_secret():
    class Response:
        status_code = 401
        text = "sensitive-provider-response"

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    client = DirectModels(
        {
            "provider": "openai",
            "base_url": "https://example.com/v1",
            "model": "model",
            "key": "test",
        },
        session=Session(),
    )
    with pytest.raises(ValueError) as failure:
        client.chat("你好")
    assert "401" in str(failure.value)
    assert "sensitive" not in str(failure.value)


def test_manual_critique_reaches_http_model_and_displays_response(model_server, tmp_path, monkeypatch):
    import time

    from PyQt6.QtWidgets import QApplication

    from companion_config import SettingsStore
    from companion_main import CompanionController
    from tests.test_companion_controller import Pet
    from ui.companion_settings import CompanionSettings

    app = QApplication.instance() or QApplication([])
    url, received = model_server
    settings = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    controller = CompanionController(Pet(), settings, state_dir=tmp_path)
    controller.speaker.speak_async = lambda text: None
    controller.perception.timer.stop()
    controller.config["profiles"][0].update(base_url=url, model="test-model")
    monkeypatch.setattr(controller.perception, "observe", lambda: {
        "title": "本地HTTP集成测试窗口", "process": "editor.exe", "scene": "work", "is_self": False,
    })
    try:
        settings.request_critique.emit()
        deadline = time.monotonic() + 3
        while controller.perception.busy and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert not controller.perception.busy
        assert controller.pet.text == "这是本地测试回复"
        assert len(received) == 1
        assert "本地HTTP集成测试窗口" in received[0]["messages"][-1]["content"]
    finally:
        controller.shutdown()
        settings.hide()
        app.processEvents()
