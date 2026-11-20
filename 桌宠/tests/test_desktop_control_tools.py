import threading

import pytest

from desktop_control.config import ControlStore
from desktop_control.tools import Tools
from desktop_control.windows import encode_capture


class Backend:
    def __init__(self):
        self.actions = []
        self.window = {'hwnd': 42}
        self.released = 0

    def monitors(self):
        return [{'id': 1, 'left': -100, 'top': 0, 'width': 200, 'height': 100}]

    def capture(self, **kwargs):
        return {'image_b64': 'AA==', 'mime': 'image/jpeg', 'width': 200,
                    'height': 100, 'left': -100, 'top': 0, 'monitor_id': 1}

    def foreground(self):
        return dict(self.window)

    def controls(self):
        return {'available': False, 'reason': 'uiautomation_not_installed', 'items': []}

    def perform(self, action, cancel):
        assert not cancel.is_set()
        self.actions.append(action)

    def release_inputs(self):
        self.released += 1

    def input_stamp(self):
        return 1


def setup_tools():
    settings = {'enabled': True, 'cloud_enabled': True, 'allow_original': False}
    backend = Backend()
    tools = Tools(lambda: dict(settings), backend)
    tools.begin('cloud')
    return tools, backend, settings


def test_store_defaults_and_atomic_roundtrip(tmp_path):
    store = ControlStore(tmp_path / '配置.json')
    assert store.load()['enabled'] is False
    assert store.load()['profile_id'] == 'original'
    store.save({'enabled': True, 'profile_id': '中文'})
    assert store.load()['profile_id'] == '中文'
    assert store.load()['max_steps'] == 20


def test_authorization_is_rechecked_and_original_denied():
    tools, backend, settings = setup_tools()
    with pytest.raises(ValueError, match='original_not_allowed'):
        tools.call('screenshot', {'original': True})
    settings['cloud_enabled'] = False
    with pytest.raises(ValueError, match='cloud_disabled'):
        tools.call('monitors', {})
    assert backend.actions == []


def test_negative_coordinates_observation_and_deduplication():
    tools, backend, _ = setup_tools()
    snapshot = tools.call('screenshot', {})
    payload = {'x': -50, 'y': 10, 'observation_id': snapshot['observation_id'], 'request_id': 'one'}
    result = tools.call('click', payload)
    assert result['status'] == 'sent'
    assert tools.call('click', payload) == result
    assert len(backend.actions) == 1
    with pytest.raises(ValueError, match='stale_observation'):
        tools.call('click', dict(payload, request_id='two'))


@pytest.mark.parametrize('x', [float('nan'), float('inf'), -101, 100])
def test_invalid_coordinates_never_reach_backend(x):
    tools, backend, _ = setup_tools()
    snap = tools.call('screenshot', {})
    with pytest.raises(ValueError):
        tools.call('move', {'x': x, 'y': 5, 'request_id': 'bad', 'observation_id': snap['observation_id']})
    assert backend.actions == []


def test_foreground_change_unknown_and_stop():
    tools, backend, _ = setup_tools()
    snap = tools.call('screenshot', {})
    backend.window = {'hwnd': 99}
    with pytest.raises(ValueError, match='foreground_changed'):
        tools.call('type', {'text': 'test', 'request_id': 'a', 'observation_id': snap['observation_id']})
    with pytest.raises(ValueError, match='unknown_tool'):
        tools.call('execute', {})
    tools.stop()
    assert backend.released
    with pytest.raises(ValueError, match='inactive'):
        tools.call('monitors', {})


def test_stop_can_cancel_a_blocking_backend():
    tools, backend, _ = setup_tools()
    started = threading.Event()
    def perform(action, cancel):
        started.set()
        assert cancel.wait(2)
    backend.perform = perform
    snap = tools.call('screenshot', {})
    thread = threading.Thread(target=lambda: tools.call('click', {
        'x': 1, 'y': 1, 'request_id': 'a', 'observation_id': snap['observation_id']}))
    thread.start()
    assert started.wait(1)
    tools.stop()
    thread.join(2)
    assert not thread.is_alive()
    assert backend.released >= 2


def test_encoding_original_is_lossless_and_standard_preserves_physical_extent():
    from PIL import Image
    image = Image.new('RGB', (3000, 1000), 'red')
    original = encode_capture(image, -3000, 0, 2, True)
    assert original['mime'] == 'image/png'
    assert original['width'] == 3000
    standard = encode_capture(image, -3000, 0, 2, False)
    assert standard['width'] == 1440
    assert standard['physical_width'] == 3000
    assert standard['left'] == -3000


@pytest.mark.parametrize('name,extra', [('click', {'button': 'bad'}), ('move', {'code': 'bad'}),
                                      ('hotkey', {'keys': ['badkey']}), ('type', {'text': 42})])
def test_invalid_action_is_rejected_before_backend(name, extra):
    tools, backend, _ = setup_tools()
    snap = tools.call('screenshot', {})
    with pytest.raises(ValueError):
        tools.call(name, dict(x=1, y=1, request_id='bad', observation_id=snap['observation_id'], **extra))
    assert backend.actions == []


def test_expired_observation_and_single_owner():
    tools, _, _ = setup_tools()
    with pytest.raises(ValueError, match='busy'):
        tools.begin('local')
    snap = tools.call('screenshot', {})
    observation = tools._observations[snap['observation_id']]
    tools._observations[snap['observation_id']] = (observation[0] - 121, *observation[1:])
    with pytest.raises(ValueError, match='stale_observation'):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'old', 'observation_id': snap['observation_id']})


@pytest.mark.parametrize('kind', ['screenshot', 'controls'])
@pytest.mark.parametrize('age', [20, 60, 119])
def test_cloud_observation_survives_model_latency_with_live_recheck(kind, age):
    tools, backend, _ = setup_tools()
    backend.controls = lambda: {'available': True, 'items': [{'name': 'WPS', 'left': 0}]}
    snapshot = tools.call(kind, {})
    identity = snapshot['observation_id']
    old = tools._observations[identity]
    tools._observations[identity] = (old[0] - age, *old[1:])
    payload = {'x': 1, 'y': 1, 'request_id': 'slow-model', 'observation_id': identity}
    result = tools.call('click', payload)
    assert result['status'] == 'sent'
    assert tools.call('click', payload) == result
    assert len(backend.actions) == 1
    with pytest.raises(ValueError, match='stale_observation'):
        tools.call('click', dict(payload, request_id='consumed'))


@pytest.mark.parametrize('change,expected', [
    ('foreground', 'foreground_changed'), ('pixels', 'observation_changed'),
    ('monitors', 'observation_changed'), ('permission', 'cloud_disabled'),
    ('stop', 'inactive'),
])
def test_delayed_cloud_observation_still_rejects_changed_state(change, expected):
    tools, backend, settings = setup_tools()
    snap = tools.call('screenshot', {})
    identity = snap['observation_id']
    old = tools._observations[identity]
    tools._observations[identity] = (old[0] - 30, *old[1:])
    if change == 'foreground':
        backend.window = {'hwnd': 99}
    elif change == 'pixels':
        capture = backend.capture
        backend.capture = lambda **kwargs: {**capture(**kwargs), 'image_b64': 'changed'}
    elif change == 'monitors':
        backend.monitors = lambda: [{'id': 2, 'left': -100, 'top': 0, 'width': 200, 'height': 100}]
    elif change == 'permission':
        settings['cloud_enabled'] = False
    else:
        tools.stop()
    with pytest.raises(ValueError, match=expected):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'changed', 'observation_id': identity})
    assert backend.actions == []


def test_local_observation_keeps_original_fifteen_second_limit():
    tools, backend, _ = setup_tools()
    tools.stop()
    tools.begin('local')
    snap = tools.call('screenshot', {})
    identity = snap['observation_id']
    old = tools._observations[identity]
    tools._observations[identity] = (old[0] - 16, *old[1:])
    with pytest.raises(ValueError, match='stale_observation'):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'local-old', 'observation_id': identity})
    assert backend.actions == []


def test_failed_input_is_released_and_never_replayed():
    tools, backend, _ = setup_tools()
    def fail(action, cancel):
        backend.actions.append(action)
        raise RuntimeError('unknown send outcome')
    backend.perform = fail
    snap = tools.call('screenshot', {})
    action = {'x': 1, 'y': 1, 'request_id': 'failed', 'observation_id': snap['observation_id']}
    with pytest.raises(RuntimeError):
        tools.call('click', action)
    assert tools.call('click', action)['status'] == 'unknown'
    assert len(backend.actions) == 1
    assert backend.released == 1


def test_original_oversize_is_rejected_without_resizing(monkeypatch):
    from PIL import Image

    from desktop_control import windows
    monkeypatch.setattr(windows, 'LIMIT', 4200)
    with pytest.raises(ValueError, match='image_too_large'):
        encode_capture(Image.new('RGB', (100, 100)), 0, 0, 1, True)


def test_capture_accepts_monitor_metadata_without_leaking_it_into_region(monkeypatch):
    import mss

    from desktop_control.windows import WindowsDesktop

    class Capture:
        def __init__(self):
            self.monitors = [{'left': -2, 'top': 0, 'width': 2, 'height': 2, 'name': 'DISPLAY'},
                             {'left': -2, 'top': 0, 'width': 2, 'height': 2, 'name': 'DISPLAY'}]
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def grab(self, region):
            assert region == {'left': -2, 'top': 0, 'width': 2, 'height': 2}
            return type('Shot', (), {'size': (2, 2), 'rgb': b'\0' * 12})()

    monkeypatch.setattr(mss, 'mss', Capture)
    desktop = WindowsDesktop()
    assert desktop.capture(monitor_id=1)['source_width'] == 2


def test_capture_revocation_does_not_return_original():
    tools, backend, settings = setup_tools()
    settings['allow_original'] = True
    capture = backend.capture
    def revoke(**kwargs):
        settings['allow_original'] = False
        return capture(**kwargs)
    backend.capture = revoke
    with pytest.raises(ValueError, match='original_not_allowed'):
        tools.call('screenshot', {'original': True})


def test_same_window_changed_pixels_reject_input():
    tools, backend, _ = setup_tools()
    snap = tools.call('screenshot', {})
    capture = backend.capture
    backend.capture = lambda **kwargs: {**capture(**kwargs), 'image_b64': 'changed'}
    with pytest.raises(ValueError, match='observation_changed'):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'changed', 'observation_id': snap['observation_id']})
    assert backend.actions == []


def test_corrupted_config_fails_closed(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{"enabled":"false"}', encoding='utf-8')
    with pytest.raises(ValueError):
        ControlStore(path).load()


def test_click_must_be_inside_observed_region():
    tools, backend, _ = setup_tools()
    capture = backend.capture
    backend.capture = lambda **kwargs: {**capture(**kwargs), 'width': 10, 'height': 10}
    snap = tools.call('screenshot', {})
    with pytest.raises(ValueError, match='outside_observation'):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'outside', 'observation_id': snap['observation_id']})
    assert backend.actions == []


@pytest.mark.parametrize('target_changed', [False, True])
def test_pointer_checks_target_neighborhood_not_unrelated_animation(target_changed):
    from PIL import Image

    tools, backend, _ = setup_tools()
    image = Image.new('RGB', (200, 100), 'white')
    backend.capture = lambda **kwargs: encode_capture(image, -100, 0, 1, True)
    snap = tools.call('screenshot', {})
    image.putpixel((50 if target_changed else 190, 10), (0, 0, 0))
    payload = {'x': -50, 'y': 10, 'request_id': 'pixel', 'observation_id': snap['observation_id']}
    if target_changed:
        with pytest.raises(ValueError, match='observation_changed'):
            tools.call('click', payload)
        assert backend.actions == []
    else:
        assert tools.call('click', payload)['status'] == 'sent'


@pytest.mark.parametrize('name,extra', [('scroll', {'delta': 120}), ('click', {}),
                                      ('drag', {'to_x': 2, 'to_y': 2, 'duration': .1})])
def test_cancel_during_move_prevents_following_button_or_scroll(name, extra):
    from desktop_control.windows import WindowsDesktop

    cancel = threading.Event()
    events = []
    class User:
        def SetCursorPos(self, x, y):
            cancel.set()
            return 1
        def mouse_event(self, flags, *args):
            events.append(flags)
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    desktop._user = User()
    desktop._held_keys = set()
    desktop._held_buttons = set()
    desktop._input_lock = threading.RLock()
    desktop._active_cancel = None
    desktop._dpi = lambda: None
    desktop.monitors = lambda: [{'left': 0, 'top': 0, 'width': 100, 'height': 100}]
    with pytest.raises(ValueError, match='cancelled'):
        desktop.perform({'name': name, 'x': 1, 'y': 1, **extra}, cancel)
    assert events == []


def test_last_monitor_call_cannot_leak_revoked_original():
    tools, backend, settings = setup_tools()
    settings['allow_original'] = True
    monitors = backend.monitors
    def revoke():
        settings['allow_original'] = False
        return monitors()
    backend.monitors = revoke
    with pytest.raises(ValueError, match='original_not_allowed'):
        tools.call('screenshot', {'original': True})


def test_original_revalidation_revocation_prevents_input():
    tools, backend, settings = setup_tools()
    settings['allow_original'] = True
    snap = tools.call('screenshot', {'original': True})
    capture = backend.capture
    def revoke(**kwargs):
        settings['allow_original'] = False
        return capture(**kwargs)
    backend.capture = revoke
    with pytest.raises(ValueError, match='original_not_allowed'):
        tools.call('click', {'x': 1, 'y': 1, 'request_id': 'revoked', 'observation_id': snap['observation_id']})
    assert backend.actions == []


def test_release_cancels_drag_without_waiting_for_whole_duration():
    from desktop_control.windows import WindowsDesktop

    cancel = threading.Event()
    pressed = threading.Event()
    events = []
    errors = []
    class User:
        def SetCursorPos(self, x, y):
            events.append(('move', cancel.is_set()))
            return 1
        def mouse_event(self, flags, *args):
            events.append((flags, cancel.is_set()))
            if flags == 2:
                pressed.set()
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    desktop._user = User()
    desktop._held_keys = set()
    desktop._held_buttons = set()
    desktop._input_lock = threading.RLock()
    desktop._active_cancel = None
    desktop._dpi = lambda: None
    desktop.monitors = lambda: [{'left': 0, 'top': 0, 'width': 100, 'height': 100}]
    def run():
        try:
            desktop.perform({'name': 'drag', 'x': 1, 'y': 1, 'to_x': 90, 'to_y': 90, 'duration': 5}, cancel)
        except ValueError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=run)
    thread.start()
    assert pressed.wait(1)
    desktop.release_inputs()
    thread.join(1)
    assert not thread.is_alive()
    assert errors == ['cancelled']
    assert cancel.is_set()
    assert all(not cancelled or kind == 4 for kind, cancelled in events)


def test_old_stop_cannot_release_new_session_inputs():
    tools, backend, _ = setup_tools()
    releasing = threading.Event()
    allow_release = threading.Event()
    begun = threading.Event()
    def release():
        releasing.set()
        assert allow_release.wait(1)
    backend.release_inputs = release
    stop_thread = threading.Thread(target=tools.stop)
    stop_thread.start()
    assert releasing.wait(1)
    begin_thread = threading.Thread(target=lambda: (tools.begin('local'), begun.set()))
    begin_thread.start()
    assert not begun.wait(.05)
    allow_release.set()
    stop_thread.join(1)
    begin_thread.join(1)
    assert begun.is_set()
