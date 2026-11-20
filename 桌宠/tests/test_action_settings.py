"""动作库搜索、分类和播放信号，以及原设置装配。"""

import copy

from PyQt6.QtWidgets import QApplication

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_settings_layout import send_wheel
from tests.test_smart_runtime import smart_runtime as smart_runtime  # noqa: PLC0414

_APP = QApplication.instance() or QApplication([])


def test_library_filters_all_actions_and_only_plays_on_explicit_request():
    from ui.action_settings import ActionLibraryPanel

    panel = ActionLibraryPanel()
    calls = []
    panel.play_requested.connect(lambda key, loop: calls.append((key, loop)))
    try:
        assert panel.action_list.count() == 100
        panel.search_edit.setText("下五子棋")
        assert panel.action_list.count() == 1 and not calls
        panel.play_button.click()
        assert calls == [("gemini:下五子棋", False)]
        panel.loop_check.setChecked(True)
        panel.play_button.click()
        assert calls[-1] == ("gemini:下五子棋", True)
        panel.idle_button.click()
        assert calls[-1] == ("idle", True)
        panel.search_edit.clear()
        panel.category_combo.setCurrentText("吃喝")
        assert panel.action_list.count() == 20
        panel.search_edit.setText("没有这个动作")
        assert panel.action_list.count() == 0 and not panel.play_button.isEnabled()
    finally:
        panel.hide()
        panel.deleteLater()
        _APP.processEvents()


def test_library_is_connected_only_to_dynamic_runtime_and_preserves_settings(smart_runtime):
    runtime = smart_runtime
    window = runtime.window
    before = copy.deepcopy(runtime.controller.config_mgr.config)
    if runtime.controller.client_mode == "classic":
        assert not hasattr(window, "action_panel")
        return
    panel = window.action_panel
    window.resize(820, 640)
    window.tabs.setCurrentIndex(window.action_tab)
    window.show()
    _APP.processEvents()
    scroll = window.tabs.widget(window.action_tab)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert window.tabs.tabText(window.tabs.count() - 1) == "云端与模型"
    panel.search_edit.setText("下五子棋")
    panel.play_button.click()
    assert runtime.pet.current_action_key == "gemini:下五子棋"
    panel.idle_button.click()
    assert runtime.pet.current_action_key == "idle"
    previous = panel.category_combo.currentIndex()
    send_wheel(panel.category_combo)
    assert panel.category_combo.currentIndex() == previous
    window.btn_save.click()
    assert runtime.controller.config_mgr.config == before


def test_random_controls_apply_save_reload_and_ignore_wheel(smart_runtime, monkeypatch):
    runtime = smart_runtime
    if runtime.controller.client_mode != "dynamic":
        return
    from tests.test_dynamic_interactions import click
    from ui.action_settings import ActionLibraryPanel

    panel = runtime.window.action_panel
    before = copy.deepcopy(runtime.controller.config_mgr.config)
    assert panel.random_enabled_check.isChecked()
    panel.random_enabled_check.setChecked(False)
    panel.random_interval_spin.setValue(45)
    assert not runtime.pet.random_actions_enabled and runtime.pet.random_action_interval == 45
    cfg = runtime.controller.config_mgr.config
    assert cfg["random_actions_enabled"] is False and cfg["random_action_interval"] == 45
    for key, value in before.items():
        if key not in ("random_actions_enabled", "random_action_interval"):
            assert cfg[key] == value
    loaded = ActionLibraryPanel()
    assert not loaded.random_enabled_check.isChecked() and loaded.random_interval_spin.value() == 45
    loaded.deleteLater()
    panel.random_enabled_check.setChecked(True)
    send_wheel(panel.random_interval_spin)
    assert panel.random_interval_spin.value() == 45
    calls = []
    monkeypatch.setattr(runtime.controller.smart, "request_manual", lambda: calls.append("立即锐评"))
    for _ in range(5):
        click(runtime.pet)
    assert calls == ["立即锐评"]
