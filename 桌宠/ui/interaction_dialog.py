from PyQt6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, 
    QFrame, QLineEdit, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QPoint
from PyQt6.QtGui import QFont, QKeySequence, QShortcut

class InteractionDialog(QWidget):
    user_replied = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Outer Card
        self.card = QFrame(self)
        self.card.setObjectName("InteractionCard")
        self.card.setStyleSheet("""
            QFrame#InteractionCard {
                background-color: #ffffff;
                border: 2.5px solid #ff4e88;
                border-radius: 18px;
            }
        """)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 10, 14, 12)
        card_layout.setSpacing(8)

        # Top Bar: Title + Close Button
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)

        title_lbl = QLabel("💬 与哈Gemi吐槽互动", self.card)
        t_font = QFont()
        t_font.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        t_font.setPointSize(10)
        t_font.setWeight(QFont.Weight.Bold)
        title_lbl.setFont(t_font)
        title_lbl.setStyleSheet("color: #ff4e88; background: transparent; border: none;")
        top_bar.addWidget(title_lbl)

        top_bar.addStretch()

        close_btn = QPushButton("×", self.card)
        close_btn.setFixedSize(20, 20)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setToolTip("关闭互动框 (Esc)")
        close_btn.setStyleSheet("""
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
        close_btn.clicked.connect(self.close)
        top_bar.addWidget(close_btn)

        card_layout.addLayout(top_bar)

        # Quote box of the previous critique
        self.quote_frame = QFrame(self.card)
        self.quote_frame.setStyleSheet("""
            QFrame {
                background-color: #f9f6fc;
                border-left: 3px solid #ff4e88;
                border-radius: 6px;
                padding: 6px 10px;
            }
        """)
        quote_layout = QVBoxLayout(self.quote_frame)
        quote_layout.setContentsMargins(6, 4, 6, 4)
        quote_layout.setSpacing(2)

        quote_title = QLabel("哈Gemi刚才说:", self.quote_frame)
        q_font = QFont()
        q_font.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        q_font.setPointSize(8)
        q_font.setWeight(QFont.Weight.Bold)
        quote_title.setFont(q_font)
        quote_title.setStyleSheet("color: #8c82a0; background: transparent; border: none;")
        quote_layout.addWidget(quote_title)

        self.quote_label = QLabel(self.quote_frame)
        self.quote_label.setWordWrap(True)
        body_font = QFont()
        body_font.setFamilies(["PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Segoe UI", "sans-serif"])
        body_font.setPointSize(9)
        self.quote_label.setFont(body_font)
        self.quote_label.setStyleSheet("color: #332d44; background: transparent; border: none;")
        quote_layout.addWidget(self.quote_label)

        card_layout.addWidget(self.quote_frame)

        # Input Row
        input_bar = QHBoxLayout()
        input_bar.setContentsMargins(0, 2, 0, 0)
        input_bar.setSpacing(6)

        self.input_reply = QLineEdit(self.card)
        self.input_reply.setPlaceholderText("跟哈Gemi吐槽互动...(Enter发送)")
        self.input_reply.setFont(body_font)
        self.input_reply.setStyleSheet("""
            QLineEdit {
                background-color: #f7f4fa;
                border: 1.5px solid #dcd5e8;
                border-radius: 12px;
                padding: 5px 10px;
                color: #2d2645;
            }
            QLineEdit:focus {
                background-color: #ffffff;
                border: 1.5px solid #ff4e88;
            }
        """)
        self.input_reply.returnPressed.connect(self._send_reply)
        input_bar.addWidget(self.input_reply)

        self.btn_send = QPushButton("发送", self.card)
        self.btn_send.setFixedSize(48, 28)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_send.setStyleSheet("""
            QPushButton {
                background-color: #ff4e88;
                color: #ffffff;
                border: none;
                border-radius: 14px;
                font-size: 11px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ff689a;
            }
            QPushButton:pressed {
                background-color: #e03b73;
            }
        """)
        self.btn_send.clicked.connect(self._send_reply)
        input_bar.addWidget(self.btn_send)

        card_layout.addLayout(input_bar)
        main_layout.addWidget(self.card)

        # Shortcut: Esc to close
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    def open_for_critique(self, critique_text: str, pet_pos: QPoint):
        if not critique_text or not critique_text.strip():
            critique_text = "（还没有锐评记录喵~ 点击我或按 Ctrl+F9 呼叫锐评吧！）"

        self.quote_label.setText(critique_text)
        self.input_reply.clear()
        self.input_reply.setEnabled(True)
        self.btn_send.setEnabled(True)

        self.setFixedWidth(420)
        self.adjustSize()

        screen = self.screen() or QApplication.primaryScreen()
        target_x = pet_pos.x() - self.width() + 45
        target_y = pet_pos.y() - self.height() - 10

        if screen:
            geom = screen.availableGeometry()
            if target_x < geom.x() + 10:
                target_x = pet_pos.x() + 160
            if target_y < geom.y() + 10:
                target_y = pet_pos.y() + 250

        self.move(target_x, target_y)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input_reply.setFocus()

    def _send_reply(self):
        text = self.input_reply.text().strip()
        if not text:
            return
        self.user_replied.emit(text)
        self.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and hasattr(self, '_drag_pos'):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
