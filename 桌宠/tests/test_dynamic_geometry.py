"""启动恢复屏外旧坐标；运行中允许用户自由放置，不自动吸回。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from ui.dynamic_pet_widget import DynamicDesktopPetWidget

_APP = QApplication.instance() or QApplication([])


class Config:
    def __init__(self, values):
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)

    def save(self, changes):
        self.values.update(changes)


@pytest.fixture
def pet():
    geom = _APP.primaryScreen().availableGeometry()
    cfg = Config({"pet_pos_x": geom.right() - 52, "pet_pos_y": geom.bottom() - 52})
    widget = DynamicDesktopPetWidget(config_mgr=cfg)
    yield widget
    widget.physics_timer.stop()
    widget.hide()
    widget.bubble.hide()
    widget.interaction_dialog.hide()
    widget.deleteLater()
    _APP.processEvents()


def test_saved_position_keeps_entire_pet_on_screen(pet):
    assert pet.screen().availableGeometry().contains(pet.geometry())


def test_action_resize_and_size_setting_keep_user_position(pet):
    geom = pet.screen().availableGeometry()
    pet.move(geom.right() - 20, geom.bottom() - 20)
    position = pet.pos()
    pet.update_size(320)
    assert pet.pos() == position
    for action in pet.actions:
        pet.set_action(action)
        assert pet.pos() == position


def test_drag_release_keeps_and_saves_user_position_even_outside_screen(pet):
    geom = pet.screen().availableGeometry()
    pet.move(geom.right() + 200, geom.bottom() + 100)
    position = pet.pos()
    pet.is_dragging = True
    event = QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(20, 20), QPointF(20, 20),
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    pet.mouseReleaseEvent(event)
    assert pet.pos() == position
    assert pet.config_mgr.get("pet_pos_x") == pet.x()
    assert pet.config_mgr.get("pet_pos_y") == pet.y()


def test_show_and_screen_workarea_changes_do_not_snap_pet_back(pet):
    geom = pet.screen().availableGeometry()
    pet.move(geom.right() + 50, geom.top() - 30)
    position = pet.pos()
    pet.show()
    _APP.processEvents()
    assert pet.pos() == position
    pet.screen().availableGeometryChanged.emit(geom)
    assert pet.pos() == position
    pet.hide()
    pet.show()
    _APP.processEvents()
    assert pet.pos() == position


def test_bubble_stays_within_screen_near_bottom_right(pet):
    geom = pet.screen().availableGeometry()
    pet.move(geom.right() - pet.width() + 1, geom.bottom() - pet.height() + 1)
    x, y = pet._calc_bubble_pos(380, 180)
    from PyQt6.QtCore import QRect
    assert geom.contains(QRect(x, y, 380, 180))
