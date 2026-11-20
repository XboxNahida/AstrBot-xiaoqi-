"""沿用原主题的本地Gemini动作库，不创建第二个桌宠或请求模型。"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app_paths import ASSETS_DIR
from config_manager import ConfigManager
from pet_actions import load_action_catalog


class ActionLibraryPanel(QWidget):
    play_requested = pyqtSignal(str, bool)
    random_settings_changed = pyqtSignal(bool, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_mgr = ConfigManager()
        self.catalog = load_action_catalog(ASSETS_DIR)
        layout = QVBoxLayout(self)
        title = QLabel(f"Gemini 猫耳娘完整动作库 · {len(self.catalog)} 个动作")
        title.setWordWrap(True)
        layout.addWidget(title)
        note = QLabel("搜索或按分类选择，再点击播放。循环播放可随时恢复待机；拖拽、思考和互动仍会触发原动作。Ctrl+F11、中键或桌宠滚轮可依次切换全部动作。仅播放动画，不请求模型或自动播放附带音轨。尺寸和帧率沿用“核心与感知”页。")
        note.setWordWrap(True)
        layout.addWidget(note)
        random_group = QGroupBox("空闲随机动作与点击互动")
        random_form = QFormLayout(random_group)
        self.random_enabled_check = QCheckBox("启用空闲随机播放（即时保存）")
        self.random_enabled_check.setChecked(bool(self.config_mgr.get("random_actions_enabled", True)))
        random_form.addRow(self.random_enabled_check)
        self.random_interval_spin = QSpinBox()
        self.random_interval_spin.setRange(5, 3600)
        self.random_interval_spin.setSuffix(" 秒")
        self.random_interval_spin.setKeyboardTracking(False)
        try:
            interval = int(self.config_mgr.get("random_action_interval", 5))
        except (TypeError, ValueError):
            interval = 5
        self.random_interval_spin.setValue(interval)
        random_form.addRow("空闲等待间隔", self.random_interval_spin)
        random_note = QLabel("播完一遍回待机，等待后再随机，不连续重复同一动作。拖拽、思考和手动循环优先，不调用模型。连续2秒内左键点击人物5次，触发立即锐评；拖动不计入，截图和语音概率沿用现有配置。")
        random_note.setWordWrap(True)
        random_form.addRow(random_note)
        layout.addWidget(random_group)
        group = QGroupBox("动作选择与播放")
        form = QVBoxLayout(group)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入动作名称或分类，例如：吃早餐、舞蹈、工作状态")
        form.addWidget(self.search_edit)
        self.category_combo = QComboBox()
        self.category_combo.addItem("全部分类", "")
        for category in dict.fromkeys(row["category"] for row in self.catalog.values()):
            self.category_combo.addItem(category, category)
        self.category_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.category_combo.setMinimumContentsLength(8)
        self.category_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form.addWidget(self.category_combo)
        self.action_list = QListWidget()
        self.action_list.setMinimumHeight(240)
        form.addWidget(self.action_list)
        self.loop_check = QCheckBox("循环播放选中动作（可被互动或思考打断）")
        form.addWidget(self.loop_check)
        buttons = QHBoxLayout()
        self.play_button = QPushButton("播放选中动作")
        self.idle_button = QPushButton("恢复待机")
        buttons.addWidget(self.play_button)
        buttons.addWidget(self.idle_button)
        form.addLayout(buttons)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        form.addWidget(self.status_label)
        layout.addWidget(group)
        license_note = QLabel("资源：hunmer/aboutme-pets；基础立绘：lokicorvus/crosspet，CC BY-NC-SA 4.0（署名、非商业、相同方式共享）。")
        license_note.setWordWrap(True)
        layout.addWidget(license_note)
        layout.addStretch()
        self.search_edit.textChanged.connect(self.refresh)
        self.category_combo.currentIndexChanged.connect(self.refresh)
        self.play_button.clicked.connect(self.play_selected)
        self.idle_button.clicked.connect(lambda: self.play_requested.emit("idle", True))
        self.action_list.itemDoubleClicked.connect(lambda _item: self.play_selected())
        self.random_enabled_check.toggled.connect(self.save_random_settings)
        self.random_interval_spin.valueChanged.connect(self.save_random_settings)
        self.refresh()

    def save_random_settings(self):
        enabled = self.random_enabled_check.isChecked()
        interval = self.random_interval_spin.value()
        self.config_mgr.save({"random_actions_enabled": enabled, "random_action_interval": interval})
        self.random_settings_changed.emit(enabled, interval)

    def refresh(self):
        old = self.action_list.currentItem()
        old_key = old.data(Qt.ItemDataRole.UserRole) if old else None
        query = self.search_edit.text().strip().casefold()
        category = self.category_combo.currentData()
        self.action_list.clear()
        for key, row in self.catalog.items():
            if category and category != row["category"]:
                continue
            if query and query not in (row["title"] + row["id"] + row["category"]).casefold():
                continue
            item = QListWidgetItem(f"{row['title']} · {row['category']}")
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.action_list.addItem(item)
            if key == old_key:
                self.action_list.setCurrentItem(item)
        if self.action_list.currentRow() < 0 and self.action_list.count():
            self.action_list.setCurrentRow(0)
        self.play_button.setEnabled(self.action_list.count() > 0)
        self.status_label.setText(f"显示 {self.action_list.count()} / {len(self.catalog)} 个动作。选择和搜索不会自动播放。")

    def play_selected(self):
        item = self.action_list.currentItem()
        if item is not None:
            key = item.data(Qt.ItemDataRole.UserRole)
            self.play_requested.emit(key, self.loop_check.isChecked())
            self.status_label.setText("已请求播放：" + self.catalog[key]["title"])
