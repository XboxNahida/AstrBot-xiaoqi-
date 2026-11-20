"""原设置中心仅追加扩展页面。"""
from companion_config import SettingsStore
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from ui.original_settings import ExtendedSettingsWindow


def test_features_tab_inserted_without_replacing_original(original_config, tmp_path):
    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "companion.json"))
    try:
        assert window.tabs.tabText(window.features_tab) == "扩展功能"
        assert window.tabs.tabText(window.tabs.count() - 1) == "云端与模型"
        window.tabs.setCurrentIndex(window.features_tab)
        window.features_panel.controls[("feeding", "enabled")].setChecked(False)
        window.save_and_apply()
        assert window.features_panel.store.load()["feeding"]["enabled"] is False
    finally:
        window.hide()
        window.deleteLater()
