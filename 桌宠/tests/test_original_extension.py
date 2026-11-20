"""原版保留回归：原控制器与设置必须继续承担实际功能。"""

import copy
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from brain.context_memory import ContextMemoryManager
from brain.gemini_client import GeminiCritiqueEngine
from companion_config import SettingsStore
from config_manager import ConfigManager
from tests.test_companion_models import model_server as model_server  # noqa: PLC0414 -- pytest复用回环模型fixture

_APP = QApplication.instance() or QApplication([])


def consume_startup_greeting(runtime):
    """等待真实问候信号处理完毕，不让固定延时污染错误/对话断言。"""
    from PyQt6.QtTest import QSignalSpy

    greeting = QSignalSpy(runtime.controller.critique_ready)
    assert greeting.wait(5000), "未收到原程序启动问候"
    assert any("就绪" in row[0] or "已就位" in row[0] for row in greeting)


@pytest.fixture
def original_config(tmp_path, monkeypatch):
    import brain.context_memory as memory
    import config_manager

    previous_cfg, previous_memory = ConfigManager._instance, ContextMemoryManager._instance
    monkeypatch.setattr(config_manager, "CONFIG_FILE", str(tmp_path / "original.json"))
    monkeypatch.setattr(config_manager, "CYBER_WHALE_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(memory, "MEMORY_FILE", str(tmp_path / "memory.json"))
    monkeypatch.setattr(ConfigManager, "set_autostart", lambda *args: True)
    ConfigManager._instance = None
    ContextMemoryManager._instance = None
    config = ConfigManager()
    config.config.update(auto_mode=False, enable_tts=False, auto_check_update=False)
    yield config
    ConfigManager._instance, ContextMemoryManager._instance = previous_cfg, previous_memory


def test_extension_transport_keeps_original_persona_and_persistent_memory(original_config):
    original_config.config.update(pet_name="原版测试", persona_style="高冷监工", api_key="")
    engine = GeminiCritiqueEngine()
    calls = []

    def transport(system, prompt, image, name):
        calls.append((system, prompt, image, name))
        return "[confident] 原版流程回复"

    engine.request_handler = transport
    answer = engine.generate_interactive_reply("work", "测试项目", "editor.exe", "继续聊天", "图片测试")
    assert answer == "[confident] 原版流程回复"
    assert calls[0][3] == "原版测试"
    from brain.prompts import get_system_prompt
    assert calls[0][0] == get_system_prompt("原版测试", original_config.get("catchphrases"), "", persona_style="高冷监工")
    assert "继续聊天" in calls[0][1]
    assert calls[0][2] == "图片测试"
    assert len(engine.context_memory.history) == 2
    assert engine.context_memory.history[0]["text"] == "主人: 继续聊天"


def test_no_handler_keeps_original_provider_dispatch(original_config, monkeypatch):
    original_config.config.update(api_provider="openai_compatible", api_key="test-key")
    engine = GeminiCritiqueEngine()
    monkeypatch.setattr(engine, "_call_openai_compatible", lambda *args: "[curious] 原通道")
    assert engine.generate_critique("work", "原版", "editor.exe") == "[curious] 原通道"


@pytest.mark.parametrize("dynamic", [False, True])
def test_all_original_settings_tabs_styles_and_controls_remain(original_config, tmp_path, dynamic):
    from ui.original_settings import ExtendedSettingsWindow
    from ui.settings_window import SettingsWindow

    app = QApplication.instance() or QApplication([])
    reference = SettingsWindow(is_dynamic=dynamic)
    window = ExtendedSettingsWindow(is_dynamic=dynamic, store=SettingsStore(tmp_path / "extension.json"))
    try:
        original_tabs = [reference.tabs.tabText(i) for i in range(reference.tabs.count())]
        assert [window.tabs.tabText(i) for i in range(len(original_tabs))] == original_tabs
        assert window.styleSheet() == reference.styleSheet()
        assert window.tabs.tabText(window.tabs.count() - 1) == "云端与模型"
        for name in ("input_key", "combo_provider", "combo_tts_engine", "combo_voice", "chk_memory", "input_name", "txt_custom_prompt", "log_browser", "btn_check_update", "btn_manual_test"):
            assert type(getattr(window, name)) is type(getattr(reference, name))
        assert window.extension.route_combo.findData("original") >= 0
        window.showMinimized()
        window.open_window()
        app.processEvents()
        assert window.isVisible() and not window.isMinimized()
    finally:
        reference.hide()
        window.hide()
        reference.deleteLater()
        window.deleteLater()
        app.processEvents()


def test_extension_route_can_be_saved_without_changing_original_settings(original_config, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    from ui.original_settings import ExtendedSettingsWindow

    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    app = QApplication.instance() or QApplication([])
    before = copy.deepcopy(original_config.config)
    store = SettingsStore(tmp_path / "extension.json")
    window = ExtendedSettingsWindow(store=store)
    try:
        window.extension.route_combo.setCurrentIndex(window.extension.route_combo.findData("original"))
        window.tabs.setCurrentIndex(window.extension_tab)
        window.btn_save.click()
        assert store.load()["route"] == "original"
        assert original_config.config == before
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize("mode", ["classic", "dynamic"])
def test_runtime_uses_original_controller_tray_engine_and_shared_speech(original_config, tmp_path, monkeypatch, mode):
    import importlib

    from original_runtime import create_runtime
    from ui.tray_icon import DesktopPetTray

    module = importlib.import_module("main_dynamic" if mode == "dynamic" else "main")
    monkeypatch.setattr(module.GlobalHotkeyManager, "start", lambda self: None)
    monkeypatch.setattr(module, "capture_screen_jpeg", lambda **kwargs: None)
    runtime = create_runtime(mode, state_dir=tmp_path)
    try:
        monkeypatch.setattr(runtime.controller.speaker, "speak_async", lambda text: None)
        base = module.DynamicPetController if mode == "dynamic" else module.DesktopPetController
        assert isinstance(runtime.controller, base)
        assert isinstance(runtime.tray, DesktopPetTray)
        assert type(runtime.controller.engine) is GeminiCritiqueEngine
        assert runtime.bridge.speaker is runtime.controller.speaker
        assert not hasattr(runtime.bridge, "perception")
        original_menu = [a.text() for a in runtime.tray.contextMenu().actions()]
        assert "检查更新与最新公告..." in original_menu
        assert "打开刚才的评论框/吐槽互动" in original_menu
        assert runtime.window.extension.config["route"] == "original"
        runtime.controller.toggle_tts()
        assert original_config.get("enable_tts") is True
        assert runtime.window.chk_tts.isChecked()
        runtime.controller.toggle_tts()
        before = len(runtime.controller.engine.context_memory.history)
        runtime.window.extension.send_message.emit("测试原互动")
        deadline = time.monotonic() + 3
        while runtime.controller._is_analyzing and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        assert not runtime.controller._is_analyzing
        # 未配原接口时仍走原流程，不能改为独立聊天控制器。
        assert len(runtime.controller.engine.context_memory.history) == before
        runtime.controller.monitor_timer.stop()
    finally:
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()


def test_log_factory_remains_singleton_and_signal_usable(original_config):
    from log_manager import get_log_manager
    one, two = get_log_manager(), get_log_manager()
    assert one is two
    messages = []
    slot = lambda *args: messages.append(args)
    one.log_added.connect(slot)
    try:
        one.info("原日志单例验证")
        assert messages[-1][2] == "原日志单例验证"
    finally:
        one.log_added.disconnect(slot)


def test_extension_autostart_keeps_original_checkbox_and_uses_current_entry(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    import config_manager

    written = []
    registry = SimpleNamespace(
        HKEY_CURRENT_USER=0, KEY_SET_VALUE=0, REG_SZ=1,
        OpenKey=lambda *args: nullcontext("test-registry"),
        SetValueEx=lambda *args: written.append(args[-1]),
    )
    monkeypatch.setattr(config_manager, "IS_WIN", True)
    monkeypatch.setattr(config_manager, "winreg", registry)
    cfg = SimpleNamespace(autostart_command='"test-pythonw.exe" "original_main.py" --mode dynamic')
    assert ConfigManager.set_autostart(cfg, True)
    assert written == [cfg.autostart_command]


@pytest.mark.parametrize("route,cancelled", [("direct", False), ("cloud", False), ("direct", True)])
def test_extension_failure_or_pairing_cancel_stops_original_thinking_without_success(original_config, tmp_path, monkeypatch, route, cancelled):
    import main_dynamic as original
    from original_runtime import create_runtime

    monkeypatch.setattr(original.GlobalHotkeyManager, "start", lambda self: None)
    runtime = create_runtime("dynamic", state_dir=tmp_path)
    try:
        consume_startup_greeting(runtime)
        config = copy.deepcopy(runtime.bridge.config)
        config["route"] = route
        runtime.bridge.apply_settings(config)
        thinking, displayed, completed = [], [], []
        monkeypatch.setattr(runtime.pet, "set_thinking", thinking.append)
        monkeypatch.setattr(runtime.pet, "show_critique", displayed.append)
        runtime.bridge.request_completed.connect(lambda *args: completed.append(args))
        if cancelled:
            def stale_transport(*args):
                runtime.bridge.link.journal.scope = "different-pairing"
                return "旧配对回复不得显示"
            monkeypatch.setattr(runtime.bridge, "_transport", stale_transport)
        runtime.window.extension.send_message.emit("错误路径测试")
        deadline = time.monotonic() + 0.6
        while thinking[-1] is True and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        assert thinking == [True, False]
        assert completed == []
        assert len(runtime.controller.engine.context_memory.history) == 1
        if cancelled:
            assert displayed == []
        else:
            assert runtime.window.extension.feedback.text().startswith("请求失败")
            assert displayed[0].startswith("请求失败")
    finally:
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()


def test_runtime_diagnostic_uses_original_controller_busy_state(original_config, tmp_path, monkeypatch):
    import json

    import main_dynamic as original
    from original_runtime import create_runtime

    monkeypatch.setattr(original.GlobalHotkeyManager, "start", lambda self: None)
    runtime = create_runtime("dynamic", state_dir=tmp_path)
    try:
        runtime.controller._is_analyzing = True
        runtime.bridge.write_runtime()
        assert json.loads((tmp_path / "companion_runtime.json").read_text(encoding="utf-8"))["critique_in_progress"] is True
    finally:
        runtime.controller._is_analyzing = False
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()


def test_queued_old_error_and_cancel_cannot_touch_new_pair_or_request(original_config, tmp_path, monkeypatch):
    import threading

    import main_dynamic as original
    from original_runtime import create_runtime

    monkeypatch.setattr(original.GlobalHotkeyManager, "start", lambda self: None)
    runtime = create_runtime("dynamic", state_dir=tmp_path)
    try:
        consume_startup_greeting(runtime)
        displayed, thinking = [], []
        monkeypatch.setattr(runtime.pet, "show_critique", displayed.append)
        monkeypatch.setattr(runtime.pet, "set_thinking", thinking.append)
        feedback = runtime.window.extension.feedback.text()
        old_scope = runtime.bridge.link.journal.scope
        epoch = runtime.bridge.request_epoch
        worker = threading.Thread(target=lambda: runtime.bridge.request_failed.emit("旧配对错误", old_scope, epoch))
        worker.start()
        worker.join()
        runtime.bridge.link.journal.scope = "new-pair"
        _APP.processEvents()
        assert displayed == []
        assert runtime.window.extension.feedback.text() == feedback
        thinking.clear()
        worker = threading.Thread(target=lambda: runtime.bridge.request_cancelled.emit(epoch))
        worker.start()
        worker.join()
        runtime.bridge.request_epoch += 1
        runtime.controller._is_analyzing = True
        _APP.processEvents()
        assert thinking == []
    finally:
        runtime.controller._is_analyzing = False
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()


def test_original_controller_direct_model_preserves_images_persona_and_memory(original_config, tmp_path, monkeypatch, model_server):
    import main as original
    from original_runtime import create_runtime

    url, received = model_server
    original_config.config.update(pet_name="原人设", custom_system_prompt="原版设定测试")
    monkeypatch.setattr(original.GlobalHotkeyManager, "start", lambda self: None)
    monkeypatch.setattr(original, "capture_screen_jpeg", lambda **kw: (b"test", "测试图片"))
    monkeypatch.setattr(original, "get_foreground_window_info", lambda: {"scene": "work", "title": "测试编辑器", "process": "editor.exe", "rect": (0, 0, 20, 20)})
    runtime = create_runtime("classic", state_dir=tmp_path)
    try:
        config = copy.deepcopy(runtime.bridge.config)
        config["route"] = "direct"
        config["profiles"][0].update(base_url=url, model="test-model")
        runtime.bridge.apply_settings(config)
        displayed = []
        monkeypatch.setattr(runtime.pet, "show_critique", displayed.append)
        runtime.controller.handle_user_reply("保留原互动")
        deadline = time.monotonic() + 4
        while not displayed and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.01)
        assert displayed[0] == "这是本地测试回复"
        assert "原版设定测试" in received[0]["messages"][0]["content"]
        content = received[0]["messages"][-1]["content"]
        assert "保留原互动" in content[0]["text"]
        assert content[1]["image_url"]["url"].endswith("测试图片")
        assert len(runtime.controller.engine.context_memory.history) == 2
        assert runtime.controller.engine.context_memory.history[-1]["text"].startswith("原人设:")
    finally:
        runtime.shutdown()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()


async def test_original_controller_actual_cloud_channel_and_receipt(original_config, tmp_path, monkeypatch):
    import asyncio

    import main_dynamic as original
    from cloud_plugin.astrbot_plugin_desktop_link.server import DesktopLinkServer
    from original_runtime import create_runtime
    from tests.test_companion_integration import eventually

    requests = []

    async def chat(device, text):
        requests.append(text)
        return "[confident] 原版云端测试回复"

    server = DesktopLinkServer(tmp_path / "server", chat)
    await server.start(port=0)
    pair = server.pair("original-pc", "原版设备")
    monkeypatch.setattr(original.GlobalHotkeyManager, "start", lambda self: None)
    monkeypatch.setattr(original, "capture_screen_jpeg", lambda **kw: None)
    runtime = create_runtime("dynamic", state_dir=tmp_path)
    try:
        displayed = []
        monkeypatch.setattr(runtime.pet, "show_critique", displayed.append)
        config = copy.deepcopy(runtime.bridge.config)
        config["route"] = "cloud"
        config["cloud"] = {"url": f"ws://127.0.0.1:{server.port}/desktop/ws", "device_id": pair["device_id"], "token": pair["token"]}
        runtime.bridge.apply_settings(config)
        await eventually(_APP, lambda: runtime.bridge.link.connected)
        command = await server.command("original-pc", "say", {"text": "云端通知走原气泡"})
        await eventually(_APP, lambda: server.command_status(command["id"])["status"] == "succeeded")
        assert "云端通知走原气泡" in displayed
        runtime.window.extension.send_message.emit("云端互动")
        await eventually(_APP, lambda: "原版云端测试回复" in displayed)
        assert "云端互动" in requests[0]
        runtime.bridge.report(False)
        await eventually(_APP, lambda: any(r["facts"].get("original_program") for r in server.reports("original-pc")))
    finally:
        await asyncio.to_thread(runtime.bridge.link.stop)
        runtime.shutdown()
        await server.stop()
        runtime.pet.hide()
        runtime.pet.bubble.hide()
        runtime.pet.interaction_dialog.hide()
        runtime.window.hide()
        runtime.tray.hide()
