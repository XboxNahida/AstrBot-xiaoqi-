"""按模型保存的上下文保护配置，不修改原模型或人格配置。"""
import threading

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from qa_budget import MODES, output_policy


class ContextSettings(QDialog):
    loaded = pyqtSignal(str)

    def __init__(self, store, profile, parent=None, *, mode='normal'):
        super().__init__(parent)
        self.store, self.profile = store, dict(profile)
        self.mode = mode if mode in MODES else 'normal'
        self.closed = False
        self.setWindowTitle('截图答疑 · 上下文保护')
        self.resize(470, 420)
        self.setStyleSheet(parent.styleSheet() if parent else "QWidget{background:#fff8fc;color:#332d44;}")
        layout = QVBoxLayout(self)
        self.info = QLabel()
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        self.origin = QLabel()
        self.origin.setWordWrap(True)
        self.origin.setOpenExternalLinks(True)
        layout.addWidget(self.origin)
        form = QFormLayout()
        self.context = self.spin(0, 4000000, ' Token')
        self.context.setSpecialValueText('自动识别')
        self.output = self.spin(64, 1000000, ' Token')
        self.safety = self.spin(5, 40, ' %')
        self.edge = self.spin(512, 4096, ' 像素')
        self.quality = self.spin(60, 95, '')
        for label, control in (('上下文窗口', self.context), ('输出预留', self.output), ('安全余量', self.safety),
                               ('上传图片最长边', self.edge), ('JPEG质量', self.quality)):
            form.addRow(label, control)
        layout.addLayout(form)
        explanation = QLabel('只从本次请求移出最旧完整轮次，不总结、不删本地历史。\n'
            '原图保留，上传副本单图≤1MiB，整包≤10MiB（含Base64）。\n'
            'Token为保守估算；未知模型先用8K，请按服务商限制核对。')
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.read = QPushButton('重新读取模型信息')
        self.read.clicked.connect(self.discover)
        self.read.setEnabled(profile.get('provider') != 'cloud')
        if profile.get('provider') == 'cloud':
            self.read.setToolTip('重新打开此页会查询云端实际模型及能力')
        buttons.addWidget(self.read)
        for label, callback in (('恢复自动', self.reset), ('保存', self.save)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.loaded.connect(self.ready)
        self.populate()
        self.status.setText(self.store.entry(self.profile).get('diagnostic', ''))

    def spin(self, low, high, suffix):
        control = QSpinBox()
        control.setRange(low, high)
        control.setSuffix(suffix)
        control.setKeyboardTracking(False)
        control.installEventFilter(self)
        return control

    def eventFilter(self, obj, event):
        if isinstance(obj, QSpinBox) and event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def populate(self):
        entry = self.store.entry(self.profile)
        policy = output_policy(self.profile, self.store.resolve(self.profile), entry, self.mode)
        overrides = entry.get('overrides', {})
        self.context.setValue(overrides.get('context_tokens', 0))
        for control, key in ((self.output, 'output_tokens'), (self.safety, 'safety_percent'),
                             (self.edge, 'image_edge'), (self.quality, 'jpeg_quality')):
            control.setValue(policy[key])
        self.describe(policy)

    def describe(self, policy):
        self.info.setText(f"模式：{MODES[self.mode]}\n模型：{self.profile.get('model', '')}\n窗口：{policy['context_tokens']} Token；来源：{policy['source']}")
        url = policy.get('source_url', '')
        self.origin.setText(f'<a href="{url}">官方模型卡</a>；若中转设置更小限制，可手动填写。' if url else '')
        self.origin.setVisible(bool(url))

    def save(self):
        try:
            self.store.override(self.profile, {'context_tokens': self.context.value(), 'output_tokens': self.output.value(),
                'safety_percent': self.safety.value(), 'image_edge': self.edge.value(), 'jpeg_quality': self.quality.value()})
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))
            return
        self.status.setText('已保存，仅对该模型及接口生效；下次发送使用新预算。')
        self.populate()

    def reset(self):
        try:
            self.store.override(self.profile, {})
            self.populate()
            self.status.setText('已恢复自动窗口和默认图片/输出配置。')
        except (ValueError, OSError) as exc:
            self.status.setText(str(exc))

    def discover(self):
        self.read.setEnabled(False)
        self.status.setText('正在读取能力信息，不会调用模型生成…')
        def work():
            try:
                policy = self.store.discover(self.profile, force=True)
                message = '已更新：' + policy['source'] + '；' + self.store.entry(self.profile).get('diagnostic', '')
            except Exception:  # noqa: BLE001 -- 后台能力读取不影响聊天。
                message = '读取失败，保留原配置。'
            self.loaded.emit(message)
        threading.Thread(target=work, daemon=True, name='答疑能力读取').start()

    def ready(self, message):
        if not self.closed:
            self.read.setEnabled(True)
            # 不覆盖用户读取期间正在编辑的值。
            policy = self.store.resolve(self.profile)
            self.describe(policy)
            self.status.setText(message)

    def closeEvent(self, event):
        self.closed = True
        super().closeEvent(event)
