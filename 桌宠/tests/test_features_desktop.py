"""离屏窗口行为与隔离资源库验收，不操控实际桌面。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget


@pytest.fixture
def pet():
    app = QApplication.instance() or QApplication([])
    widget = QWidget()
    widget.resize(60, 80)
    widget.move(100, 100)
    widget.show()
    yield widget
    widget.close()
    app.processEvents()


def test_stationary_never_moves_and_disabled_restores_flags(pet):
    from pet_features.desktop_tools import DesktopTools
    tools = DesktopTools(pet)
    start = pet.pos()
    tools.apply({"enabled": True, "mode": "stationary", "snap": True})
    tools.tick(1)
    assert pet.pos() == start
    tools.apply({"enabled": True, "locked": True, "click_through": True, "opacity": 30, "layer": "bottom"})
    assert pet.windowOpacity() == pytest.approx(.3, abs=.01)
    assert pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    tools.rescue()
    assert not pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    assert pet.windowOpacity() == 1
    assert not tools.cfg["locked"]
    tools.close()


def test_walk_yields_to_user_and_stops_at_current_position(pet):
    from pet_features.desktop_tools import DesktopTools
    tools = DesktopTools(pet)
    tools.apply({"enabled": True, "mode": "walk", "speed": 40})
    start = pet.pos()
    tools.tick(1)
    assert pet.pos() != start
    for attr, value in [("is_dragging", True), ("_menu_open", True), ("_thinking_active", True), ("_loop_override", True)]:
        setattr(pet, attr, value)
        pos = pet.pos()
        tools.tick(1)
        assert pet.pos() == pos
        setattr(pet, attr, None if attr == "_loop_override" else False)
    tools.apply({"enabled": False})
    pos = pet.pos()
    tools.tick(1)
    assert pet.pos() == pos
    tools.close()


def test_lock_filters_only_drag_motion(pet):
    from pet_features.desktop_tools import DesktopTools
    tools = DesktopTools(pet)
    tools.apply({"enabled": True, "locked": True})
    for kind, consumed in [(QEvent.Type.MouseButtonPress, False), (QEvent.Type.MouseMove, True), (QEvent.Type.MouseButtonRelease, False), (QEvent.Type.MouseButtonDblClick, False)]:
        event = QMouseEvent(kind, QPoint(4, 4).toPointF(), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        assert tools.eventFilter(pet, event) is consumed
    tools.close()


def test_resource_import_is_validated_persistent_and_cannot_delete_original(tmp_path, pet):
    from pet_features.resources import ResourceLibrary
    source = tmp_path / "原素材.png"
    image = QImage(10, 12, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(source))
    library = ResourceLibrary(tmp_path / "用户资源")
    item = library.import_image(source)
    assert library.pixmap(item["id"]).size() == image.size()
    assert ResourceLibrary(library.root).list()[0]["id"] == item["id"]
    assert ResourceLibrary(library.root).list()[0]["name"] == "原素材"
    with pytest.raises(ValueError):
        library.remove("../原素材.png")
    bad = tmp_path / "坏图.png"
    bad.write_bytes(b"bad")
    with pytest.raises(ValueError):
        library.import_image(bad)
    library.remove(item["id"])
    assert library.list() == [] and source.exists()


def test_follow_snap_and_flip_are_opt_in(pet, monkeypatch):
    from PyQt6.QtGui import QCursor

    from pet_features.desktop_tools import DesktopTools
    tools = DesktopTools(pet)
    monkeypatch.setattr(QCursor, "pos", lambda: QPoint(-200, -200))
    tools.apply({"enabled": True, "mode": "follow", "speed": 100})
    tools.tick(1)
    assert pet.x() < 100 and pet.y() < 100
    assert not pet.feature_flipped
    tools.apply({"enabled": True, "mode": "follow", "speed": 1000, "snap": True, "flip": True})
    tools.tick(1)
    assert pet.x() >= pet.screen().availableGeometry().left()
    assert pet.feature_flipped
    before = pet.pos()
    tools.close()
    assert pet.pos() == before and not pet.feature_flipped


def test_resource_limits_and_symlink_escape(tmp_path, pet):
    from pet_features.resources import ResourceLibrary
    library = ResourceLibrary(tmp_path / "库")
    source = tmp_path / "图.png"
    image = QImage(10, 10, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.blue)
    image.save(str(source))
    library.MAX_PIXELS = 50
    with pytest.raises(ValueError, match="像素"):
        library.import_image(source)
    library.MAX_PIXELS = 100
    library.MAX_BYTES = 1
    with pytest.raises(ValueError, match="20MB"):
        library.import_image(source)
    with pytest.raises(ValueError):
        library.path("a" * 32 + "/../original")


def test_zero_opacity_and_tray_recovery(pet):
    from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

    from pet_features.desktop_tools import DesktopTools
    tray = QSystemTrayIcon()
    menu = QMenu()
    tray.setContextMenu(menu)
    tools = DesktopTools(pet, tray)
    tools.apply({"enabled": True, "opacity": 0, "click_through": True})
    assert pet.windowOpacity() == 0
    menu.actions()[-1].trigger()
    assert pet.windowOpacity() == 1
    assert not pet.windowFlags() & Qt.WindowType.WindowTransparentForInput
    tools.close()


def roaming(pet):
    from pet_features.desktop_tools import DesktopTools
    clock = [100.]

    class FixedRandom:
        def uniform(self, low, high):
            return (low + high) / 2

    tools = DesktopTools(pet, clock=lambda: clock[0], rng=FixedRandom())
    tools.apply({"enabled": True, "mode": "walk", "speed": 40})
    return tools, clock


def test_walk_reaches_two_dimensional_target_and_rests(pet):
    tools, clock = roaming(pet)
    start = pet.pos()
    tools.tick(1)
    assert pet.x() != start.x() and pet.y() != start.y()
    for _ in range(50):
        tools.tick(1)
    arrived = pet.pos()
    area = pet.screen().availableGeometry()
    assert area.contains(pet.geometry())
    clock[0] += 7
    tools.tick(1)
    assert pet.pos() == arrived
    assert 8 <= tools._rest_until - 100 <= 18
    tools.close()


def test_user_press_cancels_route_and_release_rests_without_clamping(pet):
    tools, clock = roaming(pet)
    tools.tick(1)
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        event = QMouseEvent(kind, QPoint(4, 4).toPointF(), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        assert tools.eventFilter(pet, event) is False
    pet.move(-300, -200)
    manual = pet.pos()
    clock[0] += 5
    tools.tick(1)
    assert pet.pos() == manual
    assert tools._target is None
    assert 6 <= tools._rest_until - 100 <= 14
    clock[0] += 10
    tools.tick(.1)
    assert abs(pet.x() - manual.x()) <= 4 and abs(pet.y() - manual.y()) <= 4
    tools.close()


@pytest.mark.parametrize("priority", ["feature_motion_active", "feature_sleeping"])
def test_walk_yields_to_physics_and_sleep(pet, priority):
    tools, _ = roaming(pet)
    setattr(pet, priority, True)
    start = pet.pos()
    tools.tick(1)
    assert pet.pos() == start
    tools.close()


def test_slow_walk_accumulates_fractional_steps_and_tracks_bubble(pet):
    tools, _ = roaming(pet)
    tools.apply({"enabled": True, "mode": "walk", "speed": 1})
    pet.bubble = QWidget()
    pet._calc_bubble_pos = lambda w, h: (pet.x() + 9, pet.y() + 11)
    start = pet.pos()
    for _ in range(30):
        tools.tick(.1)
    assert pet.pos() != start
    assert pet.bubble.pos() == pet.pos() + QPoint(9, 11)
    stopped = pet.pos()
    tools.apply({"enabled": False})
    tools.tick(1)
    assert pet.pos() == stopped
    tools.close()
    pet.bubble.close()


def test_rescue_stops_old_flight_and_wakes_sleep(pet):
    tools, _ = roaming(pet)

    class Interaction:
        def stop(self):
            pet.feature_motion_active = False

        def wake(self):
            pet.feature_sleeping = False

    pet.feature_interaction = Interaction()
    pet.feature_motion_active = pet.feature_sleeping = True
    tools.rescue()
    assert not pet.feature_motion_active and not pet.feature_sleeping
    tools.close()


@pytest.mark.parametrize("cancel", [QEvent.Type.UngrabMouse, QEvent.Type.Hide, QEvent.Type.WindowDeactivate])
def test_lost_mouse_state_does_not_permanently_stop_roaming(pet, cancel):
    tools, clock = roaming(pet)
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(4, 4).toPointF(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tools.eventFilter(pet, press)
    tools.eventFilter(pet, QEvent(cancel))
    clock[0] += 15
    start = pet.pos()
    tools.tick(.1)
    assert pet.pos() != start
    tools.close()


def test_context_menu_without_release_does_not_latch_pressed(pet):
    tools, clock = roaming(pet)
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(4, 4).toPointF(),
        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    tools.eventFilter(pet, press)
    pet._menu_open = True
    start = pet.pos()
    tools.tick(.1)
    assert pet.pos() == start
    pet._menu_open = False
    clock[0] += 15
    tools.tick(.1)
    assert pet.pos() != start
    tools.close()
