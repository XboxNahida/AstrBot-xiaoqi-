from typing import Optional
from PyQt6.QtWidgets import QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFrame
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QPainter, QColor, QPen, QBrush, QPolygon

class SpeechBubble(QWidget):
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

        # Outer Layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 16)  # Extra bottom space for comic tail

        # Inner Card (Puffy White Comic Bubble)
        self.card = QFrame(self)
        self.card.setObjectName("BubbleCard")
        self.card.setStyleSheet("""
            QFrame#BubbleCard {
                background-color: #ffffff;
                border: 2.5px solid #2d2645;
                border-radius: 18px;
            }
        """)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 10, 14, 12)
        card_layout.setSpacing(6)

        # Top bar: Character Name + Close Button
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(2, 0, 0, 0)

        self.title_label = QLabel("哈Gemi", self.card)
        t_font = QFont()
        t_font.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        t_font.setPointSize(9)
        t_font.setWeight(QFont.Weight.Bold)
        t_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias)
        self.title_label.setFont(t_font)
        self.title_label.setStyleSheet("color: #ff4e88; background: transparent; border: none;")
        top_bar.addWidget(self.title_label)

        top_bar.addStretch()

        self.close_btn = QPushButton("×", self.card)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("关闭气泡")
        self.close_btn.setStyleSheet("""
            QPushButton {
                background-color: #f2edf7;
                color: #6d6682;
                border: 1px solid #dcd5e8;
                border-radius: 10px;
                font-size: 13px;
                font-weight: bold;
                padding-bottom: 2px;
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

        # Content Label: Crisp bold text
        self.label = QLabel(self.card)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        lbl_font = QFont()
        lbl_font.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        lbl_font.setPointSize(10)
        lbl_font.setWeight(QFont.Weight.DemiBold)
        lbl_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias)
        self.label.setFont(lbl_font)
        self.label.setStyleSheet("""
            QLabel {
                color: #1f1b2e;
                background: transparent;
                border: none;
                padding-top: 1px;
                padding-bottom: 1px;
            }
        """)
        card_layout.addWidget(self.label)

        main_layout.addWidget(self.card)

        # Typewriter timer ("慢慢")
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
        tail_start_x = cr.right() - 40
        tail_tip_x = cr.right() - 20
        tail_tip_y = cr.bottom() + 12

        tail_poly = QPolygon([
            QPoint(tail_start_x, cr.bottom() - 2),
            QPoint(tail_start_x + 18, cr.bottom() - 2),
            QPoint(tail_tip_x, tail_tip_y)
        ])

        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(tail_poly)

        painter.setPen(QPen(QColor("#2d2645"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawLine(tail_start_x, cr.bottom() - 1, tail_tip_x, tail_tip_y)
        painter.drawLine(tail_tip_x, tail_tip_y, tail_start_x + 18, cr.bottom() - 1)

    def calculate_size(self, text: str) -> tuple[int, int]:
        """Accurately calculates bubble dimensions with safe DPI margins without size accumulation."""
        length = len(text)
        if length <= 18:
            target_width = 300
        elif length <= 42:
            target_width = 380
        elif length <= 70:
            target_width = 440
        else:
            target_width = 500

        avail_w = target_width - 56
        # Use measure_w with safety inset so line breaks near boundary are conservatively counted
        measure_w = max(180, avail_w - 12)
        fm = self.label.fontMetrics()
        rect = fm.boundingRect(0, 0, measure_w, 2000, Qt.TextFlag.TextWordWrap, text)
        lbl_h = max(26, rect.height() + 12)

        # Overhead: card padding(24) + top_bar(20) + spacing(6) + outer margins(28) + border(6) = 84
        target_height = lbl_h + 86
        return target_width, target_height

    def show_text(self, text: str, duration_ms: Optional[int] = None):
        self.type_timer.stop()
        self.hide_timer.stop()

        self._full_text = text
        self._current_index = 0

        target_width, target_height = self.calculate_size(text)
        avail_w = target_width - 56

        # Lock exact dimensions before typewriter typing begins
        self.label.setFixedWidth(avail_w)
        self.setFixedSize(target_width, target_height)

        # Clear text immediately so old text is NEVER shown on screen!
        self.label.setText("")

        self.show()
        self.raise_()

        # Dynamic reading duration
        if duration_ms is None:
            duration_ms = max(7000, 6000 + len(text) * 70)

        # Start smooth typewriter effect ("慢慢")
        self.type_timer.start(20)
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
                # Clicked directly on the bubble -> emit clicked to open interaction dialog
                self.clicked.emit()
            event.accept()
