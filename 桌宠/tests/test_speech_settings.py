"""原语音输入区域的自定义模型配置，不移除腾讯云功能。"""

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox, QScrollArea

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_settings_layout import send_wheel
from ui.settings_window import SettingsWindow

_APP = QApplication.instance() or QApplication([])


@pytest.mark.parametrize("dynamic", [False, True])
def test_speech_profiles_save_switch_and_original_credentials_remain(original_config, monkeypatch, dynamic):
    monkeypatch.setattr(QMessageBox, "information", lambda *args: None)
    original_config.config.update(tencent_secret_id="old-id", tencent_secret_key="old-key")
    window = SettingsWindow(is_dynamic=dynamic)
    try:
        panel = window.speech_panel
        panel.engine_combo.setCurrentIndex(panel.engine_combo.findData("openai"))
        panel.name_edit.setText("语音一")
        panel.base_edit.setText("http://127.0.0.1:8001/v1")
        panel.key_edit.setText("speech-test-key")
        panel.model_combo.setEditText("whisper-1")
        panel.add_profile()
        panel.name_edit.setText("语音二")
        panel.model_combo.setEditText("local-asr")
        panel.profile_combo.setCurrentIndex(0)
        assert panel.name_edit.text() == "语音一"
        assert panel.model_combo.currentText() == "whisper-1"
        window.save_and_apply()
        loaded = panel.store.load()
        assert len(loaded["profiles"]) == 2
        assert loaded["engine"] == "openai"
        assert loaded["profiles"][0]["key"] == "speech-test-key"
        assert original_config.get("tencent_secret_id") == "old-id"
        assert original_config.get("tencent_secret_key") == "old-key"
        panel.profile_combo.setCurrentIndex(1)
        assert panel.model_combo.currentText() == "local-asr"
        window.resize(820, 640)
        window.show()
        _APP.processEvents()
        scroll = window.tab_general.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        old = panel.profile_combo.currentIndex()
        send_wheel(panel.profile_combo, 120)
        assert panel.profile_combo.currentIndex() == old
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


def test_stale_model_list_does_not_replace_new_profile(original_config):
    window = SettingsWindow()
    try:
        panel = window.speech_panel
        snapshot = panel._list_scope()
        panel.base_edit.setText("http://127.0.0.1:9999/v1")
        panel._models_ready(snapshot, ["old-server-asr"], "")
        assert panel.model_combo.findText("old-server-asr") == -1
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


def test_mimo_preset_protocol_saved_and_wheel_protected(original_config):
    from speech_models import active_profile

    window = SettingsWindow()
    try:
        panel = window.speech_panel
        assert panel.preset_combo.findText("小米 MiMo ASR") >= 0
        panel.preset_combo.setCurrentText("小米 MiMo ASR")
        assert panel.base_edit.text() == "https://api.xiaomimimo.com/v1"
        assert panel.model_combo.currentText() == "mimo-v2.5-asr"
        assert panel.protocol_combo.currentData() == "mimo"
        panel.key_edit.setText("synthetic-mimo-key")
        panel.save_settings()
        assert active_profile(panel.store.load())["protocol"] == "mimo"
        assert active_profile(panel.store.load())["key"] == "synthetic-mimo-key"
        old = panel.protocol_combo.currentIndex()
        send_wheel(panel.protocol_combo, 120)
        assert panel.protocol_combo.currentIndex() == old
        panel.protocol_combo.setCurrentIndex(panel.protocol_combo.findData("auto"))
        panel._capture()
        panel._refresh_profiles()
        assert panel.protocol_combo.currentData() == "auto"
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()
