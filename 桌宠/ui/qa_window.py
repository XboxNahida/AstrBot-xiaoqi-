"""常驻截图答疑窗口，保留白粉风格和可恢复会话列表。"""
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizeGrip,
    QVBoxLayout,
    QWidget,
)

from .qa_renderer import ReplyRenderer


class QAWindow(QWidget):
    send_requested = pyqtSignal(str)
    capture_requested = pyqtSignal(bool)
    selected_conversation = pyqtSignal(str)
    new_requested = pyqtSignal()
    delete_requested = pyqtSignal()
    choice_changed = pyqtSignal()
    personas_requested = pyqtSignal()
    retry_requested = pyqtSignal()
    context_requested = pyqtSignal()
    mode_changed = pyqtSignal()

    def __init__(self, cache_dir):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("截图答疑")
        self.resize(420, 360)
        self.setMinimumSize(360, 280)
        self.setStyleSheet("""
QWidget{background:transparent;color:#332d44;font-family:'Microsoft YaHei';}
QFrame#QACard{background:white;border:2.5px solid #ff4e88;border-radius:18px;}
QLabel#QATitle{color:#ff4e88;font-weight:bold;font-size:14px;}
QLabel#QAStatus{color:#8c82a0;font-size:11px;}
QPushButton{background:#f2edf7;color:#6d6682;border:1px solid #dcd5e8;border-radius:12px;padding:5px 8px;font-size:12px;}
QPushButton:hover{background:#fce7ef;border-color:#ff4e88;}
QPushButton:disabled{color:#b4acbc;border-color:#e7e0ee;}
QPushButton#QASend{background:#ff4e88;color:white;border:1px solid #ff4e88;border-radius:12px;font-weight:bold;}
QPushButton#QASend:hover{background:#ff689a;}
QPushButton#QASend:disabled{background:#dbcbd2;color:#776c73;}
QPushButton#QAClose{border-radius:10px;font-size:13px;font-weight:bold;padding:0;}
QPushButton#QAClose:hover{background:#ff4e88;color:white;}
QPlainTextEdit{background:#f7f4fa;border:1.5px solid #dcd5e8;border-radius:12px;padding:5px 8px;color:#2d2645;font-size:12px;}
QPlainTextEdit:focus{background:white;border-color:#ff4e88;}
QComboBox{background:#f7f4fa;border:1px solid #dcd5e8;border-radius:6px;padding:3px;}
QComboBox QAbstractItemView{background:white;selection-background-color:#fce7ef;selection-color:#332d44;}
""")
        self._drag_pos = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        self.card = QFrame(self)
        self.card.setObjectName('QACard')
        outer.addWidget(self.card)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(14, 10, 14, 8)
        layout.setSpacing(6)
        self.header = QWidget(self.card)
        self.header.setCursor(Qt.CursorShape.SizeAllCursor)
        self.header.installEventFilter(self)
        header = QHBoxLayout(self.header)
        header.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('截图答疑', self.header)
        self.title.setObjectName('QATitle')
        self.title.installEventFilter(self)
        header.addWidget(self.title)
        header.addStretch()
        close = QPushButton('×', self.header)
        close.setObjectName('QAClose')
        close.setFixedSize(20, 20)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip('隐藏答疑窗口 (Esc)，保留对话')
        close.clicked.connect(self.close)
        header.addWidget(close)
        layout.addWidget(self.header)
        top = QHBoxLayout()
        self.sessions = QComboBox()
        self.sessions.installEventFilter(self)
        self.sessions.setMinimumWidth(70)
        self.sessions.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sessions.activated.connect(lambda _: self.selected_conversation.emit(self.sessions.currentData()))
        top.addWidget(self.sessions, 1)
        for label, signal in (("新对话", self.new_requested), ("删除", self.delete_requested)):
            button = QPushButton(label)
            button.clicked.connect(signal.emit)
            top.addWidget(button)
        layout.addLayout(top)
        choices = QHBoxLayout()
        self.models, self.personas = QComboBox(), QComboBox()
        for control in (self.models, self.personas):
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.setMinimumWidth(60)
            control.activated.connect(lambda _: self.choice_changed.emit())
            control.installEventFilter(self)
            choices.addWidget(control, 1)
        refresh = QPushButton("云端人格")
        refresh.clicked.connect(self.personas_requested.emit)
        choices.addWidget(refresh)
        layout.addLayout(choices)
        modes = QHBoxLayout()
        modes.addWidget(QLabel('对话模式'))
        self.mode = QComboBox()
        self.mode.addItem('普通 · 省Token', 'normal')
        self.mode.addItem('保留上下文', 'retain')
        self.mode.setToolTip('普通：12K成本输入估算触发，批量收至8K目标，最近4轮优先保留。\n'
            '保留上下文：重新纳入完整历史，仅按模型窗口与上传大小保护。\n'
            '模式仅影响下次发送；不删除历史，不在回复生成中收缩。')
        self.mode.installEventFilter(self)
        self.mode.activated.connect(lambda _: self.mode_changed.emit())
        modes.addWidget(self.mode, 1)
        layout.addLayout(modes)
        self.renderer = ReplyRenderer(cache_dir)
        self.renderer.setMinimumHeight(60)
        layout.addWidget(self.renderer, 1)
        self.status = QLabel("框选截图后输入问题；Enter换行，Ctrl+Enter发送。")
        self.status.setObjectName('QAStatus')
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(56)
        status_row = QHBoxLayout()
        status_row.addWidget(self.status, 1)
        self.context_button = QPushButton('上下文配置')
        self.context_button.clicked.connect(self.context_requested.emit)
        status_row.addWidget(self.context_button)
        layout.addLayout(status_row)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("输入问题或追问…")
        self.input.setMaximumHeight(70)
        layout.addWidget(self.input)
        bottom = QHBoxLayout()
        for label, new in (("补充截图", False), ("重新提问", True)):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, n=new: self.capture_requested.emit(n))
            bottom.addWidget(button)
        self.send = QPushButton("发送")
        self.send.setObjectName('QASend')
        retry = QPushButton('重试')
        retry.clicked.connect(self.retry_requested.emit)
        bottom.addWidget(retry)
        self.send.clicked.connect(self._send)
        bottom.addWidget(self.send)
        layout.addLayout(bottom)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(14, 14)
        self.size_grip.setToolTip('拖动调整窗口大小')
        self.size_grip.move(self.width() - 18, self.height() - 18)
        QShortcut(QKeySequence("Ctrl+Return"), self, self._send)
        QShortcut(QKeySequence("Esc"), self, self.close)

    def _send(self):
        text = self.input.toPlainText().strip()
        if text and self.send.isEnabled():
            self.send_requested.emit(text)

    def set_busy(self, value):
        self.mode.setEnabled(not value)
        self.send.setEnabled(not value)
        self.send.setText("生成中…" if value else "发送")

    def eventFilter(self, obj, event):
        if obj in (getattr(self, 'header', None), getattr(self, 'title', None)):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if (event.type() == QEvent.Type.MouseMove and self._drag_pos is not None
                    and event.buttons() & Qt.MouseButton.LeftButton):
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if event.type() in (QEvent.Type.MouseButtonRelease, QEvent.Type.UngrabMouse):
                self._drag_pos = None
        if isinstance(obj, QComboBox) and event.type() == QEvent.Type.Wheel and not obj.view().isVisible():
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'size_grip'):
            self.size_grip.move(self.width() - 18, self.height() - 18)
