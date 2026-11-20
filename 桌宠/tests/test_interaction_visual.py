"""透明交互表现层的隔离验证，不移动真实鼠标。"""
import os
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication, QWidget

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def scene():
    from pet_features.interaction_visual import InteractionVisual
    pet = QWidget()
    pet.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
    pet.resize(80, 100)
    pet.move(-400, -200)
    pet.feature_sleeping = False
    interaction = SimpleNamespace(
        pet=pet, cfg={"enabled": True, "trail_enabled": True, "sling_enabled": True},
        _middle=None, _start=None, _moved=False, _target=None,
        motion=SimpleNamespace(active=False, vx=0., vy=0.), clock=lambda: 1.,
    )
    visual = InteractionVisual(pet)
    yield pet, interaction, visual
    visual.close()
    pet.close()


def test_idle_hidden_and_never_accepts_input(scene):
    _pet, interaction, visual = scene
    visual.advance(.016, interaction)
    assert not visual.isVisible()
    assert visual.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert visual.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    for attribute in (Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                      Qt.WidgetAttribute.WA_ShowWithoutActivating,
                      Qt.WidgetAttribute.WA_TranslucentBackground):
        assert visual.testAttribute(attribute)


def test_trail_distance_speed_capacity_and_expiry(scene):
    pet, interaction, visual = scene
    interaction.motion.active = True
    interaction.motion.vx = 500
    visual.advance(.01, interaction)
    count = len(visual.points)
    pet.move(pet.x() + 5, pet.y())
    visual.advance(.01, interaction)
    assert len(visual.points) == count
    for _ in range(40):
        pet.move(pet.x() + 15, pet.y())
        visual.advance(.001, interaction)
    assert 0 < len(visual.points) <= 24
    assert all(0 < point[2] <= .4 for point in visual.points)
    interaction.motion.active = False
    visual.advance(.41, interaction)
    assert visual.points == [] and not visual.isVisible()
    interaction.motion.active, interaction.motion.vx = True, 100
    visual.advance(.01, interaction)
    assert visual.points == []


def test_sling_negative_screen_geometry_and_clear(scene):
    pet, interaction, visual = scene
    anchor = QPoint(-600, -350)
    interaction._middle = (anchor, pet.pos())
    interaction._moved = True
    visual.advance(.016, interaction)
    center = pet.mapToGlobal(pet.rect().center())
    assert visual.sling == ((anchor.x(), anchor.y()), (center.x(), center.y()))
    assert visual.geometry().contains(anchor)
    assert visual.geometry().contains(center)
    assert visual.width() < 400 and visual.height() < 300
    assert visual.isVisible()
    visual.clear()
    assert visual.points == [] and visual.sling is None and not visual.isVisible()
    interaction.cfg["sling_enabled"] = False
    visual.advance(.016, interaction)
    assert visual.sling is None and not visual.isVisible()


def test_sleep_and_disabled_clear(scene):
    pet, interaction, visual = scene
    pet.feature_sleeping = True
    visual.advance(.016, interaction)
    assert visual.sleep and visual.isVisible()
    interaction.cfg["enabled"] = False
    visual.advance(.016, interaction)
    assert not visual.sleep and not visual.isVisible()


def test_drag_trail_and_paint_are_bounded_and_close_is_final(scene):
    pet, interaction, visual = scene
    interaction._start, interaction._moved = QPoint(-400, -200), True
    visual.advance(.02, interaction)
    pet.move(pet.x() + 20, pet.y())
    visual.advance(.02, interaction)
    assert len(visual.points) == 1
    assert visual.size().width() == 15 and visual.size().height() == 15
    image = QImage(visual.size(), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    visual.render(image)
    color = image.pixelColor(7, 7)
    assert color.blue() > color.red() and color.alpha() > 0
    visual.close()
    visual.advance(.02, interaction)
    assert not visual.isVisible() and visual.points == []
