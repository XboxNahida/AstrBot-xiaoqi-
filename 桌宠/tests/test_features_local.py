"""本地窗口工具装配和外观默认不改变原程序。"""
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication, QWidget

from pet_features.config import FeaturesStore
from pet_features.runtime import FeatureRuntime
from ui.features_settings import FeaturesSettingsPanel

APP = QApplication.instance() or QApplication([])


def test_rescue_and_appearance_original_defaults(tmp_path):
    pet = QWidget()
    pet.move(123, 234)
    window = QWidget()
    window.setStyleSheet("QWidget {color: red;}")
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    controller = SimpleNamespace(pet=pet, settings_win=window, speaker=SimpleNamespace(config={}),
                                 smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=False)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    try:
        runtime.configure_local()
        assert pet.pos().x() == 123
        assert window.styleSheet() == "QWidget {color: red;}"
        runtime.request("tools:rescue")
        assert runtime.tools is not None
        assert pet.isVisible()
    finally:
        runtime.close()
        pet.hide()
        panel.hide()
        window.hide()


def test_window_size_remembered_only_when_enabled(tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    pet, window = QWidget(), QWidget()
    controller = SimpleNamespace(pet=pet, settings_win=window, speaker=SimpleNamespace(config={}),
                                 smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=False)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    runtime.cfg["appearance"]["enabled"] = True
    window.resize(900, 700)
    try:
        runtime.close()
        cfg = panel.store.load()
        assert (cfg["appearance"]["width"], cfg["appearance"]["height"]) == (900, 700)
    finally:
        window.hide()
        pet.hide()
        panel.hide()


def test_click_sound_is_debounced_and_uses_shared_speaker(tmp_path):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    played = []
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    pet, window = QWidget(), QWidget()
    speaker = SimpleNamespace(config={}, play_local_file=lambda p, v: played.append((p, v)) or True)
    controller = SimpleNamespace(pet=pet, settings_win=window, speaker=speaker,
                                 smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=False)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    runtime.cfg["sound"].update(enabled=True, path="fake.wav")
    try:
        for _ in range(5):
            QApplication.sendEvent(pet, QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(2, 2), QPointF(2, 2),
                Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
        assert played == [("fake.wav", 40)]
    finally:
        runtime.close()
        pet.hide()
        window.hide()
        panel.hide()
