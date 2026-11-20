"""纯物理回归：不触碰窗口、模型或生产配置。"""
import math

import pytest


def test_velocity_recent_window_stale_slow_and_clear():
    from pet_features.motion import VelocityTracker
    tracker = VelocityTracker()
    tracker.sample(-1000, 0, 0)
    tracker.sample(0, 0, 1)
    tracker.sample(50, 0, 1.05)
    tracker.sample(100, 0, 1.10)
    assert tracker.velocity(1.10) == pytest.approx((1000, 0))
    assert tracker.velocity(1.251) == (0, 0)
    tracker.clear()
    tracker.sample(0, 0, 2)
    tracker.sample(10, 0, 2.1)
    assert tracker.velocity(2.1) == (0, 0)


def test_velocity_limits_and_bad_samples():
    from pet_features.motion import VelocityTracker
    tracker = VelocityTracker()
    tracker.sample(0, 0, 1)
    tracker.sample(10000, 10000, 1.01)
    assert math.hypot(*tracker.velocity(1.01)) == pytest.approx(2000)
    tracker.sample(float('nan'), 0, 1.02)
    assert all(math.isfinite(v) for v in tracker.velocity(1.02))
    tracker.sample(0, 0, .5)
    assert tracker.velocity(.5) == (0, 0)


def test_sparse_fast_motion_and_same_timestamp_release_keep_velocity():
    from pet_features.motion import VelocityTracker
    tracker = VelocityTracker()
    tracker.sample(0, 0, 1)
    tracker.sample(300, 0, 1.2)
    tracker.sample(300, 0, 1.2)
    assert tracker.velocity(1.2)[0] == pytest.approx(1500)


def test_sparse_motion_followed_by_pause_is_a_placement():
    from pet_features.motion import VelocityTracker
    tracker = VelocityTracker()
    tracker.sample(0, 0, 1)
    tracker.sample(300, 0, 1.2)
    tracker.sample(300, 0, 1.4)
    assert tracker.velocity(1.4) == (0, 0)


def test_friction_motion_is_frame_rate_independent():
    from pet_features.motion import MotionState
    states = []
    for fps in (30, 60, 120):
        state = MotionState()
        state.launch(100, 100, 900, 300)
        for _ in range(fps):
            state.step(1 / fps, (0, 0, 10000, 10000), {'bounce_enabled': False})
        states.append((state.x, state.y, state.vx, state.vy))
    for state in states[1:]:
        assert state == pytest.approx(states[0], abs=.05)


def test_rapid_bounce_stays_in_bounds_and_reports_impact():
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(50, 50, 2000, 0)
    result = state.step(.25, (0, 0, 100, 100), {'friction': 1})
    assert 0 <= state.x <= 100
    assert result['bounced']
    assert result['impact'] >= 1000


def test_disabled_bounce_and_outside_start_do_not_snap():
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(150, 50, 300, 0)
    state.step(.01, (0, 0, 100, 100), {})
    assert abs(state.x - 150) < 10 and state.vx < 0
    state.launch(95, 50, 1000, 0)
    state.step(.1, (0, 0, 100, 100), {'bounce_enabled': False})
    assert state.x > 100


def test_fling_already_crossed_edge_reflects_without_position_teleport():
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(101, 50, 1000, 0)
    result = state.step(.01, (0, 0, 100, 100), {})
    assert result["bounced"] and state.vx < 0
    assert 90 < state.x < 101


def test_gravity_settles_without_ground_jitter():
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(50, 0, 200, -300)
    for _ in range(1800):
        state.step(1 / 120, (0, 0, 500, 300), {'gravity_enabled': True})
    assert not state.active
    assert state.y == pytest.approx(300)
    assert (state.vx, state.vy) == (0, 0)
    before = (state.x, state.y)
    assert state.step(1, (0, 0, 500, 300), {'gravity_enabled': True})['resting']
    assert (state.x, state.y) == before


def test_stop_zero_and_invalid_inputs_remain_finite():
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(10, 20, 0, 0)
    assert state.step(0, (0, 0, 100, 100), {})['resting']
    state.launch(10, 20, 300, 0)
    before = (state.x, state.y)
    state.step(float('nan'), (0, 0, 100, 100), {})
    assert (state.x, state.y) == before
    state.step(1e20, (0, 0, 100, 100), {'gravity': float('inf')})
    assert all(math.isfinite(v) for v in (state.x, state.y, state.vx, state.vy))
    state.stop()
    assert not state.active and state.vx == state.vy == 0
    state.launch(float('nan'), 0, float('inf'), 0)
    assert not state.active


def test_finite_extreme_samples_cannot_overflow_velocity():
    from pet_features.motion import VelocityTracker
    tracker = VelocityTracker()
    tracker.sample(-1e308, 0, 1)
    tracker.sample(1e308, 0, 1.01)
    assert all(math.isfinite(v) for v in tracker.velocity(1.01))


@pytest.mark.parametrize('bounds', [None, (), (100, 0, 0, 100), (0, 0, float('nan'), 10)])
def test_bad_bounds_do_not_corrupt_motion(bounds):
    from pet_features.motion import MotionState
    state = MotionState()
    state.launch(10, 20, 300, 0)
    state.step(.01, bounds, {})
    assert state.x > 10 and state.y == 20
