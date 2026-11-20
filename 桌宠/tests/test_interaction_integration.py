"""真实角色+扩展装配，不访问用户配置/鼠标/模型。"""
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget

from pet_features.config import FeaturesStore
from pet_features.interaction import PetInteraction
from pet_features.runtime import FeatureRuntime
from tests.test_dynamic_actions import pet as pet  # noqa: PLC0414
from ui.features_settings import FeaturesSettingsPanel

APP = QApplication.instance() or QApplication([])


@pytest.fixture
def connected(pet, tmp_path):
    panel = FeaturesSettingsPanel(FeaturesStore(tmp_path / "features.json"))
    window = QWidget()
    controller = SimpleNamespace(pet=pet, settings_win=window, speaker=SimpleNamespace(config={}),
        smart=SimpleNamespace(blocked=lambda: False), _is_analyzing=False)
    runtime = FeatureRuntime(controller, panel, tmp_path)
    pet.move(100, 100)
    pet.show()
    yield runtime, pet, panel
    runtime.close()
    window.hide()
    panel.hide()
    APP.processEvents()


def test_middle_click_resets_five_click_streak_and_notifies_rest(connected):
    runtime, pet, _panel = connected
    requests = []
    pet.request_critique.connect(lambda: requests.append(True))
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    for _ in range(4):
        event(pet, QEvent.Type.MouseButtonPress, point)
        event(pet, QEvent.Type.MouseButtonRelease, point)
    event(pet, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.MiddleButton)
    runtime.tools._rest_until = 0
    event(pet, QEvent.Type.MouseButtonRelease, point, Qt.MouseButton.MiddleButton)
    assert runtime.tools._rest_until > 0
    event(pet, QEvent.Type.MouseButtonPress, point)
    event(pet, QEvent.Type.MouseButtonRelease, point)
    assert requests == [] and runtime.tools._rest_until > 0


def event(pet, kind, point, button=Qt.MouseButton.LeftButton):
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    APP.sendEvent(pet, QMouseEvent(kind, QPointF(20, 20), point, button, buttons, Qt.KeyboardModifier.NoModifier))


def test_runtime_installs_physics_and_close_unloads(connected):
    runtime, pet, _panel = connected
    physics = pet.feature_interaction
    assert physics is runtime.interaction and physics.cfg["enabled"]
    assert physics.visual is not None
    physics.motion.launch(pet.x(), pet.y(), 500, 0)
    runtime.close()
    assert not physics.timer.isActive() and not physics.motion.active


def test_desktop_mode_keeps_manual_fling_but_blocks_automatic_motion(connected):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    physics.timer.stop()
    pet._desktop_control_active = True
    assert not physics.blocked(), '操作模式不应禁止用户甩抛'
    physics._launch((800, 100), physics.epoch)
    assert physics.motion.active
    pet._desktop_auto_moving = True
    assert physics.blocked(), '自动跑步不能叠加弹射物理'
    physics.tick(.016)
    assert not physics.motion.active
    pet._desktop_auto_moving = False
    physics._launch((800, 100), physics.epoch)
    assert physics.motion.active


@pytest.mark.parametrize('rope', [False, True])
@pytest.mark.parametrize('button', [Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton])
def test_analyzing_preserves_drag_launch_gravity_and_bounce(connected, rope, button):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    physics.timer.stop()
    now = [100.]
    physics.clock = lambda: now[0]
    physics.cfg.update(rope_enabled=rope, gravity_enabled=True, friction=1.)
    runtime.controller._is_analyzing = True
    pet.set_thinking(True)
    assert not physics.blocked()
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point, button)
    now[0] += .05
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0), button)
    physics.tick(.016)
    assert physics._start is not None and pet.feature_motion_input
    now[0] += .02
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(120, 0), button)
    APP.processEvents()
    assert physics.motion.active and not pet.feature_motion_input
    vy = physics.motion.vy
    physics.tick(.016)
    assert physics.motion.active and physics.motion.vy > vy
    bounds = physics._bounds()
    physics.motion.x = bounds[2] - 1
    physics.motion.vx = 800
    physics.tick(.016)
    assert physics.motion.vx < 0 and physics.motion.active
    assert pet._thinking_active


def test_real_pet_deactivation_preserves_animation_and_natural_flight(connected):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    physics.timer.stop()
    physics.cfg.update(gravity_enabled=False, friction=.7)
    pet.play_action("happy", loop=True)
    pet.current_frame = 2
    physics._launch((800, 100), physics.epoch)
    before = pet.pos()
    APP.sendEvent(pet, QEvent(QEvent.Type.WindowDeactivate))
    assert physics.motion.active and pet.feature_motion_active
    physics.tick(.016)
    assert pet.pos() != before
    assert pet.current_action_key == "happy" and pet.current_frame == 2
    assert pet._loop_override is True
    for _ in range(200):
        physics.tick(.016)
    assert not physics.motion.active and not pet.feature_motion_active


class CountedInteraction(PetInteraction):
    def __init__(self, pet):
        self.calls = 0
        super().__init__(pet)

    def tick(self, dt=None):
        self.calls += 1
        super().tick(dt)


def test_dynamic_motion_uses_each_animation_tick_without_second_clock(pet):
    pet.show()
    physics = CountedInteraction(pet)
    physics.configure({"enabled": True})
    assert not physics.timer.isActive(), "动态角色不应再运行独立物理时钟"
    physics.cfg.update(gravity_enabled=False, friction=1., bounce_enabled=False)
    now = [100.]
    physics.clock = lambda: now[0]
    physics._launch((800, 0), physics.epoch)
    start = physics.motion.x
    for index in range(10):
        now[0] += .016
        pet.physics_timer.timeout.emit()
        assert physics.calls == index + 1
    assert physics.motion.x - start == pytest.approx(128.)
    physics.configure({"enabled": True})
    pet.physics_timer.timeout.emit()
    assert physics.calls == 11, "重新配置不能重复连接物理回调"
    physics.close()


def test_disabling_or_closing_physics_keeps_animation_clock_running(pet):
    physics = CountedInteraction(pet)
    pet.physics_timer.start()
    physics.configure({"enabled": False})
    assert pet.physics_timer.isActive() and not physics.timer.isActive()
    physics.configure({"enabled": True})
    assert pet.physics_timer.isActive() and not physics.timer.isActive()
    physics.close()
    assert pet.physics_timer.isActive()
    # 主动画时钟继续发信号，已关闭的物理不再接收。
    before = physics.calls
    pet.physics_timer.timeout.emit()
    assert physics.calls == before
    pet.physics_timer.stop()


def test_real_five_click_chain_survives_physics_filter(connected):
    runtime, pet, _panel = connected
    requests = []
    pet.request_critique.connect(lambda: requests.append(True))
    for index in range(5):
        kind = QEvent.Type.MouseButtonDblClick if index in (1, 3) else QEvent.Type.MouseButtonPress
        point = QPointF(pet.mapToGlobal(pet.rect().center()))
        event(pet, kind, point)
        event(pet, QEvent.Type.MouseButtonRelease, point)
    assert requests == [True] and pet.particles
    assert not runtime.interaction.motion.active


def test_sleep_wake_rescue_and_config_disable_work(connected):
    runtime, pet, panel = connected
    runtime.request("tools:sleep")
    APP.processEvents()
    assert pet.feature_sleeping and "睡" in pet.actions[pet.current_action_key]["title"]
    runtime.request("tools:wake")
    assert not pet.feature_sleeping
    runtime.interaction.motion.launch(pet.x(), pet.y(), 500, 0)
    runtime.request("tools:rescue")
    assert not runtime.interaction.motion.active
    cfg = panel.config
    cfg["interaction"]["enabled"] = False
    runtime.apply(cfg)
    assert not runtime.interaction.timer.isActive()


def test_locked_blocks_rope_and_sling(connected):
    runtime, pet, panel = connected
    cfg = panel.config
    cfg["behavior"].update(enabled=True, locked=True)
    runtime.apply(cfg)
    start = pet.pos()
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point)
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0))
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(100, 0))
    APP.processEvents()
    assert pet.pos() == start and not runtime.interaction.motion.active


def test_manual_animation_loop_still_allows_original_dragging(connected):
    runtime, pet, _panel = connected
    runtime.interaction.cfg["rope_enabled"] = False
    pet.play_action("happy", loop=True)
    start = pet.pos()
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point)
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0))
    runtime.interaction.tick(.016)
    assert pet.x() == start.x() + 100
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(100, 0))


@pytest.mark.parametrize("button,rope,loop", [
    (Qt.MouseButton.LeftButton, True, True),
    (Qt.MouseButton.LeftButton, False, False),
    (Qt.MouseButton.MiddleButton, True, True),
    (Qt.MouseButton.MiddleButton, True, False)])
def test_fling_preserves_current_animation_and_frame(connected, button, rope, loop):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    physics.cfg["rope_enabled"] = rope
    physics.timer.stop()
    now = [100.]
    physics.clock = lambda: now[0]
    pet.play_action("happy", loop=loop)
    pet.current_frame = 2
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point, button)
    now[0] += .05
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0), button)
    assert pet.current_action_key == "happy" and pet.current_frame == 2
    now[0] += .02
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(120, 0), button)
    APP.processEvents()
    physics.tick(.016)
    assert physics.motion.active
    assert pet.current_action_key == "happy" and pet.current_frame == 2 and pet._loop_override is loop


def test_random_single_action_does_not_disable_fling(connected):
    runtime, pet, _panel = connected
    physics = runtime.interaction
    now = [100.]
    physics.clock = lambda: now[0]
    physics.timer.stop()
    pet._next_random_at = 0
    pet._maybe_play_random()
    assert pet._loop_override is False
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point)
    now[0] += .05
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0))
    now[0] += .02
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(120, 0))
    APP.processEvents()
    assert physics.motion.active


def test_physics_and_sleep_suppress_random_actions(connected):
    _runtime, pet, _panel = connected
    for key in ("feature_motion_active", "feature_sleeping"):
        setattr(pet, key, True)
        pet._next_random_at = 0
        pet._maybe_play_random()
        assert pet.current_action_key == "idle"
        setattr(pet, key, False)


def test_middle_drag_takes_priority_over_roaming_and_background(connected):
    runtime, pet, panel = connected
    cfg = panel.config
    cfg["behavior"].update(enabled=True, mode="walk")
    runtime.apply(cfg)
    point = QPointF(pet.mapToGlobal(pet.rect().center()))
    event(pet, QEvent.Type.MouseButtonPress, point, Qt.MouseButton.MiddleButton)
    event(pet, QEvent.Type.MouseMove, point + QPointF(100, 0), Qt.MouseButton.MiddleButton)
    position, action = pet.pos(), pet.current_action_key
    runtime.tools.tick(.1)
    pet._next_random_at = 0
    pet._maybe_play_random()
    assert pet.pos() == position and pet.current_action_key == action and runtime._busy()
    event(pet, QEvent.Type.MouseButtonRelease, point + QPointF(100, 0), Qt.MouseButton.MiddleButton)
    APP.processEvents()
