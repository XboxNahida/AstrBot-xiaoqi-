"""只读交互状态的有限透明表现层，不接收输入、不调用模型。"""
import math

from PyQt6.QtCore import QPointF, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget


class InteractionVisual(QWidget):
    """独立小范围覆盖窗；坐标保持全局坐标以支持负坐标屏幕。"""

    def __init__(self, pet):
        super().__init__(None)
        self.pet = pet
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput
        )
        for attribute in (Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_TranslucentBackground):
            self.setAttribute(attribute)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.points = []
        self.sling = None
        self.sleep = False
        self._previous = self._emitted = None
        self._sleep_rect = None
        self._closed = False

    def clear(self):
        self.points.clear()
        self.sling = None
        self.sleep = False
        self._previous = self._emitted = self._sleep_rect = None
        self.hide()

    def advance(self, dt, interaction):
        if self._closed or not interaction.cfg.get("enabled", False):
            self.clear()
            return
        elapsed = float(dt)
        if not math.isfinite(elapsed) or elapsed < 0:
            elapsed = 0.
        self.points = [(x, y, life - elapsed) for x, y, life in self.points
                       if life > elapsed]
        center = self.pet.mapToGlobal(self.pet.rect().center())
        current = (center.x(), center.y())
        speed = 0.
        moving = (interaction.motion.active
                  or (interaction._start is not None and interaction._moved)
                  or interaction._target is not None)
        if interaction.motion.active:
            speed = math.hypot(interaction.motion.vx, interaction.motion.vy)
        if moving and self._previous is not None and elapsed > 0:
            speed = max(speed, math.dist(current, self._previous) / elapsed)
        if interaction.cfg.get("trail_enabled", True):
            if (moving and speed >= 180 and math.isfinite(speed)
                    and (self._emitted is None or math.dist(current, self._emitted) >= 12)):
                self.points.append((*current, .4))
                self.points = self.points[-24:]
                self._emitted = current
        else:
            self.points.clear()
            self._emitted = None
        self._previous = current
        self.sling = None
        if (interaction.cfg.get("sling_enabled", True)
                and interaction._middle is not None and interaction._moved):
            anchor = interaction._middle[0]
            self.sling = ((anchor.x(), anchor.y()), current)
        self.sleep = bool(getattr(self.pet, "feature_sleeping", False))
        self._sleep_rect = QRect(center.x() - 24, self.pet.y() - 30, 64, 26) if self.sleep else None
        self._fit_marks()

    def _fit_marks(self):
        bounds = None
        locations = [(x, y) for x, y, _life in self.points]
        if self.sling:
            locations.extend(self.sling)
        for x, y in locations:
            mark = QRect(round(x) - 7, round(y) - 7, 15, 15)
            bounds = mark if bounds is None else bounds.united(mark)
        if self._sleep_rect is not None:
            bounds = self._sleep_rect if bounds is None else bounds.united(self._sleep_rect)
        if bounds is None:
            self.hide()
            return
        self.setGeometry(bounds)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        offset = self.pos()

        def local(point):
            return QPointF(point[0] - offset.x(), point[1] - offset.y())

        painter.setPen(Qt.PenStyle.NoPen)
        for x, y, life in self.points:
            painter.setBrush(QColor(96, 190, 255, round(170 * min(1., life / .4))))
            painter.drawEllipse(local((x, y)), 3., 3.)
        if self.sling:
            painter.setPen(QPen(QColor(116, 204, 255, 190), 2.))
            painter.drawLine(local(self.sling[0]), local(self.sling[1]))
            painter.setBrush(QColor(116, 204, 255, 210))
            painter.drawEllipse(local(self.sling[0]), 4., 4.)
        if self._sleep_rect is not None:
            painter.setPen(QColor(138, 196, 255, 210))
            painter.drawText(self._sleep_rect.translated(-offset), Qt.AlignmentFlag.AlignCenter, "Zzz")
        painter.end()

    def closeEvent(self, event):
        self._closed = True
        self.clear()
        super().closeEvent(event)
