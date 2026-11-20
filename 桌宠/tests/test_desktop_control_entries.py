"""桌面操作入口追加与原设置保存路径回归。"""
from types import SimpleNamespace

from PyQt6.QtWidgets import QMenu

from companion_config import SettingsStore
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414 -- 显式复用pytest配置隔离fixture


def test_desktop_page_bottom_save_and_model_refresh(original_config, tmp_path):
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / 'extension.json'))
    try:
        assert window.tabs.tabText(window.desktop_tab) == '桌面操作'
        assert window.desktop_tab + 1 == window.extension_tab
        assert window.tabs.tabText(window.features_tab) == '扩展功能'
        window.tabs.setCurrentIndex(window.desktop_tab)
        window.desktop_panel.enabled.setChecked(True)
        window.btn_save.click()
        assert window.desktop_panel.store.load()['enabled'] is True
        window.extension.config['profiles'].append({'id': 'desktop-test', 'name': '测试', 'model': 'model'})
        window.extension.settings_saved.emit(window.extension.config)
        assert window.desktop_panel.profile.findData('desktop-test') >= 0
    finally:
        window.hide()
        window.deleteLater()


def test_menu_appends_once_and_delays_controller_lookup():
    from qa_menu import install_menu

    def original():
        menu = QMenu()
        for text in ('原功能', '立即锐评', '再次锐评', '云端原菜单'):
            menu.addAction(text)
        return menu

    calls = []
    controller = SimpleNamespace(screenshot_qa=SimpleNamespace(open=lambda: calls.append('qa')))
    pet = SimpleNamespace(_build_context_menu=original)
    install_menu(pet, controller)
    menu = pet._build_context_menu()
    labels = [action.text() for action in menu.actions()]
    assert [label for label in labels if label in ('原功能', '立即锐评', '再次锐评', '云端原菜单')] == [
        '原功能', '立即锐评', '再次锐评', '云端原菜单']
    assert labels.count('🖱 操作桌面…') == 1
    assert labels.count('停止桌面操作') == 1
    qa = next(i for i, label in enumerate(labels) if '截图答疑' in label)
    assert labels[qa + 1:qa + 3] == ['🖱 操作桌面…', '停止桌面操作']
    controller.desktop_control = SimpleNamespace(open=lambda: calls.append('open'), stop=lambda: calls.append('stop'))
    menu.actions()[qa + 1].trigger()
    menu.actions()[qa + 2].trigger()
    assert calls == ['open', 'stop']


def test_hotkeys_default_unbound_and_binding_keeps_tab_indices(original_config, tmp_path, monkeypatch):
    from desktop_bindings import install_bindings
    from hotkey_config import DEFAULT_KEYS, KEY_LABELS, ConfiguredHotkeys
    from ui.original_settings import ExtendedSettingsWindow

    for name in ('desktop', 'desktop_voice', 'desktop_stop'):
        assert DEFAULT_KEYS.get(name) == ''
        assert KEY_LABELS.get(name)
    monkeypatch.setattr(ConfiguredHotkeys, 'apply', lambda self, values, *a, **kw: setattr(self, 'bindings', values))
    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / 'extension.json'))
    noop = lambda: None
    controller = SimpleNamespace(hotkeys=SimpleNamespace(stop=noop), trigger_manual_critique=noop,
        voice_input=SimpleNamespace(request_toggle=SimpleNamespace(emit=noop)), toggle_tts=noop,
        features=SimpleNamespace(cfg={'hotkeys': {'rules': [], 'enabled': False}}, hotkey_listener=None))
    runtime = SimpleNamespace(controller=controller, window=window, pet=SimpleNamespace(cycle_next_outfit=noop))
    try:
        manager = install_bindings(runtime, tmp_path)
        assert window.tabs.tabText(window.desktop_tab) == '桌面操作'
        assert window.tabs.tabText(window.extension_tab) == '云端与模型'
        calls = []
        controller.desktop_control = SimpleNamespace(open=lambda: calls.append('open'),
            voice=lambda: calls.append('voice'), stop=lambda: calls.append('stop'))
        for key in ('desktop', 'desktop_voice', 'desktop_stop'):
            manager.callbacks[key]()
        assert calls == ['open', 'voice', 'stop']
    finally:
        window.hide()
        window.deleteLater()
