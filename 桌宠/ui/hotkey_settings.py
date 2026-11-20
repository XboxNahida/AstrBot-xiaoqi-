"""与原设置主题一致的组合键录入页。"""
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (
    QFormLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from hotkey_config import DEFAULT_KEYS, KEY_LABELS


class HotkeySettingsPanel(QWidget):
    requested = pyqtSignal(dict)

    def __init__(self, bindings):
        super().__init__()
        layout = QVBoxLayout(self)
        note = QLabel("点击输入框后直接按组合键；清空可关闭该项。保存后立即生效。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.controls = {}
        for key, label in KEY_LABELS.items():
            edit = QKeySequenceEdit(QKeySequence(bindings[key]))
            edit.setMaximumSequenceLength(1)
            self.controls[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        restore = QPushButton("恢复默认（保存后生效）")
        restore.clicked.connect(lambda: self.set_values(DEFAULT_KEYS))
        layout.addWidget(restore)
        save = QPushButton("保存按键绑定并立即生效")
        save.clicked.connect(self.save_settings)
        layout.addWidget(save)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)
        layout.addStretch()

    def set_values(self, values):
        for key, edit in self.controls.items():
            edit.setKeySequence(QKeySequence(values[key]))

    def save_settings(self):
        self.requested.emit({key: edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
                             for key, edit in self.controls.items()})
