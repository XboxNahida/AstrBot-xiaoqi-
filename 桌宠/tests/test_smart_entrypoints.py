"""核验模型总结、原文字与识别文字入口的统一省Token策略。"""

import copy
import importlib
import threading
import time

import pytest
from PyQt6.QtWidgets import QApplication

from tests.test_companion_models import model_server as model_server  # noqa: PLC0414
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_smart_runtime import smart_runtime as smart_runtime  # noqa: PLC0414


def configure(runtime, url):
    cfg = copy.deepcopy(runtime.controller.smart.cfg)
    cfg.update(enabled=True, separate_model=True, profile_id="compact", low_reasoning=True,
               reasoning_mode="low", context_chars=80)
    runtime.controller.smart.apply(cfg)
    runtime.controller.config_mgr.config["custom_system_prompt"] = "完整人设，不得精简" * 100
    runtime.bridge.config.update(route="direct", active_profile="compact", profiles=[{
        "id": "compact", "provider": "openai", "base_url": url, "model": "test-model", "key": "",
    }])


def wait_reply(runtime):
    app = QApplication.instance()
    deadline = time.monotonic() + 3
    while runtime.controller._is_analyzing and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert not runtime.controller._is_analyzing


def test_summary_shares_limits_and_metering_without_losing_facts(smart_runtime, model_server):
    runtime = smart_runtime
    url, received = model_server
    configure(runtime, url)
    facts = "请总结原始事实：" + "不可丢失的完整事实；" * 200
    reply = runtime.bridge._generate(facts, copy.deepcopy(runtime.bridge.config), [])
    assert reply == "这是本地测试回复"
    body = received[0]
    assert body.get("max_tokens") == 256 and body.get("reasoning_effort") == "low"
    assert body["messages"][0]["content"] == "完整人设，不得精简" * 100
    assert body["messages"][1]["content"] == facts
    state = runtime.controller.smart.policy.state
    assert state["requests_by_kind"]["summary"] == 1 and state["unknown_usage"] == 1
    assert not state["last_request"]["has_image"]


def test_text_and_recognized_speech_use_same_limits_and_real_usage(smart_runtime, monkeypatch):
    runtime = smart_runtime
    configure(runtime, "http://127.0.0.1:9/v1")
    requests, bubbles = [], []

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "[curious] 统一策略回复"}}],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 17, "total_tokens": 140}}

    def post(session, url, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("requests.Session.post", post)
    module = importlib.import_module("main_dynamic" if runtime.controller.client_mode == "dynamic" else "main")
    monkeypatch.setattr(module, "capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    monkeypatch.setattr(runtime.pet, "show_critique", bubbles.append)
    # 云端与模型页输入同样交由原控制器处理。
    runtime.bridge.chat("原始文字输入" * 100)
    wait_reply(runtime)
    # 仅模拟识别成功交付，不录音、不向识别服务发请求。
    voice = runtime.controller.voice_input
    voice.context = {"scene": "work", "window_title": "测试", "process_name": "test.exe"}
    voice._result(voice.generation, True, "已识别的语音原文" * 100)
    wait_reply(runtime)
    assert len(requests) == 2
    for body, original in zip(requests, ("原始文字输入" * 100, "已识别的语音原文" * 100), strict=True):
        assert body["max_tokens"] == 256 and body["reasoning_effort"] == "low"
        assert body["messages"][0]["content"] == "完整人设，不得精简" * 100
        content = body["messages"][1]["content"]
        assert content[0]["text"].endswith(original)
        assert content[1]["image_url"]["url"].endswith("YWJj")
    state = runtime.controller.smart.policy.state
    assert state["requests_by_kind"]["user"] == 2
    assert (state["input_tokens"], state["output_tokens"], state["tokens"]) == (246, 34, 280)
    assert state["unknown_usage"] == 0 and "统一策略回复" in " ".join(bubbles)


def test_failed_user_request_releases_thinking_without_retry(smart_runtime, monkeypatch):
    runtime = smart_runtime
    configure(runtime, "http://127.0.0.1:9/v1")
    calls, thinking, bubbles = [], [], []

    def fail(*args, **kwargs):
        calls.append(True)
        raise ValueError("本机模拟失败")

    monkeypatch.setattr("requests.Session.post", fail)
    module = importlib.import_module("main_dynamic" if runtime.controller.client_mode == "dynamic" else "main")
    monkeypatch.setattr(module, "capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    monkeypatch.setattr(runtime.pet, "set_thinking", thinking.append, raising=False)
    monkeypatch.setattr(runtime.pet, "show_critique", bubbles.append)
    runtime.controller.handle_user_reply("测试失败")
    wait_reply(runtime)
    assert calls == [True] and thinking[-1] is False
    assert "本机模拟失败" in bubbles


def test_old_failure_cannot_release_next_user_request(smart_runtime, monkeypatch):
    runtime = smart_runtime
    configure(runtime, "http://127.0.0.1:9/v1")
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    def fail(*args, **kwargs):
        raise ValueError("上一请求失败")
    monkeypatch.setattr("requests.Session.post", fail)
    runtime.bridge.request_epoch += 1
    # 排入Qt失败信号，但先不消费。
    worker = threading.Thread(target=lambda: run_failed_engine(runtime))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    runtime.bridge.request_epoch += 1
    runtime.controller._is_analyzing = True
    QApplication.instance().processEvents()
    assert runtime.controller._is_analyzing, "旧失败不得解除新请求的忙碌状态"
    runtime.controller._is_analyzing = False


def run_failed_engine(runtime):
    try:
        runtime.controller.engine.generate_interactive_reply("work", "测试", "test.exe", "测试", "YWJj")
    except ValueError:
        pass


def test_switching_config_cancels_thinking_for_original_user_worker(smart_runtime, monkeypatch):
    runtime = smart_runtime
    smart = runtime.controller.smart
    configure(runtime, "http://127.0.0.1:9/v1")
    thinking = []
    monkeypatch.setattr(runtime.pet, "set_thinking", thinking.append, raising=False)
    # 原互动worker持有忙碌标志，不是智能观察worker持有。
    runtime.controller._is_analyzing, smart._owns_busy = True, False
    runtime.pet.set_thinking(True)
    smart.apply(copy.deepcopy(smart.cfg))
    assert thinking[-1] is False, "丢弃旧回复时必须同时取消旧思考动画"
    assert runtime.controller._is_analyzing, "旧worker退出前不得放开并发"
    runtime.controller._is_analyzing = False


def test_queued_summary_is_not_reported_after_config_switch(smart_runtime, monkeypatch):
    runtime = smart_runtime
    smart = runtime.controller.smart
    configure(runtime, "http://127.0.0.1:9/v1")
    bridge = runtime.bridge
    bridge.config["cloud"]["token"] = "local-test-only"
    sent, emitted = [], threading.Event()
    monkeypatch.setattr(bridge.link, "report", lambda facts, summary: sent.append(summary))
    monkeypatch.setattr(bridge, "_generate", lambda *args: "旧配置总结")
    # 连接到真实后台发信号的时刻，不消费Qt队列。
    from PyQt6.QtCore import Qt
    bridge.summary_ready.connect(lambda *args: emitted.set(), Qt.ConnectionType.DirectConnection)
    bridge.report(True)
    assert emitted.wait(2)
    smart.apply(copy.deepcopy(smart.cfg))
    QApplication.instance().processEvents()
    assert not sent, "切配置后不应汇报排队的旧模型总结"
    assert not bridge.summary_busy


@pytest.mark.parametrize("kind", ["manual", "user", "summary"])
@pytest.mark.parametrize("response_style", ["empty", "malformed"])
def test_empty_model_text_still_records_actual_usage(smart_runtime, monkeypatch, kind, response_style):
    runtime = smart_runtime
    smart = runtime.controller.smart
    configure(runtime, "http://127.0.0.1:9/v1")

    class Response:
        status_code = 200

        def json(self):
            return {"choices": [] if response_style == "empty" else [{"message": None}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}}

    monkeypatch.setattr("requests.Session.post", lambda *args, **kwargs: Response())
    monkeypatch.setattr("smart_runtime.capture_screen_jpeg", lambda **kwargs: (b"jpeg", "YWJj"))
    if kind == "manual":
        assert smart.request({"title": "测试"}, "manual")
        smart.worker.join(timeout=3)
        QApplication.instance().processEvents()
    else:
        with pytest.raises(ValueError, match="文字|格式"):
            smart.generate_metered({}, "YWJj" if kind == "user" else None, kind, user_text="完整原文")
    state = smart.policy.state
    assert (state["input_tokens"], state["output_tokens"], state["tokens"]) == (90, 10, 100)
    assert state["unknown_usage"] == 0
    assert smart.policy.failed_at > 0, "空回复仍为失败，但不能抹掉实际用量"
