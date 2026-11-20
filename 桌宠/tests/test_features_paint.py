"""可选静态资源与朝向只改变人物绘制，不修改位置或原素材。"""
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QImage, QPixmap

from tests.test_dynamic_actions import pet as pet  # noqa: PLC0414


def test_manual_mirror_survives_temporary_running_direction(pet):
    pet.manual_mirrored = True
    assert pet._body_transform().m11() < 0
    pet.begin_desktop_move(-200)
    assert pet._body_transform().m11() > 0
    pet.end_desktop_move()
    assert pet._body_transform().m11() < 0
    pet.feature_flipped = True
    assert pet._body_transform().m11() > 0


def test_classic_manual_mirror_draws_without_changing_source():
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QLabel, QWidget

    from pet_features.runtime_local import LocalFeatures
    classic = QWidget()
    classic.avatar_label = QLabel(classic)
    classic.avatar_label.resize(20, 20)
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(QColor('red'))
    for x in range(10, 20):
        for y in range(20):
            image.setPixelColor(x, y, QColor('blue'))
    source = QPixmap.fromImage(image)
    classic.avatar_label.setPixmap(source)
    local = LocalFeatures()
    local.controller = SimpleNamespace(pet=classic)
    try:
        classic.manual_mirrored = True
        local._classic_overlay()
        assert local.resource_overlay.pixmap().toImage().pixelColor(2, 10).blue() > 200
        assert classic.avatar_label.pixmap().toImage() == source.toImage()
        classic.manual_mirrored = False
        local._classic_overlay()
        assert local.resource_overlay.isHidden()
    finally:
        classic.deleteLater()


def test_classic_transparent_mirror_has_one_outline_and_tracks_hover():
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QLabel, QWidget

    from pet_features.motion_bounds import BodyBounds
    from pet_features.runtime_local import LocalFeatures

    classic = QWidget()
    classic.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    classic.resize(20, 30)
    classic.avatar_label = QLabel(classic)
    classic.avatar_label.setGeometry(0, 5, 20, 20)
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    for x in range(10, 20):
        for y in range(20):
            image.setPixelColor(x, y, QColor('red'))
    classic.avatar_label.setPixmap(QPixmap.fromImage(image))
    classic.manual_mirrored = True
    local = LocalFeatures()
    local.controller = SimpleNamespace(pet=classic)
    try:
        local._classic_overlay()
        canvas = QPixmap(classic.size())
        canvas.fill(Qt.GlobalColor.transparent)
        classic.render(canvas)
        assert canvas.toImage().pixelColor(2, 10).alpha() == 255
        assert canvas.toImage().pixelColor(17, 10).alpha() == 0
        assert BodyBounds().rect(classic).left() == 0
        classic.avatar_label.move(0, 8)
        local._classic_overlay()
        assert local.resource_overlay.pos() == classic.avatar_label.pos()
        classic.manual_mirrored = False
        local._classic_overlay()
        assert not classic.avatar_label.isHidden() and local.resource_overlay.isHidden()
    finally:
        classic.deleteLater()


def test_custom_static_resource_and_flip_are_drawn(pet):
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    for x in range(10, 20):
        for y in range(20):
            image.setPixelColor(x, y, QColor("blue"))
    pet.feature_resource_pixmap = QPixmap.fromImage(image)
    pet.scale_x_ease = pet.scale_y_ease = 1
    pet.tilt_angle = 0
    pos = pet.pos()
    pet.show()
    canvas = QPixmap(pet.size())
    canvas.fill(Qt.GlobalColor.transparent)
    pet.render(canvas)
    painted = canvas.toImage()
    x, y = pet.width() // 4, pet.height() // 2
    assert painted.pixelColor(x, y).red() > 200
    pet.feature_flipped = True
    pet.render(canvas)
    assert canvas.toImage().pixelColor(x, y).blue() > 200
    assert pet.pos() == pos


def test_squash_changes_pixels_without_moving_or_resizing_pet(pet):
    pet.scale_x_ease = pet.scale_y_ease = 1
    pet.tilt_angle = 0
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    pet.feature_resource_pixmap = QPixmap.fromImage(image)
    pet.show()
    before = pet.geometry()
    normal = QPixmap(pet.size())
    normal.fill(Qt.GlobalColor.transparent)
    pet.render(normal)
    pet.feature_squash = .16
    squashed = QPixmap(pet.size())
    squashed.fill(Qt.GlobalColor.transparent)
    pet.render(squashed)
    assert normal.toImage() != squashed.toImage()
    assert pet.geometry() == before
