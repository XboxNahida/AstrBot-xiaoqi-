"""扩展规则的中文表单；未知字段无损保留。"""
import copy
import uuid

from PyQt6.QtCore import QEvent, QTime
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)


class WheelGuard:
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            event.ignore()
            return True
        return super().eventFilter(obj, event)


class CoordinateSpinBox(QDoubleSpinBox):
    """未知坐标显示为空，不将国内城市缺少的经纬度替换成零。"""
    def __init__(self):
        super().__init__()
        self.unknown = False
        self.lineEdit().textEdited.connect(self._edited)

    def _edited(self, text):
        self.unknown = not text.strip()

    def textFromValue(self, value):
        return "" if getattr(self, "unknown", False) else super().textFromValue(value)

    def stepBy(self, steps):
        self.unknown = False
        super().stepBy(steps)

    def setValue(self, value):
        self.unknown = value is None
        if self.unknown:
            self.lineEdit().setPlaceholderText("未知（可留空）")
            self.lineEdit().clear()
        else:
            super().setValue(value)
            self.lineEdit().setText(self.textFromValue(super().value()))

    def nullable_value(self):
        return None if self.unknown else super().value()


class RuleDialog(WheelGuard, QDialog):
    def __init__(self, section, rule=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑规则")
        self.resize(400, 470)
        self.original = copy.deepcopy(rule or {})
        if section == "weather" and "id" not in self.original:
            self.original["id"] = uuid.uuid4().hex
        self.fields = {}
        root = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        page = QWidget()
        self.form = QFormLayout(page)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        scroll.setWidget(page)
        root.addWidget(scroll)
        self.add("enabled", "启用这条规则", True)
        if section in ("apps", "reminders", "hotkeys"):
            self.add("id", "规则名称 / 标识", uuid.uuid4().hex[:8])
        if section == "apps":
            for key, label, default in [("process", "进程名（例如 Code.exe）", ""),
                    ("minutes", "连续前台分钟", 60), ("repeat", "重复提醒", True),
                    ("text", "到时提醒", ""), ("start_text", "应用启动问候", ""),
                    ("exit_text", "应用退出问候", "")]:
                self.add(key, label, default)
        elif section == "reminders":
            self.add("when", "提醒方式", "daily", [("每天", "daily"), ("指定星期", "weekly"), ("间隔", "interval")])
            self.add("time", "提醒时刻", "10:00", time=True)
            days = QWidget()
            row = QHBoxLayout(days)
            row.setContentsMargins(0, 0, 0, 0)
            self.days = []
            for i, label in enumerate("一二三四五六日"):
                box = QCheckBox(label)
                box.setChecked(i in self.original.get("days", [0, 1, 2, 3, 4]))
                row.addWidget(box)
                self.days.append(box)
            self.form.addRow("星期", days)
            self.add("every", "间隔分钟", 60)
            self.add("text", "提醒内容", "")
        elif section == "hotkeys":
            self.add("keys", "组合键（如 <ctrl>+<alt>+h）", "<ctrl>+<alt>+h")
            self.add("action", "执行操作", "phrases:preview", [
                ("播放本地语录", "phrases:preview"), ("投喂小鱼干", "feed:fish"),
                ("投喂蛋糕", "feed:cake"), ("查看当前天气", "weather:current"),
                ("播放 / 暂停音乐", "media:toggle"), ("窗口救援", "tools:rescue")], editable=True)
        elif section == "phrases":
            self.add("category", "使用场景", "idle", [("闲置", "idle"), ("回来时", "return"), ("提醒", "reminder")])
            self.add("text", "语录内容", "")
        elif section == "balance":
            self.add("provider", "服务商", "deepseek", [("DeepSeek", "deepseek"), ("OpenRouter", "openrouter")], editable=True)
            self.add("model", "模型标识", "")
            self.add("currency", "币种", "CNY", [("人民币 CNY", "CNY"), ("美元 USD", "USD")])
            for key, label in [("input", "输入 / 百万Token"), ("cached_input", "缓存输入 / 百万Token"), ("output", "输出 / 百万Token")]:
                self.add(key, label, 0.0)
            self.add("timezone", "时区", "Asia/Shanghai")
            self.add("start", "峰谷时段开始", "00:00", time=True)
            self.add("end", "峰谷时段结束（相同为全天）", "00:00", time=True)
            self.add("multiplier", "时段价格倍率", 1.0)
        elif section == "weather":
            self.add("name", "城市名称（通常用搜索添加）", "")
            self.add("latitude", "纬度", 0.0)
            self.add("longitude", "经度", 0.0)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def add(self, key, label, default, choices=None, time=False, editable=False):
        value = self.original.get(key, default)
        if choices:
            widget = QComboBox()
            for title, data in choices:
                widget.addItem(title, data)
            if widget.findData(value) < 0:
                widget.addItem(str(value), value)
            widget.setCurrentIndex(widget.findData(value))
            widget.setEditable(editable)
        elif time:
            widget = QTimeEdit()
            widget.setDisplayFormat("HH:mm")
            widget.setTime(QTime.fromString(str(value), "HH:mm"))
        elif type(default) is bool:
            widget = QCheckBox()
            widget.setChecked(bool(value))
        elif type(default) in (int, float):
            widget = CoordinateSpinBox() if key in ("latitude", "longitude") else (
                QDoubleSpinBox() if type(default) is float else QSpinBox())
            bounds = {"latitude": (-90, 90), "longitude": (-180, 180)}
            widget.setRange(*bounds.get(key, (0, 1000000)))
            if isinstance(widget, QDoubleSpinBox):
                widget.setDecimals(6)
            widget.setValue(value)
        else:
            widget = QLineEdit(str(value))
        if isinstance(widget, (QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit)):
            widget.installEventFilter(self)
            for child in widget.findChildren(QLineEdit):
                child.installEventFilter(self)
        widget.setMinimumWidth(0)
        self.fields[key] = widget
        self.form.addRow(label, widget)

    def value(self):
        result = copy.deepcopy(self.original)
        for key, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                result[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                result[key] = widget.currentData() if widget.currentText() == widget.itemText(widget.currentIndex()) else widget.currentText().strip()
            elif isinstance(widget, QTimeEdit):
                result[key] = widget.time().toString("HH:mm")
            elif isinstance(widget, CoordinateSpinBox):
                result[key] = widget.nullable_value()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                result[key] = widget.value()
            else:
                result[key] = widget.text().strip()
        if hasattr(self, "days"):
            result["days"] = [i for i, box in enumerate(self.days) if box.isChecked()]
        return result


class RuleListEditor(QWidget):
    def __init__(self, section, values, parent=None):
        super().__init__(parent)
        self.section = section
        self._values = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setMaximumHeight(130)
        root.addWidget(self.list)
        if section == "resources":
            root.addWidget(QLabel("资源清单只读，请使用下方导入、删除按钮管理。"))
        else:
            row = QHBoxLayout()
            for label, callback in [("新增", self.add_rule), ("编辑", self.edit_rule), ("删除", self.remove_rule)]:
                button = QPushButton(label)
                button.clicked.connect(callback)
                row.addWidget(button)
            root.addLayout(row)
            self.list.itemDoubleClicked.connect(lambda _: self.edit_rule())
        self.set_value(values)

    def set_value(self, values):
        self._values = copy.deepcopy(values)
        self.list.clear()
        for value in self._values:
            name = value.get("name") or value.get("text") or value.get("process") or value.get("model") or value.get("id") or "未命名"
            self.list.addItem(("已启用 · " if value.get("enabled", True) else "已停用 · ") + str(name))

    def value(self):
        return copy.deepcopy(self._values)

    def add_rule(self):
        dialog = RuleDialog(self.section, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_value(self._values + [dialog.value()])

    def edit_rule(self):
        index = self.list.currentRow()
        if index < 0:
            return
        dialog = RuleDialog(self.section, self._values[index], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = self.value()
            values[index] = dialog.value()
            self.set_value(values)
            self.list.setCurrentRow(index)

    def remove_rule(self):
        index = self.list.currentRow()
        if index >= 0:
            values = self.value()
            values.pop(index)
            self.set_value(values)
