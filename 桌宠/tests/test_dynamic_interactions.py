"""空闲随机动作和五连击；仅发送Qt事件，不操作系统鼠标或真实模型。"""

import time

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from tests.test_dynamic_actions import finish_current_cycle
from tests.test_dynamic_actions import pet as pet  # noqa: PLC0414


def mouse(pet, kind, x=50, y=50):
    local = QPointF(x, y)
    global_pos = QPointF(pet.mapToGlobal(local.toPoint()))
    event = QMouseEvent(kind, local, global_pos, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(pet, event)


def click(pet, *, double=False):
    mouse(pet, QMouseEvent.Type.MouseButtonDblClick if double else QMouseEvent.Type.MouseButtonPress)
    mouse(pet, QMouseEvent.Type.MouseButtonRelease)


def test_random_default_single_cycle_never_repeats_or_moves_and_respects_priority(pet, monkeypatch):
    assert pet.random_actions_enabled and pet.random_action_interval == 5
    pet.show()
    chosen = []
    def choose(keys):
        assert pet._last_random_key not in keys
        selected = keys[0]
        chosen.append(selected)
        return selected
    monkeypatch.setattr("ui.dynamic_pet_widget.random.choice", choose)
    position = pet.pos()
    for _ in range(2):
        pet._next_random_at = 0
        pet._maybe_play_random()
        assert pet.current_action_key == chosen[-1] != "idle"
        assert pet._loop_override is False and pet.pos() == position
        finish_current_cycle(pet)
        assert pet.current_action_key == "idle"
    assert chosen[0] != chosen[1]
    pet.set_thinking(True)
    pet._next_random_at = 0
    pet._maybe_play_random()
    assert pet.current_action_key == "thinking" and len(chosen) == 2
    click(pet)
    assert pet.current_action_key == "thinking"
    pet.set_thinking(False)
    pet.play_action("coding", loop=True)
    pet._next_random_at = 0
    pet._maybe_play_random()
    assert pet.current_action_key == "coding" and len(chosen) == 2
    pet.play_action("idle", loop=True)
    pet.configure_random_playback(False, 30)
    pet._next_random_at = 0
    pet._maybe_play_random()
    assert pet.current_action_key == "idle" and len(chosen) == 2


def test_five_clicks_including_double_click_emit_once_and_keep_feedback(pet):
    requests = []
    pet.request_critique.connect(lambda: requests.append(True))
    start = time.monotonic()
    click(pet)
    click(pet, double=True)
    assert pet.particles, "保留原双击爱心反馈"
    click(pet)
    click(pet, double=True)
    assert not requests
    click(pet)
    assert requests == [True], (pet._click_count, time.monotonic() - start)
    click(pet)
    assert requests == [True]


def test_random_waits_for_deadline_and_never_interrupts_menu_drag_or_dialog(pet):
    pet.show()
    pet._maybe_play_random()
    assert pet.current_action_key == "idle"
    pet._next_random_at = 0
    pet.hide()
    pet._maybe_play_random()
    assert pet.current_action_key == "idle"
    pet.show()
    for flag in ("_menu_open", "is_dragging"):
        setattr(pet, flag, True)
        pet._maybe_play_random()
        assert pet.current_action_key == "idle"
        setattr(pet, flag, False)
    pet.interaction_dialog.show()
    pet._maybe_play_random()
    assert pet.current_action_key == "idle"
    pet.interaction_dialog.hide()


def test_drag_or_expired_streak_does_not_count_as_five_clicks(pet):
    requests = []
    pet.request_critique.connect(lambda: requests.append(True))
    for _ in range(4):
        click(pet)
    mouse(pet, QMouseEvent.Type.MouseButtonPress)
    mouse(pet, QMouseEvent.Type.MouseMove, 100, 100)
    mouse(pet, QMouseEvent.Type.MouseButtonRelease, 100, 100)
    click(pet)
    assert not requests
    assert pet._click_count == 1
    pet._click_started_at -= 3
    for _ in range(4):
        click(pet)
    assert not requests
    click(pet)
    assert requests == [True]


def test_heart_feedback_debounces_bursts_and_caps_particles(pet, monkeypatch):
    actions = []
    monkeypatch.setattr(pet, "set_action", lambda key: actions.append(key))
    for _ in range(100):
        pet.trigger_happy_reaction(50, 50)
    assert len(pet.particles) == 7
    assert actions == ["happy"]
    # 800ms窗口结束后允许下一次，但即使低帧率未消散也不能无限叠加。
    for _ in range(5):
        pet._last_happy_feedback_at -= 0.81
        pet.trigger_happy_reaction(50, 50)
        assert len(pet.particles) <= 14
    assert len(actions) == 6


def test_heart_debounce_does_not_swallow_five_click_request(pet, monkeypatch):
    monkeypatch.setattr("ui.dynamic_pet_widget.time.monotonic", lambda: 100.0)
    requests = []
    pet.request_critique.connect(lambda: requests.append(True))
    click(pet)
    for _ in range(4):
        click(pet, double=True)
    assert requests == [True]
    assert len(pet.particles) == 7
