"""独立桌面操作设置与任务窗口；只发信号，不执行系统输入。"""
from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QTextCursor, QTextOption
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from desktop_control.config import DEFAULTS

STYLE = """QWidget{background:#fff8fc;color:#332d44;font-family:'Microsoft YaHei';}
QPushButton{background:#ff4e88;color:white;border:0;border-radius:8px;padding:6px 8px}
QPushButton:hover{background:#f875a0}QPushButton:disabled{background:#dbcbd2;color:#776c73}
QPlainTextEdit,QTextBrowser,QComboBox,QSpinBox{background:white;border:1px solid #dcd5e8;border-radius:6px;padding:4px}
QScrollArea{border:0}QCheckBox{spacing:6px;padding:4px 0}
"""


class ControlPanel(QWidget):
    settings_saved = pyqtSignal(dict)

    def __init__(self, store, profiles_provider):
        super().__init__()
        self.store = store
        self.profiles_provider = profiles_provider
        load_failed = False
        try:
            self.config = {**DEFAULTS, **store.load()}
        except (ValueError, OSError):
            # 独立新功能损坏不得阻止原设置/桌宠启动，也不自动覆盖坏文件。
            self.config = dict(DEFAULTS)
            load_failed = True
        self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        form = QVBoxLayout(content)
        form.setContentsMargins(8, 8, 8, 8)
        title = QLabel('桌面操作')
        title.setStyleSheet('font-size:18px;font-weight:bold')
        form.addWidget(title)
        for key, label in (
            ('enabled', '允许本地桌面操作'),
            ('cloud_enabled', '允许云端调用底层工具'),
            ('allow_original', '允许云端按需获取原图'),
            ('move_pet', '执行时移动桌宠指示位置'),
            ('pause_on_input', '手动输入时暂停（关闭可并行操作）'),
            ('prefer_controls', '优先识别窗口控件'),
        ):
            control = QCheckBox(label)
            control.setChecked(bool(self.config[key]))
            setattr(self, key, control)
            form.addWidget(control)
        notice = QLabel('云端开关仅保存本地授权、预留接口。当前云端尚未接线；开启不会让小七自动获得桌面操作能力。')
        notice.setWordWrap(True)
        form.addWidget(notice)
        form.addWidget(QLabel('本地操作模型'))
        self.profile = QComboBox()
        self.profile.setMinimumWidth(0)
        self.profile.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.profile.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.profile.installEventFilter(self)
        form.addWidget(self.profile)
        self.refresh_profiles()
        for key, label, low, high in (
            ('max_steps', '单次任务最多步骤', 1, 100),
            ('max_seconds', '单次任务最长秒数', 1, 3600),
        ):
            form.addWidget(QLabel(label))
            control = QSpinBox()
            control.setRange(low, high)
            control.setValue(int(self.config[key]))
            control.installEventFilter(self)
            setattr(self, key, control)
            form.addWidget(control)
        refresh = QPushButton('刷新本地模型列表')
        refresh.clicked.connect(self.refresh_profiles)
        form.addWidget(refresh)
        self.save_button = QPushButton('保存桌面操作设置')
        self.save_button.clicked.connect(self.save_settings)
        form.addWidget(self.save_button)
        self.status = QLabel('桌面操作配置读取失败，已禁用此功能；原文件保留，请检查后重新保存。' if load_failed
                             else '修改后请保存；桌面操作默认关闭。')
        self.status.setWordWrap(True)
        form.addWidget(self.status)
        form.addStretch()
        self.scroll.setWidget(content)
        layout.addWidget(self.scroll)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Wheel and isinstance(watched, (QComboBox, QSpinBox)):
            # 显式滚动页面；吞掉控件事件本身不会自动传递给滚动区。
            bar = self.scroll.verticalScrollBar()
            delta = event.pixelDelta().y()
            if not delta:
                delta = round(event.angleDelta().y() / 120 * bar.singleStep() * 3)
            bar.setValue(bar.value() - delta)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def refresh_profiles(self):
        selected = self.profile.currentData() or self.config['profile_id']
        self.profile.clear()
        self.profile.addItem('沿用原模型', 'original')
        seen = {'original'}
        for profile in self.profiles_provider():
            identity = profile.get('id')
            if not identity or identity in seen:
                continue
            seen.add(identity)
            self.profile.addItem(f"{profile.get('name') or identity} · {profile.get('model') or '未指定模型'}", identity)
        index = self.profile.findData(selected)
        if index < 0:
            self.profile.addItem('原模型配置已移除，请重新选择', selected)
            index = self.profile.count() - 1
        self.profile.setCurrentIndex(index)

    def save_settings(self):
        value = dict(self.config)
        for key in ('enabled', 'cloud_enabled', 'allow_original', 'move_pet', 'pause_on_input', 'prefer_controls'):
            value[key] = getattr(self, key).isChecked()
        value.update(profile_id=self.profile.currentData(), max_steps=self.max_steps.value(),
            max_seconds=self.max_seconds.value())
        try:
            self.store.save(value)
        except (OSError, ValueError) as error:
            self.status.setText(f'保存失败：{error}')
            return
        self.config = value
        self.status.setText('桌面操作设置已保存。')
        self.settings_saved.emit(dict(value))


class ControlWindow(QWidget):
    submitted = pyqtSignal(str)
    voice_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    new_requested = pyqtSignal()
    model_selected = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle('桌面操作')
        self.resize(460, 405)
        self.setMinimumSize(360, 380)
        self.setStyleSheet("""
            QWidget {color:#332d44;font-family:'Microsoft YaHei';background:transparent;}
            QFrame#ControlCard {background:white;border:2.5px solid #ff4e88;border-radius:18px;}
            QLabel#ControlTitle {color:#ff4e88;font-weight:bold;font-size:14px;}
            QLabel#ControlStatus {color:#8c82a0;font-size:11px;}
            QFrame#ControlQuote {background:#f9f6fc;border-left:3px solid #ff4e88;border-radius:6px;}
            QLabel#QuoteTitle {color:#8c82a0;font-size:11px;font-weight:bold;}
            QTextBrowser {border:0;background:transparent;font-size:13px;padding:0;}
            QTextBrowser#ControlLog {background:#f9f6fc;border:1px solid #e7e0ee;border-radius:6px;padding:4px;}
            QPlainTextEdit {background:#f7f4fa;border:1.5px solid #dcd5e8;border-radius:12px;
                padding:5px 8px;color:#2d2645;font-size:12px;}
            QPlainTextEdit:focus {background:white;border-color:#ff4e88;}
            QPushButton {background:#f2edf7;color:#6d6682;border:1px solid #dcd5e8;
                border-radius:12px;padding:5px 8px;font-size:12px;}
            QPushButton:hover {background:#fce7ef;border-color:#ff4e88;}
            QPushButton:disabled {color:#b4acbc;border-color:#e7e0ee;}
            QPushButton#ControlSend {background:#ff4e88;color:white;border:0;border-radius:14px;font-weight:bold;}
            QPushButton#ControlSend:hover {background:#ff689a;}
            QPushButton#ControlSend:disabled {background:#dbcbd2;color:#776c73;}
            QPushButton#ControlClose {border-radius:10px;font-size:13px;font-weight:bold;padding:0;}
            QPushButton#ControlClose:hover {background:#ff4e88;color:white;}
            QPushButton#RecordsToggle {background:transparent;border:0;text-align:left;padding:2px 0;font-size:11px;}
        """)
        self._busy = False
        self._drag_pos = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.card = QFrame(self)
        self.card.setObjectName('ControlCard')
        layout.addWidget(self.card)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 10, 14, 8)
        card_layout.setSpacing(6)
        self.header = QWidget(self.card)
        self.header.setCursor(Qt.CursorShape.SizeAllCursor)
        self.header.installEventFilter(self)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('桌面操作', self.header)
        self.title.setObjectName('ControlTitle')
        self.title.installEventFilter(self)
        header_layout.addWidget(self.title)
        header_layout.addStretch()
        self.close_button = QPushButton('×', self.header)
        self.close_button.setObjectName('ControlClose')
        self.close_button.setFixedSize(20, 20)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip('隐藏操作窗口 (Esc)，任务继续执行')
        self.close_button.clicked.connect(self.close)
        header_layout.addWidget(self.close_button)
        card_layout.addWidget(self.header)
        model_row = QHBoxLayout()
        model_row.addWidget(QLabel('操作模型'))
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(0)
        self.model_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.model_combo.setStyleSheet('background:#f7f4fa;border:1px solid #dcd5e8;border-radius:6px;padding:4px;')
        self.model_combo.installEventFilter(self)
        self.model_combo.activated.connect(lambda index: self.model_selected.emit(self.model_combo.itemData(index)))
        model_row.addWidget(self.model_combo, 1)
        card_layout.addLayout(model_row)
        self.model_hint = QLabel('模型选择会永久保存，并与设置中心同步。')
        self.model_hint.setWordWrap(True)
        self.model_hint.setStyleSheet('color:#8c82a0;font-size:11px;')
        card_layout.addWidget(self.model_hint)
        self.quote_frame = QFrame(self.card)
        self.quote_frame.setObjectName('ControlQuote')
        quote_layout = QVBoxLayout(self.quote_frame)
        quote_layout.setContentsMargins(12, 8, 10, 8)
        quote_layout.setSpacing(4)
        quote_title = QLabel('哈Gemi正在说：', self.quote_frame)
        quote_title.setObjectName('QuoteTitle')
        quote_layout.addWidget(quote_title)
        self.message = QTextBrowser(self.quote_frame)
        self.message.setMinimumHeight(30)
        self.message.setPlainText('告诉我你的操作目标，我会在这里说明进展。')
        quote_layout.addWidget(self.message, 1)
        card_layout.addWidget(self.quote_frame, 1)
        self.status = QLabel('就绪：输入目标后发送，或使用麦克风。')
        self.status.setObjectName('ControlStatus')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        card_layout.addWidget(self.status)
        self.records_toggle = QPushButton('操作记录（展开）')
        self.records_toggle.setObjectName('RecordsToggle')
        self.records_toggle.setCheckable(True)
        self.records_toggle.toggled.connect(self._toggle_records)
        card_layout.addWidget(self.records_toggle)
        self.log = QTextBrowser()
        self.log.setObjectName('ControlLog')
        self.log.setMinimumHeight(35)
        self.log.setMaximumHeight(105)
        self.log.hide()
        card_layout.addWidget(self.log)
        for browser in (self.message, self.log):
            browser.setOpenLinks(False)
            browser.setOpenExternalLinks(False)
            browser.setMinimumWidth(0)
            browser.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
            browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            browser.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText('输入操作目标或补充说明…\nEnter 发送，Shift+Enter 换行')
        self.input.setFixedHeight(54)
        self.input.installEventFilter(self)
        send_row = QHBoxLayout()
        self.send_button = QPushButton('发送')
        self.send_button.setObjectName('ControlSend')
        self.send_button.setFixedSize(48, 28)
        self.voice_button = QPushButton('麦克风')
        self.send_button.clicked.connect(self._submit)
        self.voice_button.clicked.connect(self.voice_requested.emit)
        send_row.addWidget(self.input, 1)
        send_row.addWidget(self.send_button)
        card_layout.addLayout(send_row)
        actions = QHBoxLayout()
        actions.setSpacing(5)
        actions.addWidget(self.voice_button)
        for name, label, signal in (
            ('pause', '暂停', self.pause_requested), ('resume', '继续', self.resume_requested),
            ('stop', '停止', self.stop_requested), ('new', '新任务', self.new_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(signal.emit)
            setattr(self, name + '_button', button)
            actions.addWidget(button)
        card_layout.addLayout(actions)
        self.size_grip = QSizeGrip(self)
        self.size_grip.setFixedSize(14, 14)
        self.size_grip.setToolTip('拖动调整窗口大小')
        self.size_grip.move(self.width() - 18, self.height() - 18)
        self.shortcut = QShortcut(QKeySequence('Ctrl+Return'), self)
        self.shortcut.activated.connect(self._submit)
        self.escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self.escape_shortcut.activated.connect(self.close)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'size_grip'):
            self.size_grip.move(self.width() - 18, self.height() - 18)

    def eventFilter(self, watched, event):
        if watched is getattr(self, 'model_combo', None) and event.type() == QEvent.Type.Wheel:
            event.accept()
            return True
        if (watched is getattr(self, 'input', None) and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self.close()
            return True
        if (watched is getattr(self, 'input', None) and event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier)):
            self._submit()
            return True
        if watched in (getattr(self, 'header', None), getattr(self, 'title', None)):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if (event.type() == QEvent.Type.MouseMove and self._drag_pos is not None
                    and event.buttons() & Qt.MouseButton.LeftButton):
                self.move(event.globalPosition().toPoint() - self._drag_pos)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._drag_pos = None
        return super().eventFilter(watched, event)

    def set_models(self, profiles, selected):
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItem('原版核心模型（核心与感知）', 'original')
        for profile in profiles:
            self.model_combo.addItem(f"{profile.get('name', profile['id'])} · {profile.get('model') or '未指定模型'}", profile['id'])
        index = self.model_combo.findData(selected)
        if index < 0:
            self.model_combo.addItem('所选模型已移除，请重新选择', selected)
            index = self.model_combo.count() - 1
        self.model_combo.setCurrentIndex(index)
        self.model_combo.blockSignals(False)

    def _toggle_records(self, checked):
        self.log.setVisible(checked)
        self.records_toggle.setText('操作记录（收起）' if checked else '操作记录（展开）')

    def show_model_message(self, text):
        """更新调用方累计的关键说明；不把模型文字解释为HTML或动作。"""
        self.message.setPlainText(str(text))
        cursor = self.message.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.message.setTextCursor(cursor)
        self.message.ensureCursorVisible()

    def _submit(self):
        text = self.input.toPlainText().strip()
        if self._busy or not text:
            return
        self.set_state('任务已提交，正在处理…', busy=True)
        self.input.clear()
        self.submitted.emit(text)

    def append(self, role, text):
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log.document().isEmpty():
            cursor.insertText('\n\n')
        cursor.insertText(f'{role}：\n{text}')
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    def set_state(self, text, busy=False):
        self._busy = bool(busy)
        self.status.setText(str(text))
        for button in (self.send_button, self.voice_button, self.new_button):
            button.setEnabled(not self._busy)

    def closeEvent(self, event):
        event.ignore()
        self.hide()
