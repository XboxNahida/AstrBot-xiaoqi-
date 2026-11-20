"""实际原控制器的快捷键→录音→回环ASR→原人设/记忆/气泡贯通。"""

import copy
import importlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414

_APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("mode", ["classic", "dynamic"])
def test_original_voice_hotkey_real_http_asr_and_original_conversation(original_config, tmp_path, monkeypatch, mode):
    from original_runtime import create_runtime

    uploads, prompts = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            uploads.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"text": "本机语音贯通测试"}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    module = importlib.import_module("main_dynamic" if mode == "dynamic" else "main")
    monkeypatch.setattr(module.GlobalHotkeyManager, "start", lambda self: None)
    monkeypatch.setattr(module, "capture_screen_jpeg", lambda **kwargs: None)
    monkeypatch.setattr(module, "get_foreground_window_info", lambda: {"scene": "work", "title": "测试", "process": "test.exe"})
    monkeypatch.setattr("speech_input.get_foreground_window_info", module.get_foreground_window_info)

    class Stream:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]

        def start(self):
            self.callback(SimpleNamespace(copy=lambda: SimpleNamespace(tobytes=lambda: b"12" * 160)), 160, None, None)

        def stop(self):
            pass

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(InputStream=Stream))
    runtime = create_runtime(mode, state_dir=tmp_path)
    try:
        controller = runtime.controller
        assert controller.voice_input is not None
        panel = runtime.window.speech_panel
        panel.engine_combo.setCurrentIndex(panel.engine_combo.findData("openai"))
        panel.base_edit.setText(f"http://127.0.0.1:{server.server_port}/v1")
        panel.model_combo.setEditText("local-asr")
        panel.save_settings()
        controller.engine.request_handler = lambda system, prompt, image, name: prompts.append(prompt) or "[happy] 已收到语音"
        controller.hotkeys._handle_voice_input()
        _APP.processEvents()
        assert controller._voice_recording and runtime.bridge.busy
        controller.hotkeys._handle_voice_input()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and (runtime.bridge.busy or not prompts):
            _APP.processEvents()
            time.sleep(0.005)
        assert uploads[0][0] == "/v1/audio/transcriptions"
        assert b"local-asr" in uploads[0][1] and b"RIFF" in uploads[0][1]
        assert "本机语音贯通测试" in prompts[0]
        assert any("本机语音贯通测试" in row["text"] for row in controller.engine.context_memory.history)
        controller.voice_input.toggle()
        assert controller._voice_recording
        runtime.window.settings_applied.emit()
        assert not controller.voice_input.busy
        controller.voice_input.toggle()
        controller.voice_input.finish()
        controller.voice_input.worker.join(timeout=2)
        previous = len(prompts)
        monkeypatch.setattr(runtime.bridge, "_transport", lambda *args: prompts.append("不得投递到新路线") or "旧结果")
        updated = copy.deepcopy(runtime.bridge.config)
        updated["route"] = "direct"
        runtime.window.extension.settings_saved.emit(updated)
        assert not controller.voice_input.busy
        _APP.processEvents()
        assert len(prompts) == previous
    finally:
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()
        server.shutdown()
        server.server_close()
