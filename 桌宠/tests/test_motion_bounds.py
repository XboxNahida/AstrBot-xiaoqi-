"""透明留白、绘制变换与负坐标屏幕边界，不采集真实桌面。"""
import pytest
from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPixmap, QRegion

from tests.test_dynamic_actions import pet as pet  # noqa: PLC0414
from tests.test_interaction_integration import connected as connected  # noqa: PLC0414


def test_alpha_padding_and_full_screen_edges(pet):
    from pet_features.motion_bounds import BodyBounds, frame_bounds
    image = QImage(200, 100, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    for x in range(40, 100):
        for y in range(20, 80):
            image.setPixelColor(x, y, QColor("red"))
    pet.feature_resource_pixmap = QPixmap.fromImage(image)
    pet.scale_x_ease = pet.scale_y_ease = 1
    pet.tilt_angle = 0
    body = BodyBounds().rect(pet)
    assert body.left() > 15 and body.top() > 15
    assert body.width() < pet.width() / 2
    screen = QRect(-1280, -800, 1280, 800)
    left, top, right, bottom = frame_bounds(screen, body)
    assert left + body.left() == pytest.approx(-1280)
    assert top + body.top() == pytest.approx(-800)
    assert right + body.right() == pytest.approx(0)
    assert bottom + body.bottom() == pytest.approx(0)


def test_visible_bounds_match_actual_transformed_paint(pet):
    from pet_features.motion_bounds import BodyBounds
    image = QImage(100, 100, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    for x in range(20, 60):
        for y in range(30, 80):
            image.setPixelColor(x, y, QColor("red"))
    pet.feature_resource_pixmap = QPixmap.fromImage(image)
    pet.feature_flipped = True
    pet.feature_squash = .12
    pet.tilt_angle = 8
    pet.scale_x_ease = pet.scale_y_ease = 1
    pet.show()
    actual = QRegion(pet.grab().mask()).boundingRect()
    calculated = BodyBounds().rect(pet)
    for actual_edge, calculated_edge in zip(
            (actual.x(), actual.y(), actual.x() + actual.width(), actual.y() + actual.height()),
            (calculated.left(), calculated.top(), calculated.right(), calculated.bottom())):
        assert abs(actual_edge - calculated_edge) <= 3


@pytest.mark.parametrize("angle", [0, 8, 22])
def test_real_irregular_character_matches_transformed_pixels(pet, angle):
    from pet_features.motion_bounds import BodyBounds
    pet.tilt_angle = angle
    pet.feature_squash = .12
    pet.feature_flipped = True
    pet.scale_x_ease = pet.scale_y_ease = 1
    pet.show()
    actual = QRegion(pet.grab().mask()).boundingRect()
    calculated = BodyBounds().rect(pet)
    for actual_edge, calculated_edge in zip(
            (actual.x(), actual.y(), actual.x() + actual.width(), actual.y() + actual.height()),
            (calculated.left(), calculated.top(), calculated.right(), calculated.bottom())):
        assert abs(actual_edge - calculated_edge) <= 3


def test_bounds_cache_is_bounded_and_contains_no_pixmaps(pet):
    from pet_features.motion_bounds import BodyBounds
    bounds = BodyBounds()
    for frame in range(100):
        pet.current_frame = frame % pet.actions[pet.current_action_key]["count"]
        bounds.rect(pet)
    assert len(bounds.cache) <= 32
    assert all(isinstance(value, QRectF) for value in bounds.cache.values())


def test_runtime_bounds_use_visible_body_and_entire_screen(connected):
    from pet_features.motion_bounds import BodyBounds, frame_bounds
    runtime, pet, _panel = connected
    body = BodyBounds().rect(pet)
    assert runtime.interaction._bounds() == pytest.approx(frame_bounds(pet.screen().geometry(), body))


def test_throw_past_screen_edge_is_not_cancelled_or_teleported(connected):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    bounds = physics._bounds()
    pet.move(round(bounds[2]) + 5, round((bounds[1] + bounds[3]) / 2))
    before = pet.pos()
    physics._launch((1000, 0), physics.epoch)
    assert physics.motion.active and pet.pos() == before
    physics.tick(.016)
    assert physics.motion.vx < 0 and abs(pet.x() - before.x()) < 30
