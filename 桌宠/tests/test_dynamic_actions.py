"""全部100动作真实解码、播放结束和原有交互的运行回归。"""

import time

import pytest
from PyQt6.QtCore import QRect
from PyQt6.QtWidgets import QApplication

from tests.test_dynamic_geometry import Config
from ui.dynamic_pet_widget import DynamicDesktopPetWidget

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def pet():
    widget = DynamicDesktopPetWidget(config_mgr=Config({}))
    widget.physics_timer.stop()
    yield widget
    for child in (widget, widget.bubble, widget.interaction_dialog):
        child.hide()
    widget.deleteLater()
    _APP.processEvents()


def test_all_100_actions_decode_first_middle_last_frames_with_bounded_cache(pet, monkeypatch):
    assert len(pet.actions) == 100
    assert len(pet._sheet_cache) == 1
    monkeypatch.setattr(pet, "show_critique", lambda text: None)
    position = pet.pos()
    keys = list(pet.actions)
    visited = set()
    for _ in keys:
        pet.cycle_next_outfit()
        visited.add(pet.current_action_key)
        row = pet.actions[pet.current_action_key]
        pixmap = pet._current_pixmap
        assert pixmap.width() == row["columns"] * row["frame_w"]
        assert pixmap.height() == row["rows"] * row["frame_h"]
        for frame in (0, row["count"] // 2, row["count"] - 1):
            rect = QRect(frame % row["columns"] * row["frame_w"],
                         frame // row["columns"] * row["frame_h"], row["frame_w"], row["frame_h"])
            assert not pixmap.copy(rect).isNull()
        assert len(pet._sheet_cache) <= 2 and pet.pos() == position
    assert visited == set(keys)


def finish_current_cycle(pet):
    row = pet.actions[pet.current_action_key]
    pet.current_frame = row["count"] - 1
    pet.frame_accumulator = 1 / row["base_fps"]
    pet.last_update_time = time.perf_counter()
    pet._step_60fps()


@pytest.mark.parametrize('dx, mirrored', [(-100, False), (100, True)])
def test_desktop_run_loops_faces_direction_and_finishes(pet, dx, mirrored):
    pet.feature_flipped = not mirrored
    pet.begin_desktop_move(dx)
    key = 'gemini:原地左转奔跑'
    assert pet.current_action_key == key and pet._loop_override is True
    assert (pet._body_transform().m11() < 0) is mirrored
    finish_current_cycle(pet)
    assert pet.current_action_key == key
    pet.trigger_happy_reaction()
    assert pet.current_action_key == key
    pet.end_desktop_move()
    assert pet.current_action_key == 'idle' and pet._loop_override is None
    assert (pet._body_transform().m11() < 0) is not mirrored


def test_vertical_desktop_run_keeps_current_facing(pet):
    pet.feature_flipped = True
    pet.begin_desktop_move(0)
    assert pet._body_transform().m11() < 0
    pet.end_desktop_move()


def test_action_menu_cannot_turn_running_into_single_play(pet):
    pet.begin_desktop_move(100)
    key = pet.current_action_key
    assert pet.play_action(key, loop=False) is False
    assert pet.set_action(key, restart=True) is False
    finish_current_cycle(pet)
    assert pet.current_action_key == key and pet.current_frame < pet.actions[key]['count']
    assert pet._loop_override is True
    pet.end_desktop_move()


def test_desktop_movement_starts_running_frame_and_loops_only_running_segment(pet):
    key = 'gemini:原地左转奔跑'
    pet.begin_desktop_move(-100)
    assert pet.current_frame == 40
    for _ in range(3):
        pet.current_frame = 63
        pet.frame_accumulator = 1 / pet.actions[key]['base_fps']
        pet.last_update_time = time.perf_counter()
        pet._step_60fps()
        assert pet.current_frame == 40 and pet.current_action_key == key
    pet.end_desktop_move()
    assert pet.current_action_key == 'idle' and pet.current_frame == 0
    assert pet.play_action(key)
    assert pet.current_frame == 0
    finish_current_cycle(pet)
    assert pet.current_action_key == 'idle'


def test_manual_single_play_returns_idle_and_loop_can_be_interrupted(pet):
    key = "gemini:下五子棋"
    assert pet.play_action(key)
    finish_current_cycle(pet)
    assert pet.current_action_key == "idle"
    assert pet.play_action(key, loop=True)
    finish_current_cycle(pet)
    assert pet.current_action_key == key
    pet.set_thinking(True)
    assert pet.current_action_key == "thinking"
    pet.set_thinking(False)
    assert pet.current_action_key == "idle"
    old = pet.current_action_key
    assert pet.play_action("不存在的动作") is False
    assert pet.current_action_key == old


def test_original_menu_preserved_and_every_action_is_reachable(pet):
    menu = pet._build_context_menu()
    labels = [action.text() for action in menu.actions()]
    for label in ("💬 呼叫锐评 (Ctrl+F9)", "🎭 切换动作/神态", "⚙️ 设置中心", "🔊 切换语音开关", "👁️ 自动感知开关", "❌ 退出程序"):
        assert label in labels
    library = next(action.menu() for action in menu.actions() if "全部动作" in action.text())
    choices = [action for group in library.actions() for action in group.menu().actions()]
    assert len(choices) == 100
    selected = next(action for action in choices if action.data() == "gemini:下五子棋")
    selected.trigger()
    assert pet.current_action_key == "gemini:下五子棋"
    menu.deleteLater()


def test_single_preview_of_idle_restores_original_loop_at_first_frame(pet):
    assert pet.play_action("idle", loop=False)
    finish_current_cycle(pet)
    assert pet.current_action_key == "idle"
    assert pet.current_frame == 0
    assert pet._loop_override is None
    finish_current_cycle(pet)
    assert pet.current_frame == 0
