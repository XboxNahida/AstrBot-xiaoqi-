"""鼠标框选虚拟桌面；跨屏坐标在每屏按DPR转换。"""
from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QPainter, QPen
from PyQt6.QtWidgets import QApplication, QWidget


class RegionSelector(QWidget):
    selected = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint |
                            Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.screens = [(screen.geometry(), screen.grabWindow(0), screen.devicePixelRatio())
                        for screen in QApplication.screens()]
        area = QRect()
        for geometry, _, _ in self.screens:
            area = area.united(geometry)
        self.setGeometry(area)
        self.origin = area.topLeft()
        self.start = self.end = QPoint()
        self.dragging = False
        self.finished = False
        self.setCursor(Qt.CursorShape.CrossCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        for geometry, pixmap, _ratio in self.screens:
            painter.drawPixmap(geometry.translated(-self.origin), pixmap)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 95))
        if self.dragging:
            rect = QRect(self.start, self.end).normalized()
            painter.setPen(QPen(QColor("#ff4e88"), 2))
            painter.drawRect(rect)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(20, 32, "拖动框选区域 · 松开完成 · Esc 取消")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start = self.end = event.position().toPoint()
            self.dragging = True
            self.update()

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.dragging or event.button() != Qt.MouseButton.LeftButton:
            return
        self.end = event.position().toPoint()
        rect = QRect(self.start, self.end).normalized().translated(self.origin)
        if rect.width() < 8 or rect.height() < 8:
            self.close()
            return
        image = QImage(rect.size(), QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.black)
        painter = QPainter(image)
        for geometry, pixmap, ratio in self.screens:
            overlap = rect.intersected(geometry)
            if overlap.isEmpty():
                continue
            source = overlap.translated(-geometry.topLeft())
            source = QRect(round(source.x()*ratio), round(source.y()*ratio),
                           round(source.width()*ratio), round(source.height()*ratio))
            painter.drawPixmap(overlap.translated(-rect.topLeft()), pixmap, source)
        painter.end()
        self.finished = True
        self.selected.emit(image)
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def closeEvent(self, event):
        if not self.finished:
            self.finished = True
            self.cancelled.emit()
        super().closeEvent(event)
