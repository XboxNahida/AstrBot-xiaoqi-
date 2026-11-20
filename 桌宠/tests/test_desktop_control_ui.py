"""桌面操作界面离屏回归，不接触真实桌面或模型。"""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QFont, QFontDatabase, QMouseEvent, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QPlainTextEdit,
    QSpinBox,
    QTextBrowser,
)

from ui.desktop_control import ControlPanel, ControlWindow

APP = QApplication.instance() or QApplication([])


class Store:
    def __init__(self):
        self.value = {}

    def load(self):
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)


def test_panel_defaults_profiles_save_and_refresh():
    store = Store()
    profiles = [{'id': 'local', 'name': '本机', 'model': 'vision'}]
    panel = ControlPanel(store, lambda: profiles)
    assert panel.config == {'enabled': False, 'cloud_enabled': False, 'allow_original': False,
        'move_pet': True, 'pause_on_input': False, 'prefer_controls': True, 'profile_id': 'original',
        'max_steps': 20, 'max_seconds': 180}
    assert panel.profile.itemData(0) == 'original'
    assert panel.profile.itemText(0) == '沿用原模型'
    panel.profile.setCurrentIndex(1)
    panel.enabled.setChecked(True)
    saved = []
    panel.settings_saved.connect(saved.append)
    panel.save_settings()
    assert store.value['profile_id'] == 'local'
    assert saved == [store.value]
    profiles.append({'id': 'second', 'name': '另一套', 'model': 'm'})
    panel.refresh_profiles()
    assert panel.profile.currentData() == 'local'
    assert panel.profile.count() == 3
    panel.close()


def test_controls_ignore_wheel_and_fit_360():
    panel = ControlPanel(Store(), lambda: [{'id': 'x', 'name': '长模型名' * 30, 'model': 'm'}])
    panel.resize(360, 480)
    panel.show()
    APP.processEvents()
    for control in panel.findChildren(QComboBox) + panel.findChildren(QSpinBox):
        before = control.currentIndex() if isinstance(control, QComboBox) else control.value()
        event = QWheelEvent(QPointF(4, 4), QPointF(4, 4), QPoint(), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        APP.sendEvent(control, event)
        after = control.currentIndex() if isinstance(control, QComboBox) else control.value()
        assert before == after
    assert panel.scroll.verticalScrollBar().value() > 0
    assert panel.scroll.horizontalScrollBar().maximum() == 0
    assert panel.scroll.verticalScrollBar().maximum() > 0
    panel.close()


def test_window_submission_busy_signals_plain_text_and_hide():
    window = ControlWindow()
    sent, voice, new = [], [], []
    window.submitted.connect(sent.append)
    window.voice_requested.connect(lambda: voice.append(True))
    window.new_requested.connect(lambda: new.append(True))
    window.input.setPlainText('  打开测试窗口\n并观察  ')
    window.send_button.click()
    window.send_button.click()
    assert sent == ['打开测试窗口\n并观察']
    assert not window.new_button.isEnabled()
    assert not window.voice_button.isEnabled()
    window.set_state('等待补充', busy=False)
    window.voice_button.click()
    window.new_button.click()
    assert voice == [True] and new == [True]
    window.append('工具', '<img src="file:///secret"> & 文本')
    assert '<img src="file:///secret"> & 文本' in window.log.toPlainText()
    window.show()
    window.close()
    assert not window.isVisible()
    window.show()
    assert window.isVisible()
    window.hide()


def test_save_failure_keeps_config_and_all_task_signals():
    class FailingStore(Store):
        def save(self, value):
            raise OSError('测试磁盘不可写')

    panel = ControlPanel(FailingStore(), list)
    panel.enabled.setChecked(True)
    saved = []
    panel.settings_saved.connect(saved.append)
    panel.save_settings()
    assert not panel.config['enabled'] and not saved
    assert '保存失败' in panel.status.text()
    window = ControlWindow()
    events = []
    for name in ('pause', 'resume', 'stop'):
        getattr(window, name + '_requested').connect(lambda n=name: events.append(n))
        getattr(window, name + '_button').click()
    assert events == ['pause', 'resume', 'stop']


def test_damaged_control_config_keeps_original_ui_available(tmp_path):
    from desktop_control.config import ControlStore
    path = tmp_path / 'control.json'
    path.write_text('{broken', encoding='utf-8')
    panel = ControlPanel(ControlStore(path), list)
    assert not panel.config['enabled'] and not panel.config['cloud_enabled']
    assert '读取失败' in panel.status.text()
    assert path.read_text(encoding='utf-8') == '{broken'
    panel.close()


def test_window_model_bubble_is_plain_text_separate_from_hidden_records():
    window = ControlWindow()
    window.show()
    APP.processEvents()
    assert isinstance(window.message, QTextBrowser)
    assert not window.log.isVisible()
    text = '<img src="file:///secret"> & 正在查找目标'
    window.show_model_message(text)
    assert window.message.toPlainText() == text
    window.append('工具', '{"action":"observe"}')
    window.set_state('正在执行…', busy=True)
    assert window.message.toPlainText() == text
    window.show_model_message('已找到目标，准备下一步。')
    assert window.message.toPlainText() == '已找到目标，准备下一步。'
    assert 'observe' in window.log.toPlainText()
    window.records_toggle.click()
    APP.processEvents()
    assert window.log.isVisible()
    window.records_toggle.click()
    assert not window.log.isVisible()
    window.hide()


def test_window_enter_submits_shift_enter_adds_line_and_ctrl_enter_still_submits():
    window = ControlWindow()
    assert isinstance(window.input, QPlainTextEdit)
    sent = []
    window.submitted.connect(sent.append)
    window.show()
    window.input.setFocus()
    APP.processEvents()
    window.input.setPlainText('打开测试窗口')
    QTest.keyClick(window.input, Qt.Key.Key_Return)
    assert sent == ['打开测试窗口']
    window.set_state('等待补充')
    window.input.setPlainText('第一行')
    window.input.moveCursor(window.input.textCursor().MoveOperation.End)
    QTest.keyClick(window.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert window.input.toPlainText() == '第一行\n'
    window.input.insertPlainText('第二行')
    QTest.keyClick(window.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert sent == ['打开测试窗口', '第一行\n第二行']
    window.hide()


def test_window_card_fits_360_and_close_does_not_stop_task():
    window = ControlWindow()
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    stopped = []
    window.stop_requested.connect(lambda: stopped.append(True))
    window.resize(360, 500)
    window.show()
    window.show_model_message('很长的关键输出' * 100 + 'a' * 500)
    window.append('操作', 'b' * 1000)
    window.records_toggle.click()
    APP.processEvents()
    assert window.width() == 360
    for widget in (window.card, window.message, window.log, window.input,
                   window.send_button, window.voice_button, window.new_button):
        left = widget.mapTo(window, QPoint(0, 0)).x()
        assert 0 <= left and left + widget.width() <= window.width()
    assert window.message.horizontalScrollBar().maximum() == 0
    assert window.log.horizontalScrollBar().maximum() == 0
    window.set_state('任务执行中', busy=True)
    window.close_button.click()
    assert not window.isVisible() and window._busy and not stopped
    window.show()
    window.input.setFocus()
    APP.processEvents()
    QTest.keyClick(window.input, Qt.Key.Key_Escape)
    assert not window.isVisible() and not stopped


def test_window_drag_resize_and_render_reference_card(tmp_path):
    font = 'C:/Windows/Fonts/msyh.ttc'
    if os.path.isfile(font):
        QFontDatabase.addApplicationFont(font)
    window = ControlWindow()
    window.setFont(QFont('Microsoft YaHei', 9))
    window.move(100, 100)
    window.show()
    APP.processEvents()
    start = window.pos()
    local = QPointF(8, 8)
    global_pos = QPointF(window.title.mapToGlobal(QPoint(8, 8)))
    for kind, pos, button, buttons in (
        (QEvent.Type.MouseButtonPress, global_pos, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, global_pos + QPointF(30, 20), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, global_pos + QPointF(30, 20), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
    ):
        APP.sendEvent(window.title, QMouseEvent(kind, local, pos, button, buttons, Qt.KeyboardModifier.NoModifier))
    assert window.pos() == start + QPoint(30, 20)
    initial = window.size()
    grip = window.size_grip
    global_pos = QPointF(grip.mapToGlobal(QPoint(7, 7)))
    for kind, offset, button, buttons in (
        (QEvent.Type.MouseButtonPress, QPointF(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseMove, QPointF(40, 30), Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton),
        (QEvent.Type.MouseButtonRelease, QPointF(40, 30), Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton),
    ):
        APP.sendEvent(grip, QMouseEvent(kind, QPointF(7, 7) + offset, global_pos + offset,
            button, buttons, Qt.KeyboardModifier.NoModifier))
    # 离屏200%下虚拟屏幕仅400×300，Qt缩放把手会按可用屏幕夹取尺寸。
    assert window.size() != initial
    assert window.width() >= window.minimumWidth()
    assert window.height() >= window.minimumHeight()
    window.resize(460, 350)
    window.show_model_message('我会先查找目标窗口，确认位置后再执行下一步。\n你可以继续操作，也可以随时暂停。')
    window.set_state('正在观察目标窗口…', busy=True)
    APP.processEvents()
    assert window.grab().save(str(tmp_path / '操作窗口-默认.png'))
    window.resize(360, 380)
    window.append('工具', '{"action":"observe","target":"测试窗口"}')
    window.records_toggle.click()
    APP.processEvents()
    assert window.size().width() == 360 and window.size().height() == 380
    for widget in (window.message, window.input, window.log, window.new_button, window.close_button):
        pos = widget.mapTo(window, QPoint())
        assert pos.x() >= 0 and pos.x() + widget.width() <= window.width()
        assert pos.y() >= 0 and pos.y() + widget.height() <= window.height()
    assert window.log.geometry().bottom() < window.input.geometry().top()
    assert window.input.geometry().bottom() < window.new_button.geometry().top()
    assert window.grab().save(str(tmp_path / '操作窗口-360-展开记录.png'))
    window.hide()
