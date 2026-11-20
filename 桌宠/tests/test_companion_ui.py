import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from companion_config import SettingsStore
from ui.companion_settings import CompanionSettings


def test_profile_fill_save_and_switch_survives_restart(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    window = CompanionSettings(store)
    window.name_edit.setText("日常模型")
    window.base_edit.setText("https://example.com/v1")
    window.key_edit.setText("fake-ui-key")
    window.model_combo.setEditText("daily-model")
    window.save_settings()
    assert store.load()["profiles"][0]["name"] == "日常模型"
    window.add_profile()
    window.name_edit.setText("备用模型")
    window.model_combo.setEditText("backup-model")
    window.save_settings()
    active = store.load()["active_profile"]
    assert active != "default"
    window.profile_combo.setCurrentIndex(0)
    assert window.model_combo.currentText() == "daily-model"
    window.save_settings()
    assert store.load()["active_profile"] == "default"
    assert "fake-ui-key" not in store.path.read_text(encoding="utf-8")
    window.close()
    app.processEvents()


def test_import_pairing_populates_connection_without_changing_model(tmp_path):
    import base64
    import json

    app = QApplication.instance() or QApplication([])
    window = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    code = base64.urlsafe_b64encode(
        json.dumps(
            {
                "url": "wss://example.com/desktop/ws",
                "device_id": "desk-one",
                "token": "fake-token",
            }
        ).encode()
    ).decode()
    window.pair_edit.setPlainText("desktop-link:" + code)
    window.import_pairing()
    assert window.cloud_url.text() == "wss://example.com/desktop/ws"
    assert window.device_id.text() == "desk-one"
    assert window.cloud_token.text() == "fake-token"
    assert window.pair_edit.toPlainText() == ""
    window.close()
    app.processEvents()


def test_empty_pairing_prefix_gives_hint_instead_of_crashing(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    window.pair_edit.setPlainText("desktop-link:")
    window.import_pairing()
    assert "不正确" in window.feedback.text()
    window.hide()
    app.processEvents()


def test_right_mouse_press_uses_companion_settings_menu(tmp_path, monkeypatch):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QContextMenuEvent, QMouseEvent
    from PyQt6.QtWidgets import QMenu

    from companion_main import CompanionPet
    from ui.dynamic_pet_widget import DynamicDesktopPetWidget

    app = QApplication.instance() or QApplication([])
    pet = CompanionPet(appearance_path=tmp_path / "appearance.json")
    window = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    window.showMinimized()
    app.processEvents()
    opened, menus, styles = [], [], []
    pet.open_settings.connect(lambda: opened.append(True))
    pet.open_settings.connect(window.open_window)

    def select_settings(menu, position):
        menus.append([action.text() for action in menu.actions()])
        styles.append(menu.styleSheet())
        next(action for action in menu.actions() if "设置中心" in action.text()).trigger()

    monkeypatch.setattr(QMenu, "exec", select_settings)
    try:
        DynamicDesktopPetWidget._show_context_menu(pet, pet.rect().center())
        original_items, original_style = menus.pop(), styles.pop()
        opened.clear()
        window.showMinimized()
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(20, 20),
            QPointF(20, 20),
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        )
        pet.mousePressEvent(event)
        pet.contextMenuEvent(
            QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pet.rect().center())
        )
        app.processEvents()
        assert len(menus) == 1
        assert [item for item in menus[0] if item in original_items] == original_items
        assert "☁️ 云端连接与模型" in menus[0]
        assert styles[0] == original_style
        assert opened == [True]
        assert window.isVisible()
        assert not window.isMinimized()
    finally:
        window.hide()
        window.deleteLater()
        pet.physics_timer.stop()
        pet.hide()
        pet.bubble.hide()
        pet.interaction_dialog.hide()
        pet.deleteLater()
        app.processEvents()


def test_reopen_settings_restores_minimized_and_hidden_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    try:
        window.showMinimized()
        app.processEvents()
        assert window.isMinimized()
        window.open_window()
        app.processEvents()
        assert window.isVisible()
        assert not window.isMinimized()
        window.hide()
        window.open_window()
        app.processEvents()
        assert window.isVisible()
        assert not window.isMinimized()
    finally:
        window.hide()
        window.deleteLater()
        app.processEvents()


def test_windows_hidden_launcher_can_reopen_settings_natively(tmp_path):
    import json
    import subprocess
    import sys

    import pytest

    if sys.platform != "win32":
        pytest.skip("验证Windows隐藏启动标志与真实窗口状态")
    code = '''
import ctypes,json,sys
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from companion_config import SettingsStore
from ui.companion_settings import CompanionSettings
app=QApplication([])
window=CompanionSettings(SettingsStore(Path(sys.argv[1])/'settings.json'))
window.show()
user32=ctypes.WinDLL('user32',use_last_error=True)
user32.IsWindowVisible.argtypes=[ctypes.c_void_p]
user32.IsWindowVisible.restype=ctypes.c_bool
results={}
def open_settings():
    results['before_qt']=window.isVisible()
    results['before_native']=user32.IsWindowVisible(int(window.winId()))
    window.open_window()
    QTimer.singleShot(100,finish)
def finish():
    results['after_qt']=window.isVisible()
    results['after_native']=user32.IsWindowVisible(int(window.winId()))
    (Path(sys.argv[1])/'visibility.json').write_text(json.dumps(results),encoding='utf-8')
    app.exit(0)
QTimer.singleShot(100,open_settings)
app.exec()
'''
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "windows"},
        startupinfo=startup,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads((tmp_path / "visibility.json").read_text(encoding="utf-8"))
    assert result["before_qt"] is True
    assert result["before_native"] is False
    assert result["after_qt"] is True
    assert result["after_native"] is True


def test_perception_controls_save_and_reload(tmp_path):
    app = QApplication.instance() or QApplication([])
    store = SettingsStore(tmp_path / "settings.json")
    window = CompanionSettings(store)
    try:
        assert window.auto_check.isChecked() is False
        assert window.screenshot_check.isChecked() is False
        window.auto_check.setChecked(True)
        window.screenshot_check.setChecked(True)
        window.cooldown_spin.setValue(90)
        window.save_settings()
        loaded = store.load()
        assert loaded["auto_mode"] is True
        assert loaded["critique_screenshot"] is True
        assert loaded["critique_cooldown"] == 90
        reopened = CompanionSettings(store)
        assert reopened.auto_check.isChecked() is True
        assert reopened.cooldown_spin.value() == 90
        reopened.deleteLater()
    finally:
        window.deleteLater()
        app.processEvents()
