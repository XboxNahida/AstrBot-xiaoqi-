"""设置页在原最小宽度及服务商切换后应完整显示，无横向溢出。"""

import pytest
from PyQt6.QtCore import QPoint, QPointF, QSignalBlocker, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QApplication, QScrollArea

from tests.test_original_extension import original_config as original_config  # noqa: PLC0414 -- 隔离配置fixture
from ui.settings_window import SettingsWindow

_APP = QApplication.instance() or QApplication([])


def send_wheel(widget, delta=-120):
    point = widget.rect().center()
    event = QWheelEvent(
        QPointF(point), QPointF(widget.mapToGlobal(point)), QPoint(), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(widget, event)
    _APP.processEvents()


@pytest.mark.parametrize("focused", [False, True])
def test_all_settings_combos_ignore_closed_wheel(original_config, tmp_path, focused):
    from PyQt6.QtWidgets import QComboBox

    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "extension.json"))
    try:
        window.show()
        _APP.processEvents()
        for combo in window.findChildren(QComboBox):
            with QSignalBlocker(combo):
                while combo.count() < 2:
                    combo.addItem("滚轮回归选项")
                combo.setCurrentIndex(0)
                if focused:
                    combo.setFocus()
                else:
                    combo.clearFocus()
                send_wheel(combo)
                assert combo.currentIndex() == 0, combo.currentText()
                # 点击展开后的手动选择仍可使用原控件接口。
                combo.setCurrentIndex(1)
                assert combo.currentIndex() == 1, (combo.objectName(), combo.count(), combo.isEditable())
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


@pytest.mark.parametrize("extension", [False, True])
def test_wheel_over_combo_scrolls_surrounding_page(original_config, tmp_path, extension):
    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "extension.json"))
    try:
        window.resize(820, 640)
        if extension:
            window.tabs.setCurrentIndex(window.extension_tab)
            combo = window.extension.route_combo
            scroll = window.tabs.widget(window.extension_tab)
        else:
            combo = window.combo_provider
            scroll = window.tab_general.findChild(QScrollArea)
        window.show()
        _APP.processEvents()
        bar = scroll.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(0)
        previous = combo.currentIndex()
        send_wheel(combo)
        assert combo.currentIndex() == previous
        assert bar.value() > 0
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


@pytest.mark.parametrize("focused", [False, True])
def test_wheel_does_not_change_any_numeric_time_or_slider_input(original_config, tmp_path, focused):
    from PyQt6.QtWidgets import QAbstractSpinBox, QDateTimeEdit, QLineEdit, QSlider

    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "extension.json"))
    try:
        window.show()
        _APP.processEvents()
        inputs = window.findChildren(QAbstractSpinBox) + window.findChildren(QSlider)
        assert any(isinstance(control, QDateTimeEdit) for control in inputs)
        for control in inputs:
            with QSignalBlocker(control):
                if not isinstance(control, QDateTimeEdit):
                    control.setValue((control.minimum() + control.maximum()) // 2)
                before = control.dateTime() if isinstance(control, QDateTimeEdit) else control.value()
                control.setFocus() if focused else control.clearFocus()
                send_wheel(control)
                after = control.dateTime() if isinstance(control, QDateTimeEdit) else control.value()
                assert after == before, (type(control).__name__, before, after)
                editor = control.findChild(QLineEdit)
                if editor is not None:
                    send_wheel(editor)
                    after = control.dateTime() if isinstance(control, QDateTimeEdit) else control.value()
                    assert after == before, (type(control).__name__, "输入文字区域")
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


@pytest.mark.parametrize("kind", ["number", "time", "slider"])
def test_wheel_over_inputs_scrolls_page_and_keyboard_still_changes_value(original_config, tmp_path, kind):
    from PyQt6.QtTest import QTest

    from companion_config import SettingsStore
    from ui.original_settings import ExtendedSettingsWindow

    window = ExtendedSettingsWindow(store=SettingsStore(tmp_path / "extension.json"))
    try:
        window.resize(820, 640)
        if kind == "time":
            from PyQt6.QtWidgets import QDateTimeEdit

            window.tabs.setCurrentIndex(window.smart_tab)
            control = window.smart_panel.controls["dnd_start"]
            control.setCurrentSection(QDateTimeEdit.Section.MinuteSection)
            scroll = window.tabs.widget(window.smart_tab)
            value = control.time
        else:
            control = window.spin_cooldown if kind == "number" else window.slider_cooldown
            control.setValue(100)
            scroll = window.tab_general.findChild(QScrollArea)
            value = control.value
        window.show()
        _APP.processEvents()
        bar = scroll.verticalScrollBar()
        bar.setValue(0)
        before = value()
        send_wheel(control)
        assert value() == before and bar.value() > 0
        control.setFocus()
        if kind == "time":
            control.setCurrentSection(QDateTimeEdit.Section.MinuteSection)
        QTest.keyClick(control, Qt.Key.Key_Right if kind == "slider" else Qt.Key.Key_Up)
        assert value() != before
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


def test_combo_popup_mouse_and_keyboard_selection_still_work(original_config):
    from PyQt6.QtTest import QTest

    window = SettingsWindow()
    try:
        window.show()
        _APP.processEvents()
        combo = window.combo_provider
        combo.setCurrentIndex(0)
        combo.setFocus()
        _APP.processEvents()
        assert combo.hasFocus()
        send_wheel(combo)
        assert combo.currentIndex() == 0
        QTest.keyClick(combo, Qt.Key.Key_Down)
        assert combo.currentIndex() == 1
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton, pos=QPoint(combo.width() - 10, combo.height() // 2))
        _APP.processEvents()
        view = combo.view()
        assert view.isVisible()
        index = combo.model().index(2, 0)
        QTest.mouseMove(view.viewport(), view.visualRect(index).center())
        QTest.qWait(250)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center())
        assert combo.currentIndex() == 2
        assert not view.isVisible()
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


@pytest.mark.parametrize("dynamic", [False, True])
@pytest.mark.parametrize("width", [820, 860, 1100])
def test_general_tab_fits_minimum_window_for_all_provider_and_voice_options(original_config, dynamic, width):
    window = SettingsWindow(is_dynamic=dynamic)
    try:
        window.resize(width, 680)
        window.show()
        _APP.processEvents()
        scroll = window.tab_general.findChild(QScrollArea)
        for provider in range(window.combo_provider.count()):
            window.combo_provider.setCurrentIndex(provider)
            for voice in range(window.combo_tts_engine.count()):
                window.combo_tts_engine.setCurrentIndex(voice)
                _APP.processEvents()
                assert scroll.horizontalScrollBar().maximum() == 0, (
                    window.combo_provider.currentData(), window.combo_tts_engine.currentData(),
                    scroll.widget().minimumSizeHint().width(), scroll.viewport().width(),
                )
                assert scroll.widget().width() <= scroll.viewport().width()
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()


def test_animation_fps_slider_is_on_its_label_row(original_config):
    window = SettingsWindow(is_dynamic=True)
    try:
        window.show()
        _APP.processEvents()
        voice_group = window.slider_fps.parentWidget()
        grid = voice_group.layout()
        label = grid.itemAtPosition(12, 0).widget()
        assert label.text() == "动态立绘帧率调整:"
        slider_row = grid.itemAtPosition(12, 1)
        assert slider_row is not None
        assert slider_row.layout().itemAt(0).widget() is window.slider_fps
    finally:
        window.hide()
        window.deleteLater()
        _APP.processEvents()
