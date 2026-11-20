"""原设置中心的单一扩展页，沿用主题与滚轮保护。"""
import json

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pet_features.ui_schema import ACTIONS, CATEGORIES, CHOICES, HINTS, LABELS
from ui.features_rules import RuleListEditor


class FeaturesSettingsPanel(QWidget):
    settings_saved = pyqtSignal(object)
    action_requested = pyqtSignal(str)

    def __init__(self, store, profiles_provider=None):
        super().__init__()
        self.store = store
        self.config = store.load()
        self.profiles_provider = profiles_provider or list
        self.controls = {}
        self.pages = {}
        self.setMinimumSize(0, 0)
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        body = QHBoxLayout()
        self.navigation = QListWidget()
        self.navigation.setFixedWidth(130)
        self.navigation.addItems([label for label, _sections in CATEGORIES])
        body.addWidget(self.navigation)
        self.content = QVBoxLayout()
        body.addLayout(self.content, 1)
        root.addLayout(body, 1)
        self.feedback = QLabel("新增后台功能默认关闭；原菜单、角色、模型与语音设置保留。")
        self.feedback.setWordWrap(True)
        self.feedback.setMinimumWidth(0)
        self.feedback.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        root.addWidget(self.feedback)
        self.save_button = QPushButton("保存扩展功能并立即生效")
        self.save_button.clicked.connect(self.save_settings)
        root.addWidget(self.save_button)
        self.navigation.currentRowChanged.connect(self._show_category)
        self.navigation.setCurrentRow(0)

    def eventFilter(self, obj, event):
        control = obj.parent() if isinstance(obj, QLineEdit) else obj
        if event.type() == QEvent.Type.Wheel and isinstance(control, (QComboBox, QSpinBox, QDoubleSpinBox)):
            event.ignore()
            return True
        return super().eventFilter(obj, event)

    def select_section(self, section):
        for index, (_label, sections) in enumerate(CATEGORIES):
            if section in sections:
                self.navigation.setCurrentRow(index)
                return

    def _show_category(self, index):
        if index < 0:
            return
        if hasattr(self, "current_scroll"):
            self.content.removeWidget(self.current_scroll)
            self.current_scroll.hide()
        if index not in self.pages:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setMinimumWidth(0)
            page = QWidget()
            page.setMinimumWidth(0)
            page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout = QVBoxLayout(page)
            for section in CATEGORIES[index][1]:
                self._build_section(layout, section)
            layout.addStretch()
            scroll.setWidget(page)
            self.pages[index] = scroll
        self.current_scroll = self.pages[index]
        self.content.addWidget(self.current_scroll)
        self.current_scroll.show()

    def _build_section(self, layout, section):
        titles = {"feeding": "互动与喂食", "weather": "天气与城市", "media": "音乐与歌词", "system": "系统状态",
            "apps": "应用使用提醒", "reminders": "定时提醒", "phrases": "本地语录", "hotkeys": "全局快捷键",
            "agent": "开发工具联动", "balance": "余额与模型价格", "usage": "用量统计", "behavior": "行为与窗口",
            "resources": "自定义资源", "sound": "本地音效", "appearance": "外观与性能", "cloud": "云端摘要",
            "interaction": "交互与物理"}
        title = QLabel(titles.get(section, section))
        title.setObjectName("section_" + section)
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        if section in HINTS:
            hint = QLabel(HINTS[section])
            hint.setWordWrap(True)
            hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(hint)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for key, value in self.config[section].items():
            widget = self._field(section, key, value)
            if widget is None:
                continue
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.controls[(section, key)] = widget
            widget.installEventFilter(self)
            if isinstance(widget, (QComboBox, QSpinBox, QDoubleSpinBox)):
                for child in widget.findChildren(QLineEdit):
                    child.installEventFilter(self)
            form.addRow(LABELS.get(key, key), widget)
        layout.addLayout(form)
        for label, action in ACTIONS.get(section, []):
            button = QPushButton(label)
            button.setObjectName("SecondaryBtn")
            button.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(a))
            layout.addWidget(button)

    def _field(self, section, key, value):
        if isinstance(value, list):
            return RuleListEditor(section, value, self)
        if type(value) is bool:
            widget = QCheckBox()
            widget.setChecked(value)
        elif key in CHOICES or key == "profile_id" or (section, key) == ("resources", "selected"):
            widget = QComboBox()
            if key == "profile_id":
                choices = [("当前选中的模型配置", "")] + [
                    (f"{p.get('name', '未命名模型')}（{p['id']}）", p["id"])
                    for p in self.profiles_provider()]
            elif key == "selected":
                choices = [("默认角色", "")] + [
                    (f"{p.get('name', '自建资源')}（{p['id']}）", p["id"])
                    for p in self.config["resources"]["items"] if p.get("id")]
            else:
                choices = CHOICES[key]
            for label, data in choices:
                widget.addItem(label, data)
            if widget.findData(value) < 0:
                widget.addItem("保留已保存项（" + str(value) + "）", value)
            widget.setCurrentIndex(max(0, widget.findData(value)))
        elif type(value) in (int, float):
            widget = QDoubleSpinBox() if type(value) is float else QSpinBox()
            if isinstance(widget, QDoubleSpinBox) and section == "interaction":
                widget.setDecimals(3)
                widget.setSingleStep(0.001 if key in ("friction", "restitution") else 0.1)
            low, high = (-3600, 3600) if key == "offset_seconds" else (0, 1000000)
            if section == "interaction":
                low, high = {"gravity": (0, 3000), "restitution": (0, 1),
                             "friction": (0.8, 1), "sling_power": (1, 30),
                             "sleep_minutes": (1, 120)}.get(key, (low, high))
            if key.endswith("probability") or key in ("opacity", "volume"):
                high = 100
            widget.setRange(low, high)
            widget.setValue(value)
        elif isinstance(value, str):
            widget = QLineEdit(value)
            widget.setPlaceholderText("填写后保存生效")
        else:
            return None
        return widget

    def capture(self):
        cfg = json.loads(json.dumps(self.config))
        for (section, key), widget in self.controls.items():
            if isinstance(widget, RuleListEditor):
                value = widget.value()
            elif isinstance(widget, QCheckBox):
                value = widget.isChecked()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                value = widget.value()
            else:
                value = widget.text().strip()
            cfg[section][key] = value
        return cfg

    def save_settings(self):
        try:
            cfg = self.capture()
            self.store.save(cfg)
        except (ValueError, OSError) as exc:
            self.feedback.setText("未保存：" + str(exc))
            return False
        self.config = cfg
        self.settings_saved.emit(cfg)
        self.feedback.setText("扩展配置已保存并生效。")
        return True

    def refresh_section(self, name):
        for index, (_label, sections) in enumerate(CATEGORIES):
            if name in sections and index in self.pages:
                page = self.pages.pop(index)
                self.content.removeWidget(page)
                page.hide()
                page.deleteLater()
                self.controls = {k: v for k, v in self.controls.items() if k[0] not in sections}
                if self.navigation.currentRow() == index:
                    del self.current_scroll
                    self._show_category(index)
                break

    def show_result(self, text, *, title='扩展功能', force_dialog=False):
        self.feedback.setText(text)
        if force_dialog or len(text) > 240:
            QMessageBox.information(self, title, text)
