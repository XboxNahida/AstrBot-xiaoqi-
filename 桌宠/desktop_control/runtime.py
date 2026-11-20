"""桌宠本地操作协调；外部只暴露原子工具，不为小七预装规划。"""
import base64
import copy
import io
import json
import os
import threading
import time
import uuid

from PIL import Image, ImageChops, ImageStat
from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication

from .capture_visibility import exclude_capture, restore_capture
from .external import ExternalTools
from .planner import LocalPlanner
from .tools import Tools
from .windows import WindowsDesktop


def same_target(before, after, action):
    """复核规划期间的目标变化；键盘动作保守比较全帧。"""
    keys = ('left', 'top', 'physical_width', 'physical_height', 'source_width', 'source_height', 'width', 'height', 'foreground')
    if any(before.get(k) != after.get(k) for k in keys):
        return False
    try:
        with (Image.open(io.BytesIO(base64.b64decode(before['image_b64']))) as old,
              Image.open(io.BytesIO(base64.b64decode(after['image_b64']))) as new):
            old, new = old.convert('RGB'), new.convert('RGB')
            if old.size != new.size:
                return False
            if 'x' not in action:
                # 不能以同一个HWND代替输入焦点/内容一致；无可靠焦点身份时宁可暂停。
                return ImageChops.difference(old, new).getbbox() is None
            sx = old.width / before.get('source_width', before.get('physical_width', old.width))
            sy = old.height / before.get('source_height', before.get('physical_height', old.height))
            points = [('x', 'y')] + ([('to_x', 'to_y')] if action['name'] == 'drag' else [])
            for xkey, ykey in points:
                x, y = (action[xkey] - before['left']) * sx, (action[ykey] - before['top']) * sy
                box = (max(0, int(x - 48*sx)), max(0, int(y - 48*sy)),
                       min(old.width, int(x + 48*sx) + 1), min(old.height, int(y + 48*sy) + 1))
                if box[2] <= box[0] or box[3] <= box[1]:
                    return False
                diff = ImageChops.difference(old.crop(box), new.crop(box))
                if max(ImageStat.Stat(diff).mean) >= 8:
                    return False
            return True
    except (ValueError, OSError, KeyError):
        return False


class DesktopControl(QObject):
    completed = pyqtSignal(object)
    progress = pyqtSignal(int, str)

    def __init__(self, controller, panel, root, *, backend=None, planner_factory=None):
        super().__init__(controller)
        self.controller, self.panel, self.root = controller, panel, root
        self.backend = backend or WindowsDesktop()
        self.tools = Tools(lambda: self.panel.store.load(), self.backend)
        self.planner_factory = planner_factory or self._planner
        self.window = None
        self.closed, self.epoch, self.workers = False, 0, 0
        self.state, self.goal, self.history = 'idle', '', []
        self._task_profile, self._task_profile_id = None, None
        self._external_active = False
        self.external = ExternalTools(self)
        self.steps, self.elapsed = 0, 0.
        self._hidden, self._motion, self.observation = [], None, None
        self._capture_guards = []
        self._capture_exclusions = {}
        self._voice_pending = False
        self._plan_cancel = threading.Event()
        self._bubble_text = ''
        self._pending_action, self._pending_message = None, ''
        settled = getattr(controller.voice_input, 'settled', None)
        if settled is not None:
            settled.connect(self._voice_settled)
        self._last_tick, self._input = time.monotonic(), self.backend.input_stamp()
        self.completed.connect(self._complete)
        self.progress.connect(self._progress)
        panel.settings_saved.connect(self.apply)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        frame_timer = getattr(controller.pet, 'physics_timer', None)
        if isinstance(frame_timer, QTimer):
            self.motion_timer = frame_timer
            self._own_motion_timer = False
        else:
            self.motion_timer = QTimer(self)
            self.motion_timer.setInterval(16)
            self._own_motion_timer = True
        self.motion_timer.timeout.connect(self._move_tick)
        controller.pet.installEventFilter(self)
        controller.pet._desktop_control_active = False

    @property
    def busy(self):
        return self.state not in ('idle', 'finished') or self._external_active

    def open(self):
        if self.closed:
            return
        if self.window is None:
            from ui.desktop_control import ControlWindow
            self.window = ControlWindow()
            self.window.submitted.connect(self.submit)
            self.window.voice_requested.connect(self.voice)
            self.window.pause_requested.connect(self.pause)
            self.window.resume_requested.connect(self.resume)
            self.window.stop_requested.connect(self.stop)
            self.window.new_requested.connect(self.new)
            self.window.model_selected.connect(self.select_model)
        self._refresh_models()
        self.window.show()
        self.window.hide()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self._show('就绪：输入目标或使用麦克风。' if not self.busy else self.state)

    def _refresh_models(self):
        if self.window:
            self.window.set_models(self.controller.bridge.config['profiles'], self.panel.config['profile_id'])

    def select_model(self, identity):
        if self.closed:
            return
        known = {'original'} | {p['id'] for p in self.controller.bridge.config['profiles']}
        if identity not in known:
            self._show('所选操作模型已移除，请重新选择。')
            self._refresh_models()
            return
        value = dict(self.panel.config, profile_id=identity)
        try:
            self.panel.store.save(value)
        except (ValueError, OSError):
            if self.window:
                self.window.model_hint.setText('模型选择保存失败，仍使用原配置。')
            self._refresh_models()
            return
        # 只修改已保存的模型字段，不顺带保存设置页其他未提交内容或停止当前任务。
        self.panel.config = value
        self.panel.refresh_profiles()
        self.panel.profile.setCurrentIndex(self.panel.profile.findData(identity))
        self._refresh_models()
        if self.window:
            self.window.model_hint.setText('已全局永久保存；当前任务沿用原模型，下一任务生效。' if self.busy else '已全局永久保存，后续文字/语音操作均使用此模型。')

    def _show(self, text):
        if self.window:
            self.window.set_state(text, busy=self.state not in ('idle', 'finished', 'paused', 'ask'))

    def _append(self, role, text):
        if self.window:
            self.window.append(role, text)
        path = self.root / '桌面操作记录.jsonl'
        try:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': time.time(), 'role': role, 'text': text}, ensure_ascii=False) + '\n')
        except OSError:
            self._show('无法写入本地操作日志，请检查工作区空间及权限。')

    def _claim(self, active):
        previous = getattr(self.controller.pet, '_desktop_control_active', False)
        self.controller.pet._desktop_control_active = active
        if not active:
            exclusions, self._capture_exclusions = self._capture_exclusions, {}
            for guard in reversed(list(exclusions.values())):
                restore_capture(guard)
        if active and not previous:
            interaction = getattr(self.controller.pet, 'feature_interaction', None)
            if interaction:
                interaction.stop()

    def _planner(self):
        from smart_models import SmartModelClient
        epoch = self.epoch
        if self._task_profile is not None:
            return LocalPlanner(copy.deepcopy(self._task_profile))
        chosen = self._task_profile_id or self.panel.config['profile_id']
        if chosen == 'original':
            profile = SmartModelClient(self.controller, {}, self.controller.bridge.config)._original_profile()
        else:
            profile = next((p for p in self.controller.bridge.config['profiles'] if p['id'] == chosen), None)
            if not profile:
                raise ValueError('所选操作模型已删除，请重新选择')
        # Vertex凭据解析等可能阻塞，旧任务初始化完成后不得覆盖新任务快照。
        if not self.closed and epoch == self.epoch:
            self._task_profile = copy.deepcopy(profile)
        return LocalPlanner(copy.deepcopy(profile))

    def submit(self, text):
        if self.closed:
            return
        text = str(text).strip()
        if not text or len(text) > 8000:
            self._show('请输入1至8000字的操作目标。')
            return
        if self.state not in ('idle', 'finished', 'paused', 'ask'):
            return
        if (getattr(self.controller, '_is_analyzing', False)
                or self.controller.voice_input.busy or self.controller.bridge.summary_busy
                or (getattr(self.controller, 'screenshot_qa', None) and self.controller.screenshot_qa.busy)):
            self._show('请等待当前聊天、答疑或语音识别结束。')
            return
        try:
            if self.state in ('ask', 'paused'):
                self.goal += '\n用户补充：' + text
                if len(self.goal) > 12000:
                    raise ValueError('补充内容过长，请开启新任务')
            else:
                self.goal, self.history, self.steps, self.elapsed = text, [], 0, 0.
                self._task_profile, self._task_profile_id = None, self.panel.config['profile_id']
            self.tools.begin('local')
        except ValueError as exc:
            self._show('未开始：' + self._error(exc))
            return
        self.epoch += 1
        self._plan_cancel.set()
        self._plan_cancel = threading.Event()
        self._bubble_text = ''
        self._append('我', text)
        self._claim(True)
        self._input, self._last_tick = self.backend.input_stamp(), time.monotonic()
        self._observe()

    def _exclude_own_windows(self):
        if self.closed or (self.state in ('idle', 'finished', 'paused', 'ask') and not self._external_active):
            return
        for widget in (self.window, getattr(self.controller.pet, 'bubble', None), self.controller.pet):
            if widget is not None and widget.isVisible():
                handle = int(widget.winId())
                if handle not in self._capture_exclusions:
                    guard = exclude_capture(widget, passthrough=False)
                    if guard is not None:
                        self._capture_exclusions[handle] = guard

    def _hide_capture(self):
        self._exclude_own_windows()
        self._hidden = []
        for widget in (self.window, getattr(self.controller.pet, 'bubble', None), self.controller.pet):
            if widget is not None and widget.isVisible():
                guard = exclude_capture(widget)
                if guard is not None:
                    self._capture_guards.append(guard)
                else:
                    self._hidden.append(widget)
                    widget.hide()

    def _restore_capture(self):
        guards, self._capture_guards = self._capture_guards, []
        for guard in reversed(guards):
            restore_capture(guard)
        hidden, self._hidden = self._hidden, []
        for widget in hidden:
            # 后台截图恢复不夺取目标应用的键盘焦点。
            old = widget.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            widget.show()
            widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, old)

    def _spawn(self, stage, work, epoch=None):
        epoch = self.epoch if epoch is None else epoch
        if self.closed or epoch != self.epoch:
            return
        self.workers += 1
        def run():
            try:
                value, error = work(), None
            except Exception as exc:  # noqa: BLE001 -- 工作线程边界，错误仅传回本地窗口。
                value, error = None, self._error(exc)
            try:
                self.completed.emit((epoch, stage, value, error))
            except RuntimeError:
                pass  # 程序退出后QObject可能已销毁，后台不得再访问界面。
        threading.Thread(target=run, name='桌面操作-' + stage, daemon=True).start()

    def _capture(self):
        before = self.backend.foreground()
        controls = self.tools.call('controls', {}).get('controls', {}) if self.panel.config['prefer_controls'] else {}
        shot = self.tools.call('screenshot', {})
        shot['controls'] = controls.get('items', []) if isinstance(controls, dict) else controls
        shot['foreground'] = self.backend.foreground()
        if before != shot['foreground']:
            raise ValueError('foreground_changed')
        shot['keyboard_target_available'] = bool(shot['foreground'].get('hwnd')) and shot['foreground'].get('pid') != os.getpid()
        return shot

    def _observe(self):
        self.state = 'observing'
        self._show('正在观察桌面…')
        self._hide_capture()
        epoch = self.epoch
        QTimer.singleShot(80, lambda: self._spawn('observe', self._capture, epoch))

    def _complete(self, event):
        epoch, stage, value, error = event
        self.workers = max(0, self.workers - 1)
        if self.closed or epoch != self.epoch:
            return
        if stage in ('observe', 'execute'):
            self._restore_capture()
        if error:
            changes = {'foreground_changed': '前台窗口发生变化', 'stale_observation': '本轮截图已过期',
                       'target_changed': '目标位置或外观发生变化', 'observation_changed': '发送前再次检查发现画面变化',
                       'own_window_focused': '焦点仍在桌宠窗口，需先点击目标应用再输入'}
            if error in changes:
                reason = changes[error]
                feedback = ((self._pending_message + '\n') if stage == 'execute' and self._pending_message else '')
                feedback += '复核结果：' + reason + '，本次未发送输入。'
                feedback += '暂停后继续将重新定位。' if self.panel.config['pause_on_input'] else '正在重新观察并定位。'
                if stage == 'execute' and self._pending_action is not None:
                    self.history.append({'action': self._pending_action, 'result': '未发送输入：' + reason})
                self._append('复核', feedback)
                self._model_message(feedback)
                if self.panel.config['pause_on_input']:
                    self.pause('目标窗口或按钮位置发生变化；继续后重新观察。')
                else:
                    # 只重做发送前被拒绝的观察/计划；未知发送结果不进入此分支。
                    self.steps += 1
                    if self.steps >= self.panel.config['max_steps']:
                        self.stop('已达到本任务操作与重新观察次数上限。')
                    else:
                        self.state = 'waiting'
                        self._show('窗口或目标发生变化，正在重新观察并继续…')
                        current_epoch = self.epoch
                        QTimer.singleShot(250, lambda: self._observe()
                            if not self.closed and current_epoch == self.epoch else None)
            else:
                description = self._bubble_text[:500]
                self.stop((description + '\n' if description else '') + '执行停止：' + error)
            return
        if stage == 'observe':
            self.observation = value
            self.state = 'planning'
            self._show('正在判断下一步…')
            goal, history = self.goal, copy.deepcopy(self.history)
            plan_epoch, cancel = self.epoch, self._plan_cancel
            def plan():
                planner = self.planner_factory()
                streamed = getattr(planner, 'next_stream', None)
                if streamed is None:
                    return planner.next(goal, value, history)
                last_progress = 0.
                def update(text):
                    nonlocal last_progress
                    now = time.monotonic()
                    if not cancel.is_set() and now - last_progress >= .08:
                        last_progress = now
                        try:
                            self.progress.emit(plan_epoch, text)
                        except RuntimeError:
                            pass
                return streamed(goal, value, history, update, cancel)
            self._spawn('plan', plan)
        elif stage == 'plan':
            self._accept(value)
        elif stage == 'execute':
            self._input = self.backend.input_stamp()
            self.steps += 1
            self.history.append(value)
            self._append('操作', json.dumps(value['action'], ensure_ascii=False) + '（输入已发送，正在检查结果）')
            self._observe()

    def _accept(self, action):
        action = dict(action)
        message = action.pop('message', '')
        name = action['name']
        fallback = {'move': '正在移动鼠标到目标位置。', 'click': '准备点击目标。',
                    'drag': '准备拖动目标。', 'scroll': '准备滚动当前区域。',
                    'type': '准备在当前输入位置填写文字。', 'hotkey': '准备发送组合键。',
                    'wait': '等待界面更新后继续检查。'}
        if name not in ('finish', 'ask'):
            self._pending_message = message or fallback.get(name, '正在处理…')
            self._pending_action = copy.deepcopy(action)
            self._model_message(self._pending_message)
            self._append('桌宠', self._pending_message)
        if name == 'finish':
            self.stop('已结束：' + action['text'] + '\n模型判断依据：' + action['evidence'])
        elif name == 'ask':
            self.tools.stop()
            self.state = 'ask'
            self._claim(False)
            self._append('桌宠', action['text'])
            self._model_message(action['text'])
            self._show('等待补充：' + action['text'])
        elif self.steps >= self.panel.config['max_steps']:
            self.stop('已达到本任务操作步数上限。')
        elif name == 'wait':
            self.steps += 1
            self.history.append({'action': action, 'result': '等待后重新观察'})
            self.state = 'waiting'
            epoch = self.epoch
            QTimer.singleShot(round(action['seconds'] * 1000), lambda: self._observe() if epoch == self.epoch and not self.closed else None)
        elif self.panel.config['move_pet'] and 'x' in action:
            self._approach(action)
        else:
            self._execute(action)

    def _approach(self, action):
        pet, point = self.controller.pet, None
        interaction = getattr(pet, 'feature_interaction', None)
        if interaction:
            interaction.stop()
        pet._desktop_auto_moving = True
        monitors = self.backend.monitors()
        for index, monitor in enumerate(monitors):
            if monitor['left'] <= action['x'] < monitor['left'] + monitor['width'] and monitor['top'] <= action['y'] < monitor['top'] + monitor['height']:
                screens = QApplication.screens()
                screen = next((s for s in screens if s.name() == monitor.get('name')), screens[min(index, len(screens)-1)])
                area = screen.geometry()
                x = area.x() + (action['x'] - monitor['left']) * area.width() / monitor['width']
                y = area.y() + (action['y'] - monitor['top']) * area.height() / monitor['height']
                # 角色在目标左侧或右侧，实际点击位置保持空出。
                left = x - pet.width() - 24
                if left < area.left():
                    left = x + 24
                point = QPoint(round(left), round(y - pet.height() / 2))
                point.setX(max(area.left(), min(point.x(), area.right() + 1 - pet.width())))
                point.setY(max(area.top(), min(point.y(), area.bottom() + 1 - pet.height())))
                break
        if point is None:
            self.stop('目标坐标不在有效显示器上。')
            return
        self.state = 'moving'
        self._show('正在移动到目标旁…')
        origin = pet.pos()
        begin = getattr(pet, 'begin_desktop_move', None)
        if begin:
            begin(point.x() - origin.x())
        duration = max(.7, min(8., ((point-origin).manhattanLength() / 300)))
        self._motion = (time.monotonic(), duration, origin, point, action)
        if self._own_motion_timer:
            self.motion_timer.start()

    def _execute(self, action):
        if self.external.pending is not None and action is self.external.pending['action']:
            self.external.execute()
            return
        self.state = 'executing'
        self._show('正在复核目标并发送输入…')
        self._hide_capture()
        before, epoch, cancel = self.observation, self.epoch, self._plan_cancel
        def work():
            def check_cancelled():
                if cancel.is_set() or self.closed or epoch != self.epoch:
                    raise ValueError('cancelled')
            for attempt in range(2):
                check_cancelled()
                after = self.tools.call('screenshot', {})
                check_cancelled()
                after['foreground'] = self.backend.foreground()
                if action['name'] in ('type', 'hotkey') and after['foreground'].get('pid') == os.getpid():
                    raise ValueError('own_window_focused')
                if before.get('foreground') != after.get('foreground'):
                    raise ValueError('foreground_changed')
                if same_target(before, after, action):
                    break
                if attempt or cancel.wait(.08):
                    raise ValueError('cancelled' if cancel.is_set() else 'target_changed')
            name = action['name']
            payload = {k: v for k, v in action.items() if k != 'name'}
            payload.update(observation_id=after['observation_id'], request_id=uuid.uuid4().hex)
            check_cancelled()
            result = self.tools.call(name, payload)
            return {'action': action, 'result': result.get('status', 'sent')}
        QTimer.singleShot(80, lambda: self._spawn('execute', work, epoch))

    def _tick(self):
        now, previous = time.monotonic(), self._last_tick
        self._last_tick = now
        if self.closed:
            return
        if self.external.pending is not None:
            self.external.tick()
            return
        if self.state in ('idle', 'finished'):
            if self._external_active:
                try:
                    self._external_active = self.tools.call('status')['active']
                except (ValueError, OSError):
                    self.tools.stop()
                    self._external_active = False
                self._claim(self._external_active)
            return
        if self.state in ('paused', 'ask'):
            return
        self.elapsed += min(now - previous, .5)
        if self.elapsed >= self.panel.config['max_seconds']:
            self.stop('本任务已达到执行时长上限。')
            return
        if self.state != 'executing' and self.panel.config['pause_on_input']:
            stamp = self.backend.input_stamp()
            if stamp != self._input:
                self._input = stamp
                self.pause('检测到你的键鼠操作，任务已暂停。')
                return
    def _move_tick(self):
        if self._motion and not self.closed:
            now = time.monotonic()
            started, duration, origin, target, action = self._motion
            fraction = min(1., (now - started) / duration)
            smooth = fraction * fraction * (3 - 2 * fraction)
            self.controller.pet.move(origin + (target-origin) * smooth)
            if fraction >= 1:
                self._end_motion()
                self._execute(action)

    def _end_motion(self):
        self._motion = None
        self.controller.pet._desktop_auto_moving = False
        if self._own_motion_timer:
            self.motion_timer.stop()
        end = getattr(self.controller.pet, 'end_desktop_move', None)
        if end:
            end()

    def eventFilter(self, watched, event):
        if (watched is self.controller.pet and self.busy
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
                and (self._motion is not None or self.external.pending is not None)):
            if self.external.pending is not None:
                self.stop('你正在拖动桌宠，本次云端输入已取消。')
            else:
                self.pause('你正在拖动桌宠，自动操作已暂停。')
            return False
        if (watched is self.controller.pet and self.busy and self.panel.config['pause_on_input']
                and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)):
            self.pause('你正在与桌宠互动，任务已暂停。')
        return False

    def pause(self, text='已暂停，继续时会重新观察桌面。'):
        if self.closed or self.state in ('idle', 'finished', 'ask', 'paused'):
            return
        self.epoch += 1
        self._plan_cancel.set()
        self.tools.stop()
        self._end_motion()
        self._restore_capture()
        self.state = 'paused'
        self._claim(False)
        self._show(text)

    def resume(self):
        if self.closed or self.state != 'paused':
            return
        try:
            self.tools.begin('local')
        except ValueError as exc:
            self._show(self._error(exc))
            return
        self.epoch += 1
        self._plan_cancel = threading.Event()
        self._claim(True)
        self._input = self.backend.input_stamp()
        self._observe()

    def stop(self, text='已停止桌面操作。'):
        self.epoch += 1
        self._plan_cancel.set()
        self.tools.stop()
        self.external.cancel()
        self._external_active = False
        self._end_motion()
        self._restore_capture()
        if self._voice_pending:
            self.controller.voice_input.cancel()
            self._voice_pending = False
        self.state = 'finished'
        self._claim(False)
        if not self.closed:
            self._show(text)
            self._append('桌宠', text)
            self._model_message(text)

    def _progress(self, epoch, text):
        if not self.closed and epoch == self.epoch and self.state == 'planning':
            self._model_message(text)

    def _model_message(self, text):
        """公开说明更新到Qt窗口和角色气泡，避免每片段重新启动打字机。"""
        if self.closed or not text:
            return
        text = str(text)[:4500]
        if self.window:
            show = getattr(self.window, 'show_model_message', None)
            if show:
                show(text)
        if text == self._bubble_text:
            return
        self._bubble_text = text
        pet = self.controller.pet
        pet.show_critique(text)
        self._exclude_own_windows()
        bubble = getattr(pet, 'bubble', None)
        if bubble is not None:
            # 流式片段本身就是逐字呈现，不再叠加原气泡打字机延迟。
            bubble.type_timer.stop()
            bubble._current_index = len(text)
            bubble.label.setText(text)

    def new(self):
        self.stop()
        self._task_profile, self._task_profile_id = None, None
        self.goal, self.history, self.state = '', [], 'idle'
        if self.window:
            self.window.log.clear()
            self.window.show_model_message('输入操作目标，我会边观察边告诉你进展。')
        self._show('新任务：输入目标或使用麦克风。')

    def voice(self):
        self.open()
        if self._voice_pending:
            self.controller.voice_input.finish()
            return
        self.pause()
        self._voice_pending = True
        epoch = self.epoch
        def delivered(text):
            self._voice_pending = False
            if not self.closed and epoch == self.epoch:
                self.submit(text)
        self.controller.voice_input.toggle_for(delivered)

    def _voice_settled(self, _success):
        self._voice_pending = False

    def apply(self, config):
        self.stop('桌面操作设置已更新，旧任务已停止。')
        self.panel.config = dict(config)
        self._refresh_models()

    def external_tool(self, name, payload=None, *, cancel=None, deadline=None):
        """复用本地移动及观察复核；同步返回真实原子结果，不调用本地模型。"""
        return self.external.call(name, payload, cancel, deadline)

    def external_tool_context(self, name, payload, cancel, deadline):
        """新版传输提供取消标记和截止时间，旧调用保持兼容。"""
        return self.external_tool(name, payload, cancel=cancel, deadline=deadline)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop()
        self.timer.stop()
        self.motion_timer.timeout.disconnect(self._move_tick)
        self.controller.pet.removeEventFilter(self)
        if self.window:
            self.window.hide()
            self.window.deleteLater()

    @staticmethod
    def _error(exc):
        hints = {'disabled': '请先在设置中心“桌面操作”页启用并保存',
                 'cloud_disabled': '本地未允许外部桌面控制', 'busy': '已有任务或输入尚未结束',
                 'original_not_allowed': '本地未允许云端获取原图'}
        text = str(exc) if isinstance(exc, ValueError) else '底层操作失败：' + type(exc).__name__
        return hints.get(text, text)
