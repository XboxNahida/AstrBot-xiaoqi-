import copy
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from companion_config import SettingsStore
from companion_main import CompanionController
from tests.test_companion_controller import Pet
from ui.companion_settings import CompanionSettings


@pytest.fixture
def companion(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    controller = CompanionController(Pet(), settings, state_dir=tmp_path)
    controller.speaker.speak_async = lambda text: None
    controller.config["profiles"][0]["model"] = "fake-model"
    assert hasattr(controller, "perception"), "独立入口应接回原菜单的感知控制器"
    controller.perception.timer.stop()
    yield app, settings, controller
    controller.shutdown()
    settings.hide()
    app.processEvents()


def settle(app, controller):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        app.processEvents()
        if not controller.perception.busy:
            return
        time.sleep(0.005)
    raise AssertionError("感知请求未结束")


def observation():
    return {
        "title": "测试工程 - 编辑器",
        "process": "code.exe",
        "scene": "work",
        "is_self": False,
        "rect": {"left": 10, "top": 20, "width": 900, "height": 600},
    }


def test_manual_critique_uses_active_route_and_shows_reply(companion, monkeypatch):
    app, settings, controller = companion
    monkeypatch.setattr(controller.perception, "observe", observation)
    requests = []

    def generate(text, config, history, **kwargs):
        requests.append((text, config["route"], history, kwargs))
        return "[curious] 代码写到哪里了？"

    controller._generate = generate
    controller.perception.trigger_manual()
    settle(app, controller)
    assert len(requests) == 1
    assert "测试工程" in requests[0][0]
    assert "未经提供" in requests[0][0]
    assert requests[0][1:3] == ("direct", [])
    assert controller.pet.text == "代码写到哪里了？"
    assert "锐评" in settings.chat_log.toPlainText()
    assert controller.busy is False


def test_tts_toggle_is_immediate_stops_and_persists(companion):
    _, settings, controller = companion
    stopped = []
    controller.speaker.stop = lambda: stopped.append(True)
    controller.perception.toggle_tts()
    assert controller.speaker.enabled is True
    assert settings.store.load()["tts_enabled"] is True
    controller.perception.toggle_tts()
    assert controller.speaker.enabled is False
    assert stopped == [True]
    assert settings.store.load()["tts_enabled"] is False


def test_auto_toggle_and_cooldown_prevent_repeated_requests(companion, monkeypatch):
    app, settings, controller = companion
    monkeypatch.setattr(controller.perception, "observe", observation)
    calls = []
    controller._generate = lambda *args, **kwargs: calls.append(True) or "继续加油"
    controller.perception.tick()
    assert calls == []
    controller.perception.toggle_auto()
    assert settings.store.load()["auto_mode"] is True
    controller.perception.tick()
    settle(app, controller)
    controller.perception.tick()
    assert calls == [True]
    controller.perception.toggle_auto()
    controller.perception.limiter.reset_cooldown()
    controller.perception.tick()
    assert calls == [True]
    assert settings.store.load()["auto_mode"] is False


@pytest.mark.parametrize("blocked", ["self", "empty", "chat", "summary"])
def test_auto_skips_self_empty_and_busy(companion, monkeypatch, blocked):
    _, _, controller = companion
    info = observation()
    if blocked == "self":
        info["is_self"] = True
    elif blocked == "empty":
        info["title"] = ""
    elif blocked == "chat":
        controller.busy = True
    else:
        controller.summary_busy = True
    controller.config["auto_mode"] = True
    monkeypatch.setattr(controller.perception, "observe", lambda: info)
    calls = []
    controller._generate = lambda *args, **kwargs: calls.append(True) or "不应执行"
    controller.perception.tick()
    assert calls == []


@pytest.mark.parametrize("route,capture_enabled,expected", [
    ("direct", False, None), ("direct", True, "fake-jpeg"), ("cloud", True, None),
])
def test_screenshot_only_when_enabled_for_direct_route(
    companion, monkeypatch, route, capture_enabled, expected
):
    app, _, controller = companion
    controller.config["route"] = route
    controller.config["critique_screenshot"] = capture_enabled
    controller.link.connected = route == "cloud"
    monkeypatch.setattr(controller.perception, "observe", observation)
    captured, generated = [], []
    monkeypatch.setattr(controller.perception, "capture", lambda rect: captured.append(rect) or "fake-jpeg")

    def generate(text, config, history, **kwargs):
        generated.append(kwargs.get("image_b64"))
        return "测试锐评"

    controller._generate = generate
    controller.perception.trigger_manual()
    settle(app, controller)
    assert generated == [expected]
    assert bool(captured) is (expected is not None)


def test_failed_auto_request_releases_busy_and_observes_cooldown(companion, monkeypatch):
    app, settings, controller = companion
    controller.config["auto_mode"] = True
    monkeypatch.setattr(controller.perception, "observe", observation)
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise ValueError("模型尚不可用")

    controller._generate = fail
    controller.perception.tick()
    settle(app, controller)
    controller.perception.tick()
    assert calls == [True]
    assert not controller.busy
    assert "模型尚不可用" in settings.feedback.text()


def test_manual_menu_uses_last_external_window_but_rejects_stale_cache(companion, monkeypatch):
    app, settings, controller = companion
    monkeypatch.setattr(controller.perception, "observe", observation)
    controller.perception.tick()
    own = {**observation(), "is_self": True, "title": "桌宠设置"}
    monkeypatch.setattr(controller.perception, "observe", lambda: own)
    calls = []
    controller.config["critique_screenshot"] = True
    controller._generate = lambda *args, **kwargs: calls.append(args[0]) or "缓存窗口锐评"
    captured = []
    monkeypatch.setattr(controller.perception, "capture", lambda rect: captured.append(rect) or "fake-jpeg")
    controller.perception.trigger_manual()
    settle(app, controller)
    assert "测试工程" in calls[0]
    assert "桌宠设置" not in calls[0]
    assert captured == []
    controller.perception.last_external_at = time.monotonic() - 61
    controller.perception.trigger_manual()
    assert len(calls) == 1
    assert "外部窗口" in settings.feedback.text()


def test_failed_toggle_save_does_not_change_runtime(companion, monkeypatch):
    _, settings, controller = companion
    original = copy.deepcopy(controller.config)

    def fail(config):
        raise OSError("磁盘无法写入")

    monkeypatch.setattr(settings.store, "save", fail)
    controller.perception.toggle_auto()
    controller.perception.toggle_tts()
    assert controller.config == original
    assert controller.speaker.enabled is False
    assert "保存失败" in settings.feedback.text()


def test_original_menu_signals_call_actions_instead_of_opening_settings(companion, tmp_path, monkeypatch):
    from companion_main import CompanionPet, connect_pet_actions

    app, _, controller = companion
    pet = CompanionPet(appearance_path=tmp_path / "appearance.json")
    called = []
    monkeypatch.setattr(controller.perception, "trigger_manual", lambda: called.append("critique"))
    monkeypatch.setattr(controller.perception, "toggle_tts", lambda: called.append("tts"))
    monkeypatch.setattr(controller.perception, "toggle_auto", lambda: called.append("auto"))
    try:
        connect_pet_actions(pet, controller)
        menu = pet._build_context_menu()
        for label in ("呼叫锐评", "切换语音", "自动感知"):
            next(action for action in menu.actions() if label in action.text()).trigger()
        assert called == ["critique", "tts", "auto"]
    finally:
        pet.physics_timer.stop()
        pet.deleteLater()
        app.processEvents()


def test_local_critique_respects_disabled_foreground_reporting(companion, monkeypatch):
    app, _, controller = companion
    controller.config["cloud"]["token"] = "fake-pairing"
    controller.config["report_foreground"] = False
    monkeypatch.setattr(controller.perception, "observe", observation)
    controller._generate = lambda *args, **kwargs: "测试锐评"
    controller.perception.trigger_manual()
    settle(app, controller)
    report = controller.link.journal.reports()[0]
    assert "foreground" not in report["facts"]
    assert observation()["title"] not in str(report["facts"])


def test_unconfigured_model_never_attempts_auto_request(companion, monkeypatch):
    _, settings, controller = companion
    controller.config["profiles"][0]["model"] = ""
    controller.config["auto_mode"] = True
    monkeypatch.setattr(controller.perception, "observe", observation)
    called = []
    controller._generate = lambda *args, **kwargs: called.append(True) or "不应执行"
    controller.perception.tick()
    controller.perception.trigger_manual()
    assert called == []
    assert "填写模型" in settings.feedback.text()
    assert "填写模型" in controller.pet.text


def test_hotkey_background_callbacks_execute_actions_on_qt_thread(companion, monkeypatch):
    import perception.hotkey_listener as listener

    app, settings, controller = companion
    main_thread = threading.get_ident()
    called, callbacks = [], {}

    class Hotkeys:
        def __init__(self, **kwargs):
            callbacks.update(kwargs)

        def start(self):
            called.append("start")

        def stop(self):
            called.append("stop")

    monkeypatch.setattr(listener, "GlobalHotkeyManager", Hotkeys)
    monkeypatch.setattr(controller.perception, "observe", observation)
    controller._generate = lambda *args, **kwargs: "快捷键锐评"
    monkeypatch.setattr(controller.pet, "cycle_next_outfit", lambda: called.append(threading.get_ident()), raising=False)
    controller.perception.start_hotkeys()

    def press():
        callbacks["on_trigger_critique"]()
        callbacks["on_toggle_mute"]()
        callbacks["on_switch_outfit"]()

    worker = threading.Thread(target=press)
    worker.start()
    worker.join(2)
    settle(app, controller)
    assert settings.store.load()["tts_enabled"] is True
    assert "快捷键锐评" in settings.chat_log.toPlainText()
    assert called == ["start", main_thread]
    controller.perception.stop()
    assert called[-1] == "stop"


def test_model_route_receives_image_only_for_direct_request(companion, monkeypatch):
    import companion_main

    _, _, controller = companion
    received = []

    class Model:
        def __init__(self, profile):
            pass

        def chat(self, text, history, **kwargs):
            received.append(kwargs.get("image_b64"))
            return "模型响应"

    monkeypatch.setattr(companion_main, "DirectModels", Model)
    assert controller._generate("截图分析", controller.config, [], image_b64="fake-jpeg") == "模型响应"
    assert received == ["fake-jpeg"]
    controller.config["route"] = "cloud"
    monkeypatch.setattr(controller.link, "chat", lambda text, **kwargs: received.append(kwargs) or "云端响应")
    controller._generate("文字观察", controller.config, [])
    assert "image_b64" not in received[-1]


def test_summary_waits_for_inflight_critique_even_with_direct_model(companion):
    _, settings, controller = companion
    controller.config["cloud"]["token"] = "fake"
    controller.busy = controller.perception.busy = True
    controller.report(True)
    assert controller.summary_busy is False
    assert "正在处理" in settings.report_log.toPlainText()


def test_changed_foreground_window_is_not_captured_with_old_title(companion, monkeypatch):
    app, _, controller = companion
    controller.config["critique_screenshot"] = True
    original = observation()
    samples = iter([original, {**original, "title": "另一个应用"}])
    monkeypatch.setattr(controller.perception, "observe", lambda: next(samples))
    captured = []
    monkeypatch.setattr(controller.perception, "capture", lambda rect: captured.append(rect) or "fake-jpeg")
    controller._generate = lambda *args, **kwargs: "文字观察锐评"
    controller.perception.trigger_manual()
    settle(app, controller)
    assert captured == []
