import base64
import io
import threading
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QApplication, QWidget

_APP = QApplication.instance() or QApplication([])


class Backend:
    def __init__(self):
        self.actions = []
    def monitors(self):
        return [{'id': 1, 'left': 0, 'top': 0, 'width': 600, 'height': 400}]
    def foreground(self):
        return {'hwnd': 123, 'pid': 456}
    def capture(self, **kwargs):
        buf = io.BytesIO()
        Image.new('RGB', (600, 400), 'white').save(buf, 'JPEG')
        return {'image_b64': base64.b64encode(buf.getvalue()).decode(), 'mime': 'image/jpeg',
                'width': 600, 'height': 400, 'physical_width': 600, 'physical_height': 400,
                'left': 0, 'top': 0, 'monitor_id': 1}
    def controls(self):
        return {'available': True, 'items': []}
    def perform(self, action, cancel):
        assert not cancel.is_set()
        self.actions.append(action)
    def release_inputs(self):
        pass
    def input_stamp(self):
        return 0


def make_control(tmp_path, planner):
    from desktop_control.config import ControlStore
    from desktop_control.runtime import DesktopControl
    from ui.desktop_control import ControlPanel
    store = ControlStore(tmp_path / 'control.json')
    cfg = store.load()
    cfg.update(enabled=True, move_pet=False)
    store.save(cfg)
    controller = QObject()
    controller.pet = QWidget()
    controller.pet.show_critique = lambda _text: None
    controller.pet.show()
    controller._is_analyzing = False
    controller.bridge = SimpleNamespace(summary_busy=False, config={'profiles': []})
    controller.voice_input = SimpleNamespace(busy=False, cancel=lambda: None)
    panel = ControlPanel(store, list)
    backend = Backend()
    control = DesktopControl(controller, panel, tmp_path, backend=backend, planner_factory=lambda: planner)
    return control, backend, controller


def wait_for(predicate):
    deadline = time.monotonic() + 4
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(.005)
    assert predicate()


def test_local_loop_observes_after_click_and_finishes(tmp_path):
    class Planner:
        calls = 0
        def next(self, goal, observation, history):
            self.calls += 1
            return ({'name': 'click', 'x': 100, 'y': 100} if self.calls == 1 else
                    {'name': 'finish', 'text': '已完成', 'evidence': '新截图已显示结果'})
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    control.submit('点击按钮')
    wait_for(lambda: not control.busy)
    assert len(backend.actions) == 1 and planner.calls == 2
    assert not controller.pet._desktop_control_active
    control.close()
    controller.pet.close()


def test_stopped_late_model_result_cannot_click(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Planner:
        def next(self, *_args):
            entered.set()
            release.wait(3)
            return {'name': 'click', 'x': 100, 'y': 100}
    control, backend, controller = make_control(tmp_path, Planner())
    control.submit('点击按钮')
    wait_for(entered.is_set)
    control.stop()
    release.set()
    wait_for(lambda: control.workers == 0)
    assert not backend.actions and not control.busy
    control.close()
    controller.pet.close()


def test_external_primitives_do_not_invoke_planner(tmp_path):
    control, backend, controller = make_control(tmp_path, None)
    cfg = dict(control.panel.config, cloud_enabled=True)
    control.panel.store.save(cfg)
    control.apply(cfg)
    result = control.external_tool('begin', {})
    assert result['session_id']
    observation = control.external_tool('screenshot', {})
    control.external_tool('click', {'x': 50, 'y': 50, 'observation_id': observation['observation_id'], 'request_id': 'one'})
    assert len(backend.actions) == 1
    control.external_tool('stop', {})
    control.close()
    controller.pet.close()


@pytest.mark.parametrize('action', [
    {'name': 'type', 'text': 'hello'},
    {'name': 'hotkey', 'keys': ['CTRL', 'A']},
    {'name': 'drag', 'x': 50, 'y': 50, 'to_x': 400, 'to_y': 250},
])
def test_changed_planned_target_pauses_without_input(tmp_path, action):
    class Planner:
        def next(self, *_args):
            original_capture = backend.capture
            def changed(**kwargs):
                shot = original_capture(**kwargs)
                picture = Image.open(io.BytesIO(base64.b64decode(shot['image_b64'])))
                # 保持起点、前台窗口不变，只改变拖拽落点/键盘输入区域。
                picture.paste('black', (360, 210, 440, 290))
                buf = io.BytesIO()
                picture.save(buf, 'JPEG')
                shot['image_b64'] = base64.b64encode(buf.getvalue()).decode()
                return shot
            backend.capture = changed
            return action
    control, backend, controller = make_control(tmp_path, Planner())
    control.panel.config['pause_on_input'] = True
    try:
        control.submit('操作按钮')
        wait_for(lambda: control.state in ('paused', 'finished'))
        assert not backend.actions
        assert control.state == 'paused'
    finally:
        control.close()
        controller.pet.close()


def test_voice_precheck_failure_allows_retry(tmp_path):
    from PyQt6.QtCore import pyqtSignal
    class Voice(QObject):
        settled = pyqtSignal(bool)
        busy = False
        attempts = 0
        def toggle_for(self, _callback):
            self.attempts += 1
            self.settled.emit(False)
        def finish(self):
            pass
        def cancel(self):
            self.settled.emit(False)
    control, _backend, controller = make_control(tmp_path, None)
    voice = Voice()
    controller.voice_input = voice
    # 与正式装配相同，重建以连接语音结束信号。
    control.close()
    from desktop_control.runtime import DesktopControl
    control = DesktopControl(controller, control.panel, tmp_path, backend=_backend)
    try:
        control.voice()
        control.voice()
        assert voice.attempts == 2
        assert not control._voice_pending
    finally:
        control.close()
        controller.pet.close()


def test_pet_movement_reaches_target_before_input(tmp_path):
    class Planner:
        def next(self, _goal, _observation, history):
            return ({'name': 'click', 'x': 300, 'y': 200} if not history else
                    {'name': 'finish', 'text': '完成', 'evidence': '已再次观察'})
    control, backend, controller = make_control(tmp_path, Planner())
    control.panel.config['move_pet'] = True
    initial = controller.pet.pos()
    try:
        control.submit('点击按钮')
        wait_for(lambda: control.state == 'moving')
        assert not backend.actions and controller.pet._desktop_control_active
        wait_for(lambda: not control.busy)
        assert controller.pet.pos() != initial
        assert len(backend.actions) == 1
    finally:
        control.close()
        controller.pet.close()


@pytest.mark.parametrize('ending', ['arrival', 'pause', 'stop', 'close'])
def test_model_movement_starts_and_ends_running(tmp_path, ending):
    control, _backend, controller = make_control(tmp_path, None)
    pet, events = controller.pet, []
    pet.begin_desktop_move = lambda dx: events.append(('begin', dx))
    pet.end_desktop_move = lambda: events.append(('end', None))
    control._execute = lambda action: events.append(('execute', action))
    try:
        control._approach({'name': 'click', 'x': 300, 'y': 200})
        assert events and events[0][0] == 'begin'
        if ending == 'arrival':
            started, duration, origin, target, action = control._motion
            control._motion = (started - duration - 1, duration, origin, target, action)
            control._move_tick()
            assert pet.pos() == target
            assert [event[0] for event in events] == ['begin', 'end', 'execute']
        else:
            getattr(control, ending)()
            assert [event[0] for event in events] == ['begin', 'end']
        assert control._motion is None
    finally:
        control.close()
        pet.close()


def test_only_model_destination_is_kept_on_target_screen(tmp_path):
    from PyQt6.QtCore import Qt

    control, _backend, controller = make_control(tmp_path, None)
    controller.pet.close()
    pet = controller.pet = QWidget(None, Qt.WindowType.FramelessWindowHint)
    pet.show()
    pet.resize(160, 120)
    try:
        control._approach({'name': 'click', 'x': 595, 'y': 395})
        started, duration, origin, target, action = control._motion
        control._motion = (started - duration - 1, duration, origin, target, action)
        control._execute = lambda _action: None
        control._move_tick()
        assert _APP.primaryScreen().geometry().contains(pet.geometry())
        pet.move(-300, -300)
        assert pet.x() == -300 and pet.y() == -300
    finally:
        control.close()
        pet.close()


def test_capture_guard_keeps_pet_visible_and_restores(monkeypatch, tmp_path):
    import desktop_control.runtime as module
    control, _backend, controller = make_control(tmp_path, None)
    guarded, restored = [], []
    monkeypatch.setattr(module, 'exclude_capture', lambda widget: guarded.append(widget) or ('guard', widget))
    monkeypatch.setattr(module, 'restore_capture', restored.append)
    try:
        control._hide_capture()
        assert controller.pet.isVisible() and guarded == [controller.pet]
        control._restore_capture()
        assert restored == [('guard', controller.pet)]
        assert controller.pet.isVisible()
    finally:
        control.close()
        controller.pet.close()


@pytest.mark.parametrize('ending,pending_capture', [('pause', False), ('stop', True), ('close', True), ('ask', False)])
def test_own_windows_stay_excluded_between_capture_movement_and_pause(monkeypatch, tmp_path, ending, pending_capture):
    import desktop_control.runtime as module

    control, _backend, controller = make_control(tmp_path, None)
    state, calls = {'excluded': False}, []
    def exclude(widget, *, passthrough=True):
        previous = state['excluded']
        state['excluded'] = True
        calls.append(passthrough)
        return id(widget), previous, passthrough
    def restore(token):
        state['excluded'] = token[1]
    monkeypatch.setattr(module, 'exclude_capture', exclude)
    monkeypatch.setattr(module, 'restore_capture', restore)
    try:
        control.state = 'planning'
        control._hide_capture()
        assert calls == [False, True]
        control._restore_capture()
        assert state['excluded'] and controller.pet.isVisible()
        control.state = 'moving'
        control._hide_capture()
        if not pending_capture:
            control._restore_capture()
        assert calls == [False, True, True] and state['excluded']
        if ending == 'ask':
            control._accept({'name': 'ask', 'text': '请补充目标'})
        else:
            getattr(control, ending)()
        assert state['excluded'] is False
        assert not control._capture_exclusions and not control._capture_guards
    finally:
        control.close()
        controller.pet.close()


@pytest.mark.parametrize('cancelled', [False, True])
def test_transient_after_move_artifact_rechecks_locally_without_new_model_plan(tmp_path, cancelled):
    class Planner:
        calls = 0
        def next(self, _goal, _observation, _history):
            self.calls += 1
            return ({'name': 'click', 'x': 300, 'y': 200} if self.calls == 1 else
                    {'name': 'finish', 'text': '完成', 'evidence': '已查看点击后结果'})
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    control.panel.config['move_pet'] = True
    moved = {'artifact': False}
    artifact_ready = threading.Event()
    controller.pet.begin_desktop_move = lambda _dx: moved.update(artifact=True)
    original = backend.capture
    def capture(**kwargs):
        shot = original(**kwargs)
        if moved['artifact']:
            moved['artifact'] = False
            data = io.BytesIO()
            Image.new('RGB', (600, 400), 'red').save(data, 'JPEG')
            shot['image_b64'] = base64.b64encode(data.getvalue()).decode()
            artifact_ready.set()
        return shot
    backend.capture = capture
    try:
        control.submit('点击按钮')
        if cancelled:
            wait_for(artifact_ready.is_set)
            control.stop()
            wait_for(lambda: control.workers == 0)
            assert len(backend.actions) == 0 and planner.calls == 1
        else:
            wait_for(lambda: not control.busy)
            assert len(backend.actions) == 1 and planner.calls == 2
        assert not any('未发送' in row.get('result', '') for row in control.history)
    finally:
        control.close()
        controller.pet.close()


def test_pause_resume_between_local_rechecks_cannot_send_old_action(monkeypatch, tmp_path):
    import desktop_control.runtime as module

    control, backend, controller = make_control(tmp_path, None)
    callbacks, workers = [], []
    monkeypatch.setattr(module.QTimer, 'singleShot', lambda _delay, callback: callbacks.append(callback))
    control._spawn = lambda _stage, work, _epoch=None: workers.append(work)
    control.tools.begin('local')
    control.state = 'planning'
    control.observation = {**backend.capture(), 'foreground': backend.foreground()}
    original, shots = control.tools.call, [0]
    def call(name, payload=None):
        if name == 'screenshot':
            shots[0] += 1
            if shots[0] == 2:
                control.pause()
                control.resume()
        result = original(name, payload)
        if name == 'screenshot' and shots[0] == 1:
            data = io.BytesIO()
            Image.new('RGB', (600, 400), 'red').save(data, 'JPEG')
            result['image_b64'] = base64.b64encode(data.getvalue()).decode()
        return result
    control.tools.call = call
    try:
        control._execute({'name': 'click', 'x': 300, 'y': 200})
        callbacks.pop(0)()
        with pytest.raises(ValueError, match='cancelled'):
            workers.pop(0)()
        assert not backend.actions
    finally:
        control.close()
        controller.pet.close()


def test_model_movement_is_slow_enough_to_see_running(tmp_path):
    from PyQt6.QtCore import QPoint

    control, _backend, controller = make_control(tmp_path, None)
    pet = controller.pet
    pet.resize(160, 120)
    action = {'name': 'click', 'x': 300, 'y': 200}
    try:
        control._approach(action)
        target = control._motion[3]
        pet.move(target - QPoint(600, 0))
        control._approach(action)
        assert 1.8 <= control._motion[1] <= 2.5
        pet.move(target)
        control._approach(action)
        assert control._motion[1] >= .7
    finally:
        control.close()
        pet.close()


def test_failed_target_keeps_public_explanation_and_tells_next_model(tmp_path):
    control, _backend, controller = make_control(tmp_path, None)
    shown = []
    controller.pet.show_critique = shown.append
    control._execute = lambda _action: setattr(control, 'state', 'executing')
    try:
        control._accept({'name': 'click', 'x': 100, 'y': 100,
                         'message': '准备点击任务栏浏览器，依据是图标下方的应用名称。'})
        control._complete((control.epoch, 'execute', None, 'target_changed'))
        assert '浏览器' in shown[-1] and '未发送输入' in shown[-1]
        assert '重新' in shown[-1]
        assert control.history[-1]['action']['name'] == 'click'
        assert '未发送' in control.history[-1]['result']
        record = (tmp_path / '桌面操作记录.jsonl').read_text(encoding='utf-8')
        assert '图标下方' in record and '未发送输入' in record
    finally:
        control.close()
        controller.pet.close()


def test_own_control_window_focus_requires_click_before_keyboard_input(tmp_path):
    import os

    class Planner:
        calls = 0
        def next(self, _goal, observation, history):
            assert observation['keyboard_target_available'] is False
            self.calls += 1
            if self.calls == 1:
                return {'name': 'type', 'text': '不应写入桌宠窗口'}
            assert '焦点' in history[-1]['result'] and '未发送' in history[-1]['result']
            return {'name': 'ask', 'text': '请指定需要输入的应用窗口'}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    backend.foreground = lambda: {'hwnd': 123, 'pid': os.getpid()}
    try:
        control.submit('输入文字')
        wait_for(lambda: control.state in ('ask', 'finished'))
        assert control.state == 'ask' and not backend.actions
        assert planner.calls == 2
    finally:
        control.close()
        controller.pet.close()


def test_pause_resume_discards_old_plan_and_reobserves(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                release.wait(3)
                return {'name': 'click', 'x': 50, 'y': 50}
            return {'name': 'finish', 'text': '新观察完成', 'evidence': '恢复后重新截图'}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    try:
        control.submit('测试暂停')
        wait_for(entered.is_set)
        control.pause()
        release.set()
        wait_for(lambda: control.workers == 0)
        control.resume()
        wait_for(lambda: not control.busy)
        assert planner.calls == 2 and not backend.actions
    finally:
        release.set()
        control.close()
        controller.pet.close()


def test_new_settings_allow_parallel_input_by_default(tmp_path):
    from desktop_control.config import ControlStore
    assert ControlStore(tmp_path / 'new.json').load()['pause_on_input'] is False


@pytest.mark.parametrize('pause_on_input', [False, True])
def test_pet_interaction_obeys_optional_input_pause(tmp_path, pause_on_input):
    from PyQt6.QtCore import QEvent
    control, _backend, controller = make_control(tmp_path, None)
    control.panel.config['pause_on_input'] = pause_on_input
    control.state = 'planning'
    try:
        control.eventFilter(controller.pet, QEvent(QEvent.Type.MouseButtonPress))
        assert control.state == ('paused' if pause_on_input else 'planning')
    finally:
        control.close()
        controller.pet.close()


def test_manual_input_does_not_interrupt_parallel_task(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Planner:
        def next(self, *_args):
            entered.set()
            release.wait(3)
            return {'name': 'finish', 'text': '完成', 'evidence': '观察完成'}
    control, backend, controller = make_control(tmp_path, Planner())
    try:
        control.submit('不中断任务')
        wait_for(entered.is_set)
        backend.input_stamp = lambda: 999
        control._tick()
        assert control.state == 'planning'
        release.set()
        wait_for(lambda: not control.busy)
    finally:
        release.set()
        control.close()
        controller.pet.close()


def test_parallel_target_change_reobserves_without_manual_resume(tmp_path):
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            if self.calls == 1:
                backend.foreground = lambda: {'hwnd': 789, 'pid': 456}
                return {'name': 'click', 'x': 50, 'y': 50}
            return {'name': 'finish', 'text': '新观察完成', 'evidence': '已依据切换后的窗口'}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    try:
        control.submit('并行观察')
        wait_for(lambda: control.state in ('paused', 'finished'))
        assert control.state == 'finished'
        assert planner.calls == 2 and not backend.actions
    finally:
        control.close()
        controller.pet.close()


def test_repeated_parallel_changes_obey_task_step_limit(tmp_path):
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            handle = self.calls + 1000
            backend.foreground = lambda: {'hwnd': handle, 'pid': 456}
            return {'name': 'click', 'x': 50, 'y': 50}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    control.panel.config['max_steps'] = 2
    try:
        control.submit('反复变化')
        wait_for(lambda: control.state in ('paused', 'finished'))
        assert control.state == 'finished'
        assert planner.calls == 2 and not backend.actions
    finally:
        control.close()
        controller.pet.close()


def test_stop_cancels_delayed_parallel_reobservation(tmp_path):
    from PyQt6.QtTest import QTest
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            backend.foreground = lambda: {'hwnd': 789, 'pid': 456}
            return {'name': 'click', 'x': 50, 'y': 50}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    try:
        control.submit('等待时停止')
        wait_for(lambda: control.state == 'waiting')
        control.stop()
        QTest.qWait(350)
        assert control.state == 'finished'
        assert planner.calls == 1 and not backend.actions
    finally:
        control.close()
        controller.pet.close()


def test_unknown_send_failure_is_not_retried_in_parallel(tmp_path):
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            return {'name': 'click', 'x': 50, 'y': 50}
    planner = Planner()
    control, backend, controller = make_control(tmp_path, planner)
    def uncertain(action, _cancel):
        backend.actions.append(action)
        raise OSError('发送结果未知')
    backend.perform = uncertain
    try:
        control.submit('不可重放')
        wait_for(lambda: not control.busy)
        assert planner.calls == 1 and len(backend.actions) == 1
    finally:
        control.close()
        controller.pet.close()


def test_stream_progress_reaches_bubble_before_click_and_stop_discards_late_chunks(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Planner:
        def next_stream(self, _goal, _observation, _history, on_message, cancel):
            on_message('准备关闭窗口')
            entered.set()
            release.wait(3)
            on_message('迟到输出不可展示')
            assert cancel.is_set()
            return {'name': 'click', 'x': 50, 'y': 50, 'message': '迟到动作'}
    control, backend, controller = make_control(tmp_path, Planner())
    bubbles = []
    controller.pet.show_critique = bubbles.append
    try:
        control.submit('关闭窗口')
        wait_for(entered.is_set)
        wait_for(lambda: '准备关闭窗口' in bubbles)
        assert not backend.actions
        control.stop()
        release.set()
        wait_for(lambda: control.workers == 0)
        assert '迟到输出不可展示' not in bubbles and not backend.actions
    finally:
        release.set()
        control.close()
        controller.pet.close()


def test_public_message_never_sent_as_native_action_parameter(tmp_path):
    class Planner:
        calls = 0
        def next(self, *_args):
            self.calls += 1
            return ({'name': 'click', 'x': 100, 'y': 100, 'message': '点击关闭按钮'} if self.calls == 1 else
                    {'name': 'finish', 'text': '已关闭', 'evidence': '窗口已消失', 'message': '关闭成功'})
    control, backend, controller = make_control(tmp_path, Planner())
    bubbles = []
    controller.pet.show_critique = bubbles.append
    try:
        control.submit('关闭窗口')
        wait_for(lambda: not control.busy)
        assert len(backend.actions) == 1
        assert 'message' not in backend.actions[0]
        assert any('点击关闭按钮' in value for value in bubbles)
        assert any('窗口已消失' in value for value in bubbles)
    finally:
        control.close()
        controller.pet.close()
