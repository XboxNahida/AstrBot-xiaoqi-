"""本地鼠标物理；原左键点击/五连击仍由角色处理。"""
import math
import time

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
from PyQt6.QtWidgets import QApplication

from .events import action_key
from .motion import MotionState, VelocityTracker
from .motion_bounds import BodyBounds, frame_bounds


class PetInteraction(QObject):
    def __init__(self, pet, *, clock=None, blocked=None):
        super().__init__(pet)
        self.pet, self.cfg = pet, {}
        self.clock = clock or time.monotonic
        self.blocked = blocked or (lambda: False)
        self.motion, self.tracker = MotionState(), VelocityTracker()
        self.body_bounds = BodyBounds()
        self.closed, self.epoch = False, 0
        self._start = self._target = self._middle = None
        self._moved = False
        self._rope_v = [0., 0.]
        self._last = self.last_interaction = self.clock()
        self.squash = 0.
        self.sleep_key = None
        self.visual = None
        self._input_stamp = self._input_clock = 0.
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        # 动态角色的绘制与位移共用帧时钟，避免两个原生定时器不同步。
        frame_timer = getattr(pet, "physics_timer", None)
        self._frame_timer = frame_timer if isinstance(frame_timer, QTimer) else None
        if self._frame_timer is not None:
            self._frame_timer.timeout.connect(self.tick)
        pet.installEventFilter(self)

    def configure(self, cfg):
        self.stop()
        self.wake()
        self.cfg = dict(cfg)
        self._last = self.last_interaction = self.clock()
        if self.cfg.get("enabled", False) and self._frame_timer is None:
            self.timer.start()
        else:
            self.timer.stop()

    def stop(self):
        self.epoch += 1
        self.motion.stop()
        self._start = self._target = self._middle = None
        self._rope_v = [0., 0.]
        self.pet.feature_motion_active = False
        self.pet.feature_motion_input = False
        self.pet.feature_squash = 0.
        self.squash = 0.
        if self.visual:
            self.visual.clear()

    def _busy(self):
        pet = self.pet
        return (self.blocked() or not pet.isVisible()
                or any(getattr(pet, k, False) for k in ("_menu_open", "_thinking_active"))
                or QApplication.activePopupWidget() is not None
                or (getattr(pet, "interaction_dialog", None) is not None and pet.interaction_dialog.isVisible()))

    def _motion_blocked(self):
        # 对话/分析只影响自主休眠，不接管角色位置或清空手动物理。
        return self.blocked() or not self.pet.isVisible()

    def _move(self, x, y):
        self.pet.move(round(x), round(y))
        bubble = getattr(self.pet, "bubble", None)
        if bubble is not None and bubble.isVisible():
            if hasattr(self.pet, "_calc_bubble_pos"):
                bubble.move(*self.pet._calc_bubble_pos(bubble.width(), bubble.height()))
            else:
                bubble.move(self.pet.x() - bubble.width() + 45, self.pet.y() - bubble.height() - 6)

    def _event_time(self, event):
        stamp = event.timestamp()
        if self._input_stamp and stamp >= self._input_stamp:
            return self._input_clock + (stamp - self._input_stamp) / 1000
        return self.clock()

    def _bounds(self):
        pet = self.pet
        body = self.body_bounds.rect(pet)
        center = pet.pos() + body.center().toPoint()
        screen = QApplication.screenAt(center) or pet.screen()
        if screen is None:
            return None
        return frame_bounds(screen.geometry(), body)

    def _launch(self, velocity, epoch):
        if self.closed or epoch != self.epoch or not self.cfg.get("enabled") or self._motion_blocked():
            return
        bounds = self._bounds()
        x, y = self.pet.x(), self.pet.y()
        # 仅明确甩出才启动；屏外起点不传送，交给物理反向外向速度。
        if bounds is None:
            return
        if math.hypot(*velocity) < 180:
            return
        self.motion.launch(x, y, *velocity)
        self.pet.feature_motion_active = self.motion.active
        self._last = self.clock()

    def _released(self, velocity, epoch):
        if self.closed or epoch != self.epoch:
            return
        self.pet.feature_motion_input = False
        if velocity is not None:
            self._launch(velocity, epoch)

    def eventFilter(self, watched, event):
        if watched is not self.pet or self.closed or not self.cfg.get("enabled"):
            return False
        kind = event.type()
        # 失焦只取消尚未松手的输入；已发射/排队发射的惯性继续完成。
        if (kind == QEvent.Type.Hide
                or (kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse)
                    and self._start is not None)):
            self.stop()
            self.pet.is_dragging = False
            return False
        press = kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
        if press:
            self.stop()
            self.wake()
            self.last_interaction = self.clock()
            # 思考动画和对话窗口不禁用弹性跟手、甩抛或弹弓。
            if self._motion_blocked():
                return False
            self._input_stamp, self._input_clock = event.timestamp(), self.clock()
            button = event.button()
            point = event.globalPosition().toPoint()
            if button == Qt.MouseButton.MiddleButton and self.cfg.get("sling_enabled", True) and not self.blocked():
                self.pet._click_count = 0
                self.pet.feature_motion_input = True
                self._middle = (point, self.pet.pos())
                self._start, self._moved = point, False
                self.pet.feature_motion_active = True
                return True
            if button == Qt.MouseButton.LeftButton and not self.blocked():
                self.pet.feature_motion_input = True
                self._start, self._moved = point, False
                self._offset = point - self.pet.pos()
                self.tracker.clear()
                self.tracker.sample(point.x(), point.y(), self._event_time(event))
                if self.cfg.get("click_enabled", True):
                    self.squash = .16
            return False
        if kind == QEvent.Type.MouseMove and self._start is not None:
            point = event.globalPosition().toPoint()
            self._moved |= (point - self._start).manhattanLength() >= QApplication.startDragDistance()
            if self._middle is not None:
                if self._moved:
                    delta = point - self._middle[0]
                    self._move(self._middle[1].x() + delta.x(), self._middle[1].y() + delta.y())
                return True
            self.tracker.sample(point.x(), point.y(), self._event_time(event))
            if self._moved and self.cfg.get("rope_enabled", True):
                self._target = point - self._offset
                self.pet._press_moved = True
                self.pet._click_count = 0
                self.pet.feature_motion_active = True
                return True
        if kind == QEvent.Type.MouseButtonRelease:
            point = event.globalPosition().toPoint()
            epoch = self.epoch
            if self._middle is not None:
                anchor, _pos = self._middle
                moved = self._moved
                self._start = self._target = self._middle = None
                self.pet.feature_motion_active = False
                if moved:
                    power = self.cfg.get("sling_power", 8.)
                    velocity = ((anchor.x() - point.x()) * power, (anchor.y() - point.y()) * power)
                    QTimer.singleShot(0, lambda: self._released(velocity, epoch))
                elif hasattr(self.pet, "cycle_next_outfit"):
                    self.pet.feature_motion_input = False
                    self.pet.cycle_next_outfit()
                return True
            if event.button() == Qt.MouseButton.LeftButton and self._start is not None:
                moment = self._event_time(event)
                self.tracker.sample(point.x(), point.y(), moment)
                velocity = self.tracker.velocity(moment)
                moved = self._moved
                fling = moved and self.cfg.get("fling_enabled", True) and math.hypot(*velocity) >= 600
                if moved and not fling and self._target is not None:
                    target = point - self._offset
                    self._move(target.x(), target.y())
                self._start = self._target = None
                self.pet.feature_motion_active = False
                QTimer.singleShot(0, lambda: self._released(velocity if fling else None, epoch))
            self.last_interaction = self.clock()
        return False

    def tick(self, dt=None):
        if self.closed or not self.cfg.get("enabled"):
            return
        now = self.clock()
        elapsed = min(.05, max(0., now - self._last if dt is None else dt))
        self._last = now
        busy = self._busy()
        if getattr(self.pet, "feature_sleeping", False):
            if busy or getattr(self.pet, "current_action_key", None) != self.sleep_key:
                self.wake()
            if self.visual:
                self.visual.advance(elapsed, self)
            return
        if self._motion_blocked():
            self.stop()
            self.last_interaction = now
            return
        if self._target is not None:
            x, y = float(self.pet.x()), float(self.pet.y())
            # 有阻尼的弹性跟手；只约束每步时长，不钳制桌面位置。
            steps = max(1, math.ceil(elapsed / .008))
            for _ in range(steps):
                step = elapsed / steps
                self._rope_v[0] += ((self._target.x() - x) * 150 - self._rope_v[0] * 22) * step
                self._rope_v[1] += ((self._target.y() - y) * 150 - self._rope_v[1] * 22) * step
                x += self._rope_v[0] * step
                y += self._rope_v[1] * step
            self._move(x, y)
        elif self.motion.active:
            result = self.motion.step(elapsed, self._bounds(), self.cfg)
            self._move(self.motion.x, self.motion.y)
            if result["bounced"] and self.cfg.get("click_enabled", True):
                self.squash = min(.18, result["impact"] / 6000)
            self.pet.feature_motion_active = self.motion.active
            if not self.motion.active:
                manager = getattr(self.pet, "config_mgr", None)
                if manager is not None:
                    manager.save({"pet_pos_x": self.pet.x(), "pet_pos_y": self.pet.y()})
        if self.squash > .001:
            self.pet.feature_squash = self.squash
            self.squash *= math.exp(-elapsed * 14)
            self.pet.update()
        elif getattr(self.pet, "feature_squash", 0):
            self.pet.feature_squash = 0.
            self.pet.update()
        if self.visual:
            self.visual.advance(elapsed, self)
        if (self.cfg.get("auto_sleep", False) and self._start is None and not self.motion.active
                and now - self.last_interaction >= self.cfg.get("sleep_minutes", 10) * 60):
            self.sleep()

    def sleep(self):
        if not self.cfg.get("enabled") or self._busy() or self._start is not None:
            return
        self.stop()
        self.sleep_key = action_key("sleep", getattr(self.pet, "actions", {}))
        self.pet.feature_sleeping = True
        if self.sleep_key and hasattr(self.pet, "play_action"):
            self.pet.play_action(self.sleep_key, loop=True)
        if self.visual:
            self.visual.advance(0., self)

    def wake(self):
        if getattr(self.pet, "feature_sleeping", False):
            self.pet.feature_sleeping = False
            if self.visual:
                self.visual.clear()
            if getattr(self.pet, "current_action_key", None) == self.sleep_key and hasattr(self.pet, "set_action"):
                self.pet.set_action("idle")
        self.last_interaction = self.clock()

    def close(self):
        if self.closed:
            return
        self.stop()
        self.wake()
        self.closed = True
        self.timer.stop()
        if self._frame_timer is not None:
            self._frame_timer.timeout.disconnect(self.tick)
        self.pet.removeEventFilter(self)
        if self.visual:
            self.visual.close()
