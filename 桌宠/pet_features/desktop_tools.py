"""低优先级桌面行为；原地模式始终保留用户坐标。"""
import math
import random
import time

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt
from PyQt6.QtGui import QAction, QCursor
from PyQt6.QtWidgets import QApplication


class DesktopTools(QObject):
    def __init__(self, pet, tray=None, *, clock=None, rng=None):
        super().__init__(pet)
        self.pet, self.cfg = pet, {}
        self._original_flags = pet.windowFlags()
        self._original_opacity = pet.windowOpacity()
        self._clock = clock or time.monotonic
        self._rng = rng or random.Random()
        self._target = None
        self._rest_until = 0.
        self._position = None
        self._last_position = None
        self._pressed = False
        self._closed = False
        self._rescue_action = None
        pet.installEventFilter(self)
        if tray is not None and tray.contextMenu() is not None:
            self._rescue_action = QAction("恢复桌宠窗口／解除穿透锁定", self)
            self._rescue_action.triggered.connect(self.rescue)
            tray.contextMenu().addAction(self._rescue_action)

    def _window(self, flags, opacity):
        pos, visible = self.pet.pos(), self.pet.isVisible()
        if self.pet.windowFlags() != flags:
            self.pet.setWindowFlags(flags)
            self.pet.move(pos)
            if visible:
                self.pet.show()
        self.pet.setWindowOpacity(opacity)

    def apply(self, cfg_behavior):
        previous = self.cfg
        self.cfg = dict(cfg_behavior)
        if (not self.cfg.get("enabled") or self.cfg.get("mode") != previous.get("mode")
                or self.cfg.get("locked")):
            self._target = self._position = self._last_position = None
            self._rest_until = 0.
        enabled = self.cfg.get("enabled", False)
        flags = self._original_flags
        if enabled:
            flags &= ~(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowStaysOnBottomHint | Qt.WindowType.WindowTransparentForInput)
            flags |= {"top": Qt.WindowType.WindowStaysOnTopHint, "bottom": Qt.WindowType.WindowStaysOnBottomHint}.get(self.cfg.get("layer", "top"), Qt.WindowType.Widget)
            if self.cfg.get("click_through", False):
                flags |= Qt.WindowType.WindowTransparentForInput
        opacity = max(0., min(1., float(self.cfg.get("opacity", 100)) / 100)) if enabled else self._original_opacity
        self._window(flags, opacity)
        self.pet.feature_flipped = bool(enabled and self.cfg.get("facing") == "left")
        self.pet.update()

    def eventFilter(self, watched, event):
        if watched is self.pet:
            kind = event.type()
            if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
                # 右键释放通常由菜单消费，不等待角色收到该释放。
                self._pressed = event.button() != Qt.MouseButton.RightButton
                self._target = self._position = self._last_position = None
                if not self._pressed:
                    self._rest_until = self._clock() + self._rng.uniform(6, 14)
            elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse,
                          QEvent.Type.Hide, QEvent.Type.WindowDeactivate):
                was_pressed = self._pressed
                self._pressed = False
                if was_pressed or kind == QEvent.Type.MouseButtonRelease:
                    self._rest_until = self._clock() + self._rng.uniform(6, 14)
                if kind != QEvent.Type.MouseButtonRelease:
                    self.pet.is_dragging = self.pet._dragging = False
            if (kind == QEvent.Type.MouseMove and self.cfg.get("enabled")
                    and self.cfg.get("locked") and event.buttons() & Qt.MouseButton.LeftButton):
                return True
        return False

    def _busy(self):
        return (self._pressed or not self.pet.isVisible()
                or getattr(self.pet, '_desktop_control_active', False)
                or any(getattr(self.pet, key, False) for key in ("is_dragging", "_dragging", "_menu_open", "_thinking_active", "feature_motion_active", "feature_sleeping"))
                or getattr(self.pet, "_loop_override", None) is not None
                or QApplication.activePopupWidget() is not None
                or (getattr(self.pet, "interaction_dialog", None) is not None and self.pet.interaction_dialog.isVisible()))

    def tick(self, elapsed_seconds=.1):
        mode = self.cfg.get("mode", "stationary")
        if self._closed or not self.cfg.get("enabled") or mode not in ("walk", "follow") or self._busy() or self.cfg.get("locked"):
            return
        if self._clock() < self._rest_until:
            return
        seconds = float(elapsed_seconds)
        speed = float(self.cfg.get("speed", 30))
        if not math.isfinite(seconds) or not math.isfinite(speed):
            return
        step = max(0, min(seconds, 1)) * max(0, min(speed, 1000))
        pos = self.pet.pos()
        if self._last_position != pos or self._position is None:
            self._position = (float(pos.x()), float(pos.y()))
            self._target = None
        x, y = self._position
        if mode == "follow":
            delta = QCursor.pos() - self.pet.frameGeometry().center()
            distance = math.hypot(delta.x(), delta.y())
            if distance <= 30:
                return
            dx, dy = delta.x(), delta.y()
        else:
            if self._target is None:
                screen = self.pet.screen() or QApplication.primaryScreen()
                if screen is None:
                    return
                area = screen.availableGeometry()
                right = max(area.left(), area.right() - self.pet.width() + 1)
                bottom = max(area.top(), area.bottom() - self.pet.height() + 1)
                self._target = (self._rng.uniform(area.left(), right), self._rng.uniform(area.top(), bottom))
            dx, dy = self._target[0] - x, self._target[1] - y
            distance = math.hypot(dx, dy)
        fraction = min(step / distance, 1) if distance else 1
        x, y = x + dx * fraction, y + dy * fraction
        target = QPoint(round(x), round(y))
        screen = self.pet.screen()
        if screen and mode == "follow" and self.cfg.get("snap"):
            area = screen.availableGeometry()
            target.setX(max(area.left(), min(target.x(), max(area.left(), area.right() - self.pet.width() + 1))))
            target.setY(max(area.top(), min(target.y(), max(area.top(), area.bottom() - self.pet.height() + 1))))
            x, y = float(target.x()), float(target.y())
        self._position = (x, y)
        self.pet.move(target)
        self._last_position = self.pet.pos()
        if mode == "walk" and fraction == 1:
            self._target = None
            self._rest_until = self._clock() + self._rng.uniform(8, 18)
        bubble = getattr(self.pet, "bubble", None)
        calc = getattr(self.pet, "_calc_bubble_pos", None)
        if bubble is not None and callable(calc):
            bubble.move(*calc(bubble.width(), bubble.height()))
        if self.cfg.get("flip"):
            facing = self.cfg.get("facing", "auto")
            self.pet.feature_flipped = facing == "left" or (facing == "auto" and dx < 0)
            self.pet.update()

    def rescue(self):
        interaction = getattr(self.pet, "feature_interaction", None)
        if interaction is not None:
            interaction.stop()
            interaction.wake()
        self.apply({"enabled": False, "locked": False, "click_through": False})
        screen = self.pet.screen() or QApplication.primaryScreen()
        if screen:
            area = screen.availableGeometry()
            self.pet.move(area.center() - self.pet.rect().center())
        self.pet.show()

    def close(self):
        if self._closed:
            return
        self.apply({"enabled": False})
        self._closed = True
        self.pet.removeEventFilter(self)
        if self._rescue_action:
            self._rescue_action.deleteLater()
