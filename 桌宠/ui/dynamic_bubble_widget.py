from typing import Optional
from PyQt6.QtWidgets import QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QBrush, QPolygon

class DynamicSpeechBubble(QWidget):
    """
    Compact, manga-style comic speech bubble tailored specifically for the dynamic pet.
    Tightly wraps text with minimal whitespace, and accurately positions its comic tail.
    """
    closed = pyqtSignal()
    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.ToolTip
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._full_text = ""
        self._current_index = 0
        self.tail_flipped = False  # False: tail points down to pet below; True: tail points up

        # Outer layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 12)  # 12px bottom for tail

        # Inner comic card
        self.card = QFrame(self)
        self.card.setObjectName("DynamicBubbleCard")
        self.card.setStyleSheet("""
            QFrame#DynamicBubbleCard {
                background-color: #ffffff;
                border: 2px solid #2d2645;
                border-radius: 14px;
            }
        """)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(11, 6, 11, 7)
        card_layout.setSpacing(3)

        # Top bar: Title + Close Button
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        self.title_label = QLabel("哈Gemi", self.card)
        t_font = QFont()
        t_font.setFamilies(["PingFang SC", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        t_font.setPointSize(9)
        t_font.setWeight(QFont.Weight.Bold)
        t_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.title_label.setFont(t_font)
        self.title_label.setStyleSheet("color: #ff4e88; background: transparent; border: none;")
        top_bar.addWidget(self.title_label)

        top_bar.addStretch()

        self.close_btn = QPushButton("×", self.card)
        self.close_btn.setFixedSize(16, 16)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: #f0f0f5;
                color: #888899;
                border: 1px solid #d4d4e0;
                border-radius: 8px;
                font-family: Arial, sans-serif;
                font-size: 12px;
                font-weight: bold;
                padding: 0px;
                margin: 0px;
            }
            QPushButton:hover {
                background-color: #ff4e88;
                color: #ffffff;
                border: 1px solid #ff4e88;
            }
            QPushButton:pressed {
                background-color: #e03672;
            }
        """)
        self.close_btn.clicked.connect(self.hide_bubble)
        top_bar.addWidget(self.close_btn)
        card_layout.addLayout(top_bar)

        # Content Label: Compact & crisp
        self.label = QLabel(self.card)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lbl_font = QFont()
        lbl_font.setFamilies(["PingFang SC", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        lbl_font.setPointSize(9)
        lbl_font.setWeight(QFont.Weight.DemiBold)
        lbl_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        self.label.setFont(lbl_font)
        self.label.setStyleSheet("""
            QLabel {
                color: #1f1b2e;
                background: transparent;
                border: none;
                line-height: 128%;
            }
        """)
        card_layout.addWidget(self.label)
        main_layout.addWidget(self.card)

        # Make inner labels transparent to mouse clicks so card/bubble receives them reliably
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.card.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.card.setToolTip("点击打开评论框，与哈Gemi继续吐槽互动 💬")

        # Forward mouse events on card directly to this widget
        self.card.mousePressEvent = self.mousePressEvent
        self.card.mouseMoveEvent = self.mouseMoveEvent
        self.card.mouseReleaseEvent = self.mouseReleaseEvent

        # Typewriter timer
        self.type_timer = QTimer(self)
        self.type_timer.timeout.connect(self._type_next_char)

        # Auto hide timer
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_bubble)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        cr = self.card.geometry()
        tail_start_x = cr.right() - 28
        tail_tip_x = cr.right() - 14
        tail_tip_y = cr.bottom() + 10

        tail_poly = QPolygon([
            QPoint(tail_start_x, cr.bottom() - 2),
            QPoint(tail_start_x + 14, cr.bottom() - 2),
            QPoint(tail_tip_x, tail_tip_y)
        ])

        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(tail_poly)

        painter.setPen(QPen(QColor("#2d2645"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(tail_start_x, cr.bottom() - 1, tail_tip_x, tail_tip_y)
        painter.drawLine(tail_tip_x, tail_tip_y, tail_start_x + 14, cr.bottom() - 1)

    def calculate_size(self, text: str) -> tuple[int, int]:
        """Compact sizing tailored to text length without giant white borders."""
        length = len(text)
        if length <= 16:
            target_width = 210
        elif length <= 35:
            target_width = 260
        elif length <= 55:
            target_width = 300
        elif length <= 85:
            target_width = 340
        else:
            target_width = 380

        avail_w = target_width - 34
        measure_w = max(160, avail_w - 6)
        fm = self.label.fontMetrics()
        rect = fm.boundingRect(0, 0, measure_w, 2000, Qt.TextFlag.TextWordWrap, text)
        lbl_h = max(20, rect.height() + 4)

        # Overhead: card padding(13) + top_bar(16) + spacing(3) + outer margins(18) + border(4) = 54
        target_height = lbl_h + 54
        return target_width, target_height

    def show_text(self, text: str, duration_ms: Optional[int] = None):
        self.type_timer.stop()
        self.hide_timer.stop()

        self._full_text = text
        self._current_index = 0

        target_width, target_height = self.calculate_size(text)
        avail_w = target_width - 34

        self.label.setFixedWidth(avail_w)
        self.setFixedSize(target_width, target_height)
        self.label.setText("")

        self.show()
        self.raise_()

        if duration_ms is None:
            duration_ms = max(7000, 6000 + len(text) * 70)

        self.type_timer.start(18)
        self.hide_timer.start(duration_ms)

    def _type_next_char(self):
        if self._current_index < len(self._full_text):
            self._current_index += 1
            self.label.setText(self._full_text[:self._current_index])
            self.update()
        else:
            self.type_timer.stop()
            self.label.setText(self._full_text)
            self.update()

    def hide_bubble(self):
        self.type_timer.stop()
        self.hide_timer.stop()
        self.label.setText("")
        self.hide()
        self.closed.emit()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._press_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and hasattr(self, '_press_pos'):
            dist = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if dist < 5:
                self.clicked.emit()
            event.accept()
