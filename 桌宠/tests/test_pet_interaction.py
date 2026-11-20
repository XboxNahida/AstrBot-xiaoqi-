"""隔离Qt事件验证甩抛与原点击共存，不操作鼠标。"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

APP = QApplication.instance() or QApplication([])


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.resize(80, 80)
        self.move(200, 200)
        self.cycles = self.releases = 0
        self.actions = {"idle": {"title": "待机"}, "sleep": {"title": "睡觉"}}
        self.current_action_key = "idle"
        self._loop_override = None

    def mouseReleaseEvent(self, event):
        self.releases += 1

    def cycle_next_outfit(self):
        self.cycles += 1

    def play_action(self, key, loop=False):
        self.current_action_key, self._loop_override = key, loop

    def set_action(self, key):
        self.current_action_key, self._loop_override = key, None


@pytest.fixture
def setup_pet():
    from pet_features.interaction import PetInteraction
    pet = Pet()
    pet.show()
    now = [1.0]
    interaction = PetInteraction(pet, clock=lambda: now[0])
    interaction.configure({"enabled": True, "rope_enabled": False})
    interaction.timer.stop()
    yield pet, interaction, now
    interaction.close()
    pet.close()
    APP.processEvents()


def send(pet, kind, x, y, button=Qt.MouseButton.LeftButton, buttons=None):
    buttons = (Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button) if buttons is None else buttons
    event = QMouseEvent(kind, QPointF(10, 10), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pet, event)


def test_drag_release_launches_but_original_release_still_runs(setup_pet):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 260, 220)
    now[0] += .02
    send(pet, QEvent.Type.MouseButtonRelease, 280, 220)
    APP.processEvents()
    assert pet.releases == 1 and interaction.motion.active
    before = pet.pos()
    interaction.tick(.016)
    assert pet.x() > before.x()
    send(pet, QEvent.Type.MouseButtonPress, 280, 220)
    assert not interaction.motion.active


def test_slow_release_and_manual_offscreen_placement_do_not_snap(setup_pet):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 240, 210)
    now[0] += .5
    pet.move(-300, -200)
    send(pet, QEvent.Type.MouseButtonRelease, 240, 210)
    APP.processEvents()
    for _ in range(10):
        interaction.tick(.016)
    assert not interaction.motion.active and pet.pos().x() == -300


def test_five_clicks_and_double_click_are_not_swallowed(setup_pet):
    pet, interaction, now = setup_pet
    for index in range(5):
        kind = QEvent.Type.MouseButtonDblClick if index == 1 else QEvent.Type.MouseButtonPress
        send(pet, kind, 210, 210)
        send(pet, QEvent.Type.MouseButtonRelease, 210, 210)
        now[0] += .1
    APP.processEvents()
    assert pet.releases == 5 and not interaction.motion.active


def test_middle_click_cycles_and_drag_launches_sling(setup_pet):
    pet, interaction, _now = setup_pet
    middle = Qt.MouseButton.MiddleButton
    send(pet, QEvent.Type.MouseButtonPress, 210, 210, middle)
    send(pet, QEvent.Type.MouseButtonRelease, 210, 210, middle)
    assert pet.cycles == 1
    send(pet, QEvent.Type.MouseButtonPress, 210, 210, middle)
    send(pet, QEvent.Type.MouseMove, 270, 210, middle)
    send(pet, QEvent.Type.MouseButtonRelease, 270, 210, middle)
    APP.processEvents()
    assert pet.cycles == 1 and interaction.motion.active and interaction.motion.vx < 0


def test_disable_and_close_cancel_queued_release(setup_pet):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 270, 210)
    send(pet, QEvent.Type.MouseButtonRelease, 270, 210)
    interaction.configure({"enabled": False})
    APP.processEvents()
    assert not interaction.motion.active
    interaction.close()
    interaction.tick(.02)
    assert not interaction.timer.isActive()


def test_sleep_wakes_on_click_or_model_thinking(setup_pet):
    pet, interaction, _now = setup_pet
    interaction.sleep()
    assert pet.feature_sleeping and pet.current_action_key == "sleep"
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    assert not pet.feature_sleeping
    send(pet, QEvent.Type.MouseButtonRelease, 210, 210)
    interaction.sleep()
    pet._thinking_active = True
    pet.current_action_key = "thinking"
    interaction.tick(.016)
    assert not pet.feature_sleeping and pet.current_action_key == "thinking"


def test_rope_drag_moves_without_clamping_and_does_not_steal_click(setup_pet):
    pet, interaction, _now = setup_pet
    interaction.configure({"enabled": True, "rope_enabled": True})
    interaction.timer.stop()
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    send(pet, QEvent.Type.MouseMove, -210, 110)
    for _ in range(80):
        interaction.tick(.016)
    assert pet.x() < 0
    send(pet, QEvent.Type.MouseButtonRelease, -210, 110)
    APP.processEvents()
    assert pet.releases == 1


@pytest.mark.parametrize('state', ['thinking', 'dialog', 'menu'])
def test_conversation_and_menu_preserve_flight(setup_pet, state):
    pet, interaction, _now = setup_pet
    interaction.motion.launch(200, 200, 800, 0)
    dialog = QWidget()
    if state == 'thinking':
        pet._thinking_active = True
    elif state == 'menu':
        pet._menu_open = True
    else:
        pet.interaction_dialog = dialog
        dialog.show()
    try:
        before = pet.x()
        interaction.tick(.016)
        assert interaction.motion.active and pet.x() > before
    finally:
        dialog.close()


def test_release_ungrab_does_not_cancel_launch_but_mid_drag_cancel_does(setup_pet):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 270, 210)
    now[0] += .02
    send(pet, QEvent.Type.MouseButtonRelease, 290, 210)
    QApplication.sendEvent(pet, QEvent(QEvent.Type.UngrabMouse))
    APP.processEvents()
    assert interaction.motion.active
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    send(pet, QEvent.Type.MouseMove, 270, 210)
    QApplication.sendEvent(pet, QEvent(QEvent.Type.UngrabMouse))
    assert interaction._start is None and not interaction.motion.active


@pytest.mark.parametrize("gravity", [False, True])
def test_deactivate_preserves_flight_velocity_and_physics(setup_pet, gravity):
    pet, interaction, _now = setup_pet
    interaction.cfg.update(gravity_enabled=gravity, friction=1., restitution=.65)
    interaction._launch((800, 100), interaction.epoch)
    before = pet.pos()
    QApplication.sendEvent(pet, QEvent(QEvent.Type.WindowDeactivate))
    assert interaction.motion.active and pet.feature_motion_active
    assert (interaction.motion.vx, interaction.motion.vy) == (800, 100)
    interaction.tick(.016)
    assert pet.x() > before.x() and interaction.motion.active
    assert interaction.motion.vy > 100 if gravity else interaction.motion.vy == 100
    bounds = interaction._bounds()
    interaction.motion.x = bounds[2] - 1
    interaction.tick(.016)
    assert interaction.motion.vx < 0 and interaction.motion.active


@pytest.mark.parametrize("button", [Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton])
def test_deactivate_after_release_preserves_queued_launch(setup_pet, button):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210, button)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 290, 210, button)
    now[0] += .02
    send(pet, QEvent.Type.MouseButtonRelease, 310, 210, button)
    QApplication.sendEvent(pet, QEvent(QEvent.Type.WindowDeactivate))
    APP.processEvents()
    assert interaction.motion.active
    assert not pet.feature_motion_input


@pytest.mark.parametrize("button", [Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton])
def test_deactivate_during_drag_clears_input_without_launch(setup_pet, button):
    pet, interaction, now = setup_pet
    send(pet, QEvent.Type.MouseButtonPress, 210, 210, button)
    now[0] += .05
    send(pet, QEvent.Type.MouseMove, 290, 210, button)
    QApplication.sendEvent(pet, QEvent(QEvent.Type.WindowDeactivate))
    send(pet, QEvent.Type.MouseButtonRelease, 310, 210, button)
    APP.processEvents()
    assert interaction._start is interaction._target is interaction._middle is None
    assert not interaction.motion.active and not pet.feature_motion_input
    assert not pet.is_dragging


def test_hide_still_stops_active_flight(setup_pet):
    pet, interaction, _now = setup_pet
    interaction._launch((800, 100), interaction.epoch)
    pet.hide()
    assert not interaction.motion.active and not pet.feature_motion_active


def test_auto_sleep_and_optional_gravity_are_local(setup_pet):
    pet, interaction, now = setup_pet
    interaction.configure({"enabled": True, "auto_sleep": True, "sleep_minutes": 1})
    interaction.timer.stop()
    now[0] += 61
    interaction.tick(.02)
    assert pet.feature_sleeping
    interaction.wake()
    interaction.cfg["gravity_enabled"] = True
    interaction._launch((0, 0), interaction.epoch)
    assert not interaction.motion.active
    interaction._launch((700, 0), interaction.epoch)
    assert interaction.motion.active
    before = pet.y()
    for _ in range(20):
        interaction.tick(.016)
    assert pet.y() > before


def test_quick_normal_placement_does_not_start_gravity(setup_pet):
    pet, interaction, now = setup_pet
    interaction.cfg["gravity_enabled"] = True
    send(pet, QEvent.Type.MouseButtonPress, 210, 210)
    now[0] += .1
    send(pet, QEvent.Type.MouseMove, 240, 210)
    now[0] += .01
    send(pet, QEvent.Type.MouseButtonRelease, 240, 210)
    APP.processEvents()
    before = pet.pos()
    for _ in range(30):
        interaction.tick(.016)
    assert not interaction.motion.active and pet.pos() == before


def test_event_timestamps_preserve_fling_despite_handling_delay(setup_pet):
    pet, interaction, now = setup_pet

    class TimedMouseEvent(QMouseEvent):
        def timestamp(self):
            return self.stamp

    for kind, stamp, x in ((QEvent.Type.MouseButtonPress, 1000, 210),
                            (QEvent.Type.MouseMove, 1050, 290),
                            (QEvent.Type.MouseButtonRelease, 1060, 310)):
        now[0] += .5
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        event = TimedMouseEvent(kind, QPointF(10, 10), QPointF(x, 210),
            Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
        event.stamp = stamp
        APP.sendEvent(pet, event)
    APP.processEvents()
    assert interaction.motion.active and interaction.motion.vx > 1000
