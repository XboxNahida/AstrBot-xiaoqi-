"""智能模式与旧固定模式互斥，以及手动截图的时序。"""

import copy
import importlib
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from tests.test_companion_models import model_server as model_server  # noqa: PLC0414
from tests.test_original_extension import consume_startup_greeting
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414

_APP = QApplication.instance() or QApplication([])


@pytest.fixture(params=["classic", "dynamic"])
def smart_runtime(original_config, tmp_path, monkeypatch, request):
    from original_runtime import create_runtime
    module = importlib.import_module("main_dynamic" if request.param == "dynamic" else "main")
    monkeypatch.setattr(module.GlobalHotkeyManager, "start", lambda self: None)
    runtime = create_runtime(request.param, state_dir=tmp_path)
    runtime.controller.speaker.enabled = False
    yield runtime
    runtime.shutdown()
    for obj in (runtime.pet, runtime.pet.bubble, runtime.pet.interaction_dialog, runtime.window, runtime.tray):
        obj.hide()


def test_smart_mode_stops_legacy_timer_and_restores_without_overwriting_auto(smart_runtime, monkeypatch):
    runtime = smart_runtime
    smart = runtime.controller.smart
    cfg = copy.deepcopy(smart.cfg)
    cfg["enabled"] = True
    old_auto = runtime.controller.auto_mode
    smart.apply(cfg)
    assert not runtime.controller.monitor_timer.isActive()
    module = importlib.import_module("main_dynamic" if runtime.controller.client_mode == "dynamic" else "main")
    base = module.DynamicPetController if runtime.controller.client_mode == "dynamic" else module.DesktopPetController
    calls = []
    monkeypatch.setattr(base, "_check_desktop_state", lambda self: calls.append("旧固定调用"))
    runtime.controller._check_desktop_state()
    assert not calls
    cfg["enabled"] = False
    smart.apply(cfg)
    assert runtime.controller.monitor_timer.isActive()
    assert runtime.controller.auto_mode == old_auto
    runtime.controller._check_desktop_state()
    assert calls == ["旧固定调用"]


def test_manual_captures_before_worker_and_uses_manual_voice_source(smart_runtime, monkeypatch):
    runtime = smart_runtime
    smart = runtime.controller.smart
    events = []
    monkeypatch.setattr("smart_runtime.get_foreground_window_info", lambda: {"title": "立即截图测试", "process": "test.exe", "scene": "work"})
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: events.append("截图") or (b"jpeg", "BASE64"))
    monkeypatch.setattr(runtime.controller.speaker, "set_source", lambda source: events.append(source))
    def model(info, image, kind="passive"):
        assert events[0] == "截图"
        assert image == "BASE64" and kind == "manual"
        return {"text": "[confident] 立即锐评结果", "usage_known": False}
    monkeypatch.setattr(smart, "generate", model)
    runtime.controller.trigger_manual_critique()
    assert events[0] == "截图"
    deadline = time.monotonic() + 2
    while runtime.controller._is_analyzing and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)
    _APP.processEvents()
    assert "manual" in events


def test_settings_change_discards_queued_smart_result(smart_runtime, monkeypatch):
    smart = smart_runtime.controller.smart
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "BASE64"))
    monkeypatch.setattr(smart, "generate", lambda *args: {"text": "旧结果", "usage_known": False})
    shown = []
    monkeypatch.setattr(smart_runtime.pet, "show_critique", shown.append)
    smart.request({"title": "测试", "scene": "work", "process": "test"}, "manual")
    smart.worker.join(timeout=2)
    smart.apply(copy.deepcopy(smart.cfg))
    _APP.processEvents()
    assert "旧结果" not in shown
    assert not smart_runtime.controller._is_analyzing


def test_passive_request_reaches_local_visual_endpoint_with_full_persona(smart_runtime, model_server, monkeypatch):
    runtime = smart_runtime
    url, received = model_server
    runtime.controller.config_mgr.config["custom_system_prompt"] = "完整人设" * 500
    runtime.bridge.config.update(route="direct", active_profile="test", profiles=[{
        "id": "test", "provider": "openai", "base_url": url, "model": "test-model", "key": "",
    }])
    info = {"title": "本机视觉测试", "scene": "work", "process": "test.exe"}
    monkeypatch.setattr("smart_runtime.get_foreground_window_info", lambda: info)
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    sources, bubbles = [], []
    monkeypatch.setattr(runtime.controller.speaker, "set_source", sources.append)
    monkeypatch.setattr(runtime.pet, "show_critique", bubbles.append)
    assert runtime.controller.smart.request(info, "passive")
    runtime.controller.smart.worker.join(timeout=3)
    _APP.processEvents()
    assert received[0]["messages"][0]["content"] == "完整人设" * 500
    assert received[0]["messages"][1]["content"][1]["image_url"]["url"].endswith("YWJj")
    assert received[0]["max_tokens"] == 256
    assert "这是本地测试回复" in bubbles and "passive" in sources


def test_cloud_notice_uses_cloud_probability(smart_runtime, monkeypatch):
    sources = []
    monkeypatch.setattr(smart_runtime.controller.speaker, "set_source", sources.append)
    smart_runtime.bridge.display("云端通知")
    assert sources[-1] == "cloud"


def test_icebreaker_does_not_skip_new_scene_debounce(smart_runtime, monkeypatch):
    smart = smart_runtime.controller.smart
    cfg = copy.deepcopy(smart.cfg)
    cfg.update(enabled=True, events_enabled=False, icebreaker_enabled=True, meeting_enabled=False)
    smart.apply(cfg)
    smart_runtime.controller.auto_mode = True
    smart.last_interaction = 0
    monkeypatch.setattr("smart_runtime.get_foreground_window_info", lambda: {"title": "刚切到的新窗口", "process": "test.exe"})
    calls = []
    monkeypatch.setattr(smart, "request", lambda *args: calls.append(args))
    smart.poll()
    assert not calls


def test_user_reply_source_survives_intervening_status_bubble(smart_runtime, monkeypatch):
    runtime = smart_runtime
    consume_startup_greeting(runtime)
    release, entered = threading.Event(), threading.Event()
    def transport(*args):
        entered.set()
        assert release.wait(3)
        return "[curious] USER-REPLY"
    runtime.controller.engine.request_handler = transport
    capture_module = "main_dynamic" if runtime.controller.client_mode == "dynamic" else "main"
    monkeypatch.setattr(f"{capture_module}.capture_screen_jpeg", lambda **kwargs: None)
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "BASE64"))
    seen = []
    monkeypatch.setattr(runtime.controller.speaker, "set_source", seen.append)
    runtime.controller.handle_user_reply("测试")
    assert entered.wait(2)
    runtime.controller.toggle_auto()
    release.set()
    deadline = time.monotonic() + 3
    while "user" not in seen and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)
    assert seen[-1] == "user"


def test_local_configuration_error_does_not_consume_model_budget(smart_runtime):
    smart_runtime.bridge.config["route"] = "cloud"
    smart = smart_runtime.controller.smart
    assert not smart.request({"title": "配置测试"}, "passive")
    assert not smart.policy.state["calls"] and smart.policy.state["unknown_usage"] == 0


def test_dnd_discards_inflight_passive_but_still_records_usage(smart_runtime, monkeypatch):
    runtime = smart_runtime
    smart = runtime.controller.smart
    bubbles = []
    monkeypatch.setattr(runtime.pet, "show_critique", bubbles.append)
    smart.cfg["dnd_enabled"] = True
    runtime.controller._is_analyzing = smart._owns_busy = True
    smart._complete((smart.generation, {}, "passive", {"text": "过期自主回复", "usage_known": True, "total_tokens": 12}, True))
    assert "过期自主回复" not in bubbles
    assert smart.policy.state["tokens"] == 12 and not runtime.controller._is_analyzing


def test_user_chat_interrupts_old_speech_at_submission_when_enabled(smart_runtime, monkeypatch):
    runtime = smart_runtime
    module = importlib.import_module("main_dynamic" if runtime.controller.client_mode == "dynamic" else "main")
    base = module.DynamicPetController if runtime.controller.client_mode == "dynamic" else module.DesktopPetController
    monkeypatch.setattr(base, "handle_user_reply", lambda *args: None)
    stopped = []
    monkeypatch.setattr(runtime.controller.speaker, "stop", lambda: stopped.append(True))
    runtime.controller.handle_user_reply("主动对话")
    assert stopped == [True]
    runtime.controller.smart.cfg["user_interrupt"] = False
    runtime.controller.handle_user_reply("不打断")
    assert stopped == [True]


@pytest.mark.parametrize("kind", ["manual", "user"])
def test_all_enabled_entries_share_compact_visual_request_rules(smart_runtime, model_server, monkeypatch, kind):
    runtime = smart_runtime
    smart = runtime.controller.smart
    url, received = model_server
    cfg = copy.deepcopy(smart.cfg)
    cfg.update(enabled=True, separate_model=True, profile_id="cheap", low_reasoning=True, reasoning_mode="low", context_chars=80)
    smart.apply(cfg)
    runtime.controller.config_mgr.config["custom_system_prompt"] = "完整人设" * 200
    runtime.bridge.config.update(route="direct", active_profile="cheap", profiles=[{
        "id": "cheap", "provider": "openai", "base_url": url, "model": "test-model", "key": "",
    }])
    info = {"scene": "video", "title": "全入口测试", "process": "test.exe"}
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    monkeypatch.setattr("smart_runtime.get_foreground_window_info", lambda: info)
    monkeypatch.setattr(runtime.controller.engine.context_memory, "get_recent_context", lambda *args, **kwargs: ["长旧记忆" * 1000])
    if kind == "manual":
        runtime.controller.trigger_manual_critique()
        smart.worker.join(timeout=3)
        _APP.processEvents()
    else:
        runtime.controller.engine.generate_interactive_reply("video", "全入口测试", "test.exe", "原始问题" * 100, "YWJj")
        _APP.processEvents()
    assert received, "请求必须到达已选模型"
    body = received[0]
    assert body["max_tokens"] == 256 and body["reasoning_effort"] == "low"
    assert body["messages"][0]["content"] == "完整人设" * 200
    prompt = body["messages"][1]["content"][0]["text"]
    assert "前情上下文连续记忆" not in prompt and len(prompt) < (180 if kind == "manual" else 600)
    assert smart.policy.state["requests_by_kind"].get(kind) == 1
    assert smart.policy.state["unknown_usage"] == 1


def test_enabled_manual_obeys_budget_but_still_captures_immediately(smart_runtime, monkeypatch):
    smart = smart_runtime.controller.smart
    cfg = copy.deepcopy(smart.cfg)
    cfg.update(enabled=True, count_limit_enabled=True, hourly_limit=1)
    smart.apply(cfg)
    smart.policy.reserve({}, time.time(), kind="user")
    captured, called = [], []
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: captured.append(True) or (b"jpeg", "YWJj"))
    monkeypatch.setattr(smart, "generate", lambda *args: called.append(True) or {"text": "不应调用"})
    # 模型配置在限额检查前有效，不以无关配置错误掩盖缺口。
    monkeypatch.setattr("smart_models.SmartModelClient.validate_configuration", lambda *args: None)
    assert not smart.request({"title": "手动测试"}, "manual")
    assert captured and not called and len(smart.policy.state["calls"]) == 1
