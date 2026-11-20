import json
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication

from companion_config import SettingsStore
from smart_models import SmartModelClient
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from ui.companion_settings import CompanionSettings


def test_timeout_ui_switch_save_reload_and_old_config(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / 'settings.json')
    old = store.defaults()
    old['profiles'][0].pop('timeout_seconds', None)
    store.path.write_text(json.dumps(old), encoding='utf-8')
    window = CompanionSettings(store)
    try:
        assert window.timeout_spin.value() == 120
        from tests.test_settings_layout import send_wheel
        send_wheel(window.timeout_spin)
        assert window.timeout_spin.value() == 120
        window.timeout_spin.setValue(42)
        window.stream_usage_check.setChecked(False)
        window.add_profile()
        assert window.timeout_spin.value() == 120
        window.timeout_spin.setValue(240)
        window.save_settings()
        window.profile_combo.setCurrentIndex(0)
        assert window.timeout_spin.value() == 42
        window.save_settings()
        assert [p['timeout_seconds'] for p in store.load()['profiles']] == [42, 240]
        assert [p['stream_usage'] for p in store.load()['profiles']] == [False, True]
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('invalid', [0, 4, 601, True, '120', 12.5])
def test_invalid_timeout_preserves_existing_settings(tmp_path, invalid):
    store = SettingsStore(tmp_path / 'settings.json')
    cfg = store.defaults()
    store.save(cfg)
    before = store.path.read_bytes()
    cfg['profiles'][0]['timeout_seconds'] = invalid
    with pytest.raises(ValueError, match='超时'):
        store.save(cfg)
    assert store.path.read_bytes() == before


def test_original_profile_inherits_core_timeout():
    original = SimpleNamespace(config_mgr={'api_provider': 'openai_compatible', 'api_timeout': 56},
                               engine=SimpleNamespace(model='test'))
    assert SmartModelClient(original, {}, {})._original_profile()['timeout_seconds'] == 56


def test_timeout_controls_fit_original_settings_and_wheel_scrolls_page(original_config, tmp_path):
    from tests.test_settings_layout import send_wheel
    from ui.original_settings import ExtendedSettingsWindow
    app = QApplication.instance() or QApplication([])
    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / 'settings.json'))
    try:
        window.resize(820, 640)
        window.tabs.setCurrentIndex(window.extension_tab)
        window.extension.tabs.setCurrentIndex(1)
        window.show()
        app.processEvents()
        scroll = window.tabs.widget(window.extension_tab)
        scroll.verticalScrollBar().setValue(0)
        control = window.extension.timeout_spin
        before = control.value()
        send_wheel(control)
        assert control.value() == before
        assert scroll.verticalScrollBar().value() > 0
        assert scroll.horizontalScrollBar().maximum() == 0
        scroll.verticalScrollBar().setValue(0)
        assert window.grab().save(str(tmp_path / '模型超时配置.png'))
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()
