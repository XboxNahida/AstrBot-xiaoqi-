from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from ui.region_selector import RegionSelector

_APP = QApplication.instance() or QApplication(['qa_selector'])


def test_window_manager_close_signals_cancel_once_without_capture(monkeypatch):
    monkeypatch.setattr(QApplication, 'screens', list)
    selector = RegionSelector()
    events = []
    selector.cancelled.connect(lambda: events.append('cancel'))
    selector.close()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert events == ['cancel']


def test_reverse_cross_screen_crop_uses_screen_dpr(monkeypatch):
    monkeypatch.setattr(QApplication, 'screens', list)
    selector = RegionSelector()
    first, second = QImage(100, 100, QImage.Format.Format_RGB32), QImage(200, 200, QImage.Format.Format_RGB32)
    first.fill(Qt.GlobalColor.red)
    second.fill(Qt.GlobalColor.blue)
    selector.screens = [(QRect(-100, 0, 100, 100), QPixmap.fromImage(first), 1),
                        (QRect(0, 0, 100, 100), QPixmap.fromImage(second), 2)]
    selector.origin = QPoint(-100, 0)
    selector.start, selector.dragging = QPoint(150, 80), True
    class Event:
        def button(self):
            return Qt.MouseButton.LeftButton
        def position(self):
            from PyQt6.QtCore import QPointF
            return QPointF(50, 20)
    images, cancelled = [], []
    selector.selected.connect(images.append)
    selector.cancelled.connect(lambda: cancelled.append(True))
    selector.mouseReleaseEvent(Event())
    assert len(images) == 1 and not cancelled
    assert images[0].pixelColor(10, 10) == Qt.GlobalColor.red
    assert images[0].pixelColor(90, 10) == Qt.GlobalColor.blue
