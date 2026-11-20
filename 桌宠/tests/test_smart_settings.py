"""智能陪伴独立配置页与原设置互斥回归；只用临时配置。"""

import copy
import importlib.util
import sys
import types

import pytest
from PyQt6.QtCore import Qt, QTime
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QSpinBox, QTimeEdit

from smart_config import SmartSettingsStore
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_settings_layout import send_wheel

_APP = QApplication.instance() or QApplication([])


def make_panel(tmp_path, **kwargs):
    assert importlib.util.find_spec("ui.smart_settings") is not None, "缺少独立智能设置页"
    from ui.smart_settings import SmartSettingsPanel

    return SmartSettingsPanel(SmartSettingsStore(tmp_path / "smart.json"), **kwargs)


def dispose(widget):
    widget.hide()
    widget.deleteLater()
    _APP.processEvents()


def test_all_schema_fields_have_real_controls_and_round_trip(tmp_path):
    panel = make_panel(tmp_path)
    try:
        assert set(panel.controls) == set(SmartSettingsStore.defaults())
        received = []
        panel.settings_saved.connect(received.append)
        for key, default in SmartSettingsStore.defaults().items():
            control = panel.controls[key]
            if type(default) is bool:
                assert isinstance(control, QCheckBox)
                control.setChecked(not default)
            elif type(default) is int:
                assert isinstance(control, QSpinBox)
                control.setValue(control.minimum() + 1)
            elif key in ("dnd_start", "dnd_end"):
                assert isinstance(control, QTimeEdit)
                control.setTime(QTime(12, 34))
        for controls in panel.controls["voice_sources"].values():
            controls["enabled"].setChecked(False)
            controls["probability"].setValue(0)
            assert controls["probability"].minimum() == 0
            assert controls["probability"].maximum() == 100
        assert panel.save_settings()
        loaded = panel.store.load()
        assert received == [loaded]
        assert loaded["dnd_start"] == "12:34"
        assert loaded["enabled"] is True
        assert loaded["voice_sources"]["user"] == {"enabled": False, "probability": 0}
        assert "已保存" in panel.status_label.text()
    finally:
        dispose(panel)


def test_devices_refresh_keeps_selected_offline_ids(tmp_path, monkeypatch):
    store = SmartSettingsStore(tmp_path / "smart.json")
    config = store.defaults()
    config["allowed_devices"] = ["offline"]
    store.save(config)
    devices = [{"id": "headset", "name": "耳机", "default": True, "available": True}]
    monkeypatch.setitem(sys.modules, "smart_audio", types.SimpleNamespace(audio_devices=lambda: devices))
    panel = make_panel(tmp_path)
    try:
        panel.refresh_devices()
        listing = panel.controls["allowed_devices"]
        by_id = {listing.item(i).data(Qt.ItemDataRole.UserRole): listing.item(i) for i in range(listing.count())}
        assert by_id["offline"].isSelected()
        assert "离线" in by_id["offline"].text()
        by_id["headset"].setSelected(True)
        devices.clear()
        panel.refresh_devices()
        assert panel.save_settings()
        assert set(store.load()["allowed_devices"]) == {"offline", "headset"}
    finally:
        dispose(panel)


def test_model_refresh_keeps_selection_without_copying_secrets(tmp_path):
    profiles = [{"id": "vision", "name": "视觉", "model": "test-vision", "key": "private-secret"}]
    panel = make_panel(tmp_path, profiles_provider=lambda: profiles)
    try:
        combo = panel.controls["profile_id"]
        combo.setCurrentIndex(combo.findData("vision"))
        profiles.clear()
        panel.refresh_profiles()
        assert combo.currentData() == "vision"
        assert "不可用" in combo.currentText()
        assert panel.save_settings()
        assert "private-secret" not in panel.store.path.read_text(encoding="utf-8")
    finally:
        dispose(panel)


@pytest.mark.parametrize("dynamic", [False, True])
def test_new_tab_save_is_independent_and_original_mode_controls_restore(original_config, tmp_path, dynamic):
    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    before = copy.deepcopy(original_config.config)
    store = SettingsStore(tmp_path / "extension.json")
    window = ExtendedSettingsWindow(store=store, is_dynamic=dynamic)
    try:
        assert hasattr(window, "smart_panel"), "尚未接入智能陪伴页"
        assert window.tabs.tabText(window.smart_tab) == "智能陪伴"
        assert window.tabs.tabText(window.tabs.count() - 1) == "云端与模型"
        assert window.smart_panel.store.path == tmp_path / "smart_settings.json"
        window.tabs.setCurrentIndex(window.smart_tab)
        window.smart_panel.controls["enabled"].setChecked(True)
        window.btn_save.click()
        for control in (window.chk_auto, window.slider_cooldown, window.spin_cooldown):
            assert not control.isEnabled()
            assert "智能" in control.toolTip()
        assert original_config.config == before
        assert not store.path.exists()
        window.smart_panel.controls["enabled"].setChecked(False)
        window.btn_save.click()
        for control in (window.chk_auto, window.slider_cooldown, window.spin_cooldown):
            assert control.isEnabled()
        assert original_config.config == before
    finally:
        dispose(window)


def test_new_page_fits_minimum_width_and_combo_wheel_scrolls(original_config, tmp_path):
    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "extension.json"))
    try:
        assert hasattr(window, "smart_panel"), "尚未接入智能陪伴页"
        window.resize(820, 640)
        window.tabs.setCurrentIndex(window.smart_tab)
        window.show()
        _APP.processEvents()
        scroll = window.tabs.widget(window.smart_tab)
        assert scroll.horizontalScrollBar().maximum() == 0
        for combo in window.smart_panel.findChildren(QComboBox):
            scroll.verticalScrollBar().setValue(0)
            previous = combo.currentIndex()
            send_wheel(combo)
            assert combo.currentIndex() == previous
            assert scroll.verticalScrollBar().value() > 0
    finally:
        dispose(window)
