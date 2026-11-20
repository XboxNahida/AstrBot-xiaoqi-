"""退出后的排队问候/汇报确认不能再操作UI，不发送模型请求。"""
import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414


@pytest.mark.parametrize("mode", ["classic", "dynamic"])
def test_late_greeting_and_cloud_ack_do_not_touch_closed_ui(original_config, tmp_path, monkeypatch, mode):
    from original_runtime import GuiHotkeys, create_runtime
    monkeypatch.setattr(GuiHotkeys, "start", lambda self: None)
    runtime = create_runtime(mode, state_dir=tmp_path)
    calls = []
    monkeypatch.setattr(runtime.pet, "show_critique", lambda *args: calls.append("bubble"))
    monkeypatch.setattr(runtime.window.extension, "append_chat", lambda *args: calls.append("chat"))
    monkeypatch.setattr(runtime.window.extension.report_log, "appendPlainText", lambda *args: calls.append("ack"))
    try:
        runtime.shutdown()
        for widget in (runtime.pet, runtime.pet.bubble, runtime.pet.interaction_dialog, runtime.window, runtime.tray):
            widget.hide()
        runtime.bridge.link.report_sent.emit("late")
        QTest.qWait(1200)
        assert calls == []
        assert not runtime.pet.isVisible()
    finally:
        runtime.shutdown()
        for widget in (runtime.pet, runtime.pet.bubble, runtime.pet.interaction_dialog, runtime.window, runtime.tray):
            widget.hide()
        QApplication.processEvents()
