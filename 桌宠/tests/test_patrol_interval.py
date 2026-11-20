"""巡逻间隔可精确填写，保存后最多一小时一次。"""

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from brain.rate_limiter import CritiqueRateLimiter
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from ui.settings_window import SettingsWindow

_APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("dynamic", [False, True])
def test_patrol_typed_interval_save_reload_and_slider_sync(original_config, monkeypatch, dynamic):
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    window = SettingsWindow(is_dynamic=dynamic)
    try:
        assert window.slider_cooldown.maximum() == 3600
        spin = window.spin_cooldown
        assert (spin.minimum(), spin.maximum()) == (15, 3600)
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), "3600")
        QTest.keyClick(spin.lineEdit(), Qt.Key.Key_Enter)
        assert window.slider_cooldown.value() == 3600
        window.save_and_apply()
        original_config.load()
        assert original_config.get("cooldown_seconds") == 3600
        window.slider_cooldown.setValue(1500)
        assert spin.value() == 1500
        window._load_values()
        assert spin.value() == window.slider_cooldown.value() == 3600
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


def test_same_window_does_not_extend_selected_hour_to_ninety_minutes(monkeypatch):
    limiter = CritiqueRateLimiter(3600)
    monkeypatch.setattr("brain.rate_limiter.time.time", lambda: 10000)
    limiter.record_critique("work", "同一窗口")
    monkeypatch.setattr("brain.rate_limiter.time.time", lambda: 13599)
    assert not limiter.should_trigger_auto("work", "同一窗口")
    monkeypatch.setattr("brain.rate_limiter.time.time", lambda: 13600)
    assert limiter.should_trigger_auto("work", "同一窗口")
