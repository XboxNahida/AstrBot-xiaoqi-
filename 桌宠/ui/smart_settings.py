"""智能陪伴独立设置，沿用原设置中心主题与底部保存按钮。"""

import copy

from PyQt6.QtCore import Qt, QTime, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

# 数值范围与 smart_config 的存储校验保持一致，所有数值均可键盘输入。
_BOUNDS = {
    "debounce_seconds": (1, 600, " 秒"), "dedup_minutes": (1, 1440, " 分钟"),
    "cooldown_seconds": (1, 3600, " 秒"), "icebreaker_minutes": (1, 1440, " 分钟"),
    "context_chars": (50, 4000, " 字符"), "max_output_tokens": (64, 4096, " Token"),
    "hourly_limit": (1, 100, " 次"), "daily_limit": (1, 1000, " 次"),
    "daily_tokens": (1000, 10000000, " Token"), "backoff_minutes": (1, 1440, " 分钟"),
    "voice_interval_seconds": (1, 3600, " 秒"), "speech_chars": (20, 500, " 字符"),
    "volume": (0, 100, " %"), "asr_threshold": (1, 10000, " PCM幅度"),
    "asr_daily_count": (1, 1000, " 次"), "asr_daily_seconds": (1, 86400, " 秒"),
}
_GROUPS = (
    ("模式与场景", (
        ("enabled", "启用智能陪伴（取代原固定间隔模式）"),
        ("events_enabled", "监听窗口变化事件"), ("normalize_title", "归一化窗口标题"),
        ("debounce_enabled", "启用稳定场景消抖"), ("debounce_seconds", "场景稳定时间"),
        ("dedup_enabled", "启用相同场景去重"), ("dedup_minutes", "场景去重时间"),
        ("cooldown_enabled", "限制自主请求最小间隔"), ("cooldown_seconds", "自主请求最小间隔"),
        ("meeting_enabled", "会议或麦克风使用时暂停自主互动"),
        ("dnd_enabled", "立即免打扰"), ("dnd_schedule_enabled", "启用定时免打扰"),
        ("dnd_start", "免打扰开始"), ("dnd_end", "免打扰结束"),
        ("icebreaker_enabled", "启用自主破冰"), ("icebreaker_minutes", "破冰间隔"),
    )),
    ("模型与上下文", (
        ("separate_model", "主动聊天与总结使用独立模型配置"), ("profile_id", "聊天与总结独立模型"),
        ("context_enabled", "加入相关简短上下文"), ("context_chars", "上下文最大长度"),
        ("short_output", "发送生成预算上限"), ("max_output_tokens", "总生成Token（含思考）"),
        ("low_reasoning", "发送低思考强度选项（需模型支持）"), ("reasoning_mode", "思考强度"),
    )),
    ("模型调用预算", (
        ("count_limit_enabled", "启用模型请求次数预算"), ("hourly_limit", "每小时最多请求"),
        ("daily_limit", "每天最多请求"), ("token_limit_enabled", "启用每日Token软预算"),
        ("daily_tokens", "每日Token目标"), ("backoff_enabled", "失败后暂停自主请求"),
        ("backoff_minutes", "失败退避时间"),
    )),
    ("语音播报", (
        ("voice_rules_enabled", "按来源决定是否播报"),
        ("voice_sources", "各来源独立开关与播报概率"),
        ("device_filter_enabled", "只允许选定的默认输出设备播报"),
        ("allowed_devices", "允许的音频输出设备（可多选）"),
        ("voice_interval_enabled", "限制自主播报最小间隔"),
        ("voice_interval_seconds", "自主播报最小间隔"),
        ("voice_cache_enabled", "缓存重复语音"), ("clean_speech", "朗读前清理格式和表情标签"),
        ("short_speech", "限制朗读长度"), ("speech_chars", "最长朗读字符数"),
        ("user_interrupt", "用户对话优先打断自主播报"), ("volume", "播报音量"),
    )),
    ("语音识别省调用", (
        ("asr_filter_enabled", "本地过滤静音录音"), ("asr_trim_enabled", "裁剪录音首尾静音"),
        ("asr_threshold", "静音判断幅度阈值"), ("asr_confirm", "上传识别前先确认"),
        ("asr_limit_enabled", "启用每日语音识别预算"),
        ("asr_daily_count", "每日识别次数上限"), ("asr_daily_seconds", "每日识别音频时长上限"),
    )),
)
_SOURCES = {
    "user": "主动文字或语音互动", "manual": "手动立即锐评", "passive": "自主场景观察",
    "icebreaker": "自主破冰", "greeting": "问候与本地状态提示", "cloud": "云端通知", "reminder": "更新提醒",
}


class SmartSettingsPanel(QWidget):
    settings_saved = pyqtSignal(dict)

    def __init__(self, store, profiles_provider=None):
        super().__init__()
        self.store = store
        self.config = store.load()
        self.profiles_provider = profiles_provider or list
        self.controls = {}
        self._devices_loaded = False
        root = QVBoxLayout(self)
        self._note(root, "智能模式启用并保存后，原固定间隔巡逻与自动感知控件停用；关闭并保存后恢复原模式和原数值。")
        self._note(root, "立即锐评、自主观察及破冰统一跟随“云端与模型”页保存的主模型，旧独立模型选择不再影响锐评；沿用原版通道时跟随“核心与感知”。本页独立模型选项仅用于主动聊天和总结。所有智能请求仍使用本页输出参数和预算。完整人设保留，每次观察附截图；立即锐评触发即截图。")
        self._note(root, "完整气泡文字立即显示，独立于概率、静音、设备条件和语音合成。锐评主模型需要支持图片；云端智能锐评通道暂不支持时会明确提示。此页不修改既有模型密钥或原音色。")
        self._note(root, "总生成Token可能包含内部思考，256对思考模型可能在生成正文前耗尽。可调高总预算或选择支持关闭思考的模型；关闭/低思考及上限是否生效由服务商决定，实际usage可能超出请求上限，不是费用硬封顶。它也不限制完整人设与截图输入量。Token软预算按实际usage控制；启用预算且用量未知时暂停后续模型请求。关闭智能模式恢复原请求通道。开始录音始终停播，防止录到桌宠自己的声音。")
        self._note(root, "Gemini 3 系列不能完全关闭思考。选择“关闭 / 最低”时，Flash使用最低minimal，Pro使用最低low；选择“低”时发送low。原生Gemini与OpenAI兼容接口均按此适配，其他模型仍发送原关闭参数；失败不会自动重试生成。")
        for title, fields in _GROUPS:
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            for key, label in fields:
                control = self._make_control(key, label)
                self.controls[key] = control
                if isinstance(control, QCheckBox):
                    form.addRow(control)
                elif key == "voice_sources":
                    box = QWidget()
                    rows = QFormLayout(box)
                    for entry in control.values():
                        line = QHBoxLayout()
                        line.addWidget(entry["enabled"])
                        line.addWidget(entry["probability"])
                        rows.addRow(line)
                    form.addRow(box)
                else:
                    caption = QLabel(label)
                    caption.setWordWrap(True)
                    form.addRow(caption, control)
                if key == "profile_id":
                    button = QPushButton("刷新已有模型配置")
                    button.clicked.connect(self.refresh_profiles)
                    form.addRow(button)
                if key == "allowed_devices":
                    button = QPushButton("刷新输出设备（不播放声音）")
                    button.clicked.connect(self.refresh_devices)
                    form.addRow(button)
                    self.device_status = QLabel("设备尚未加载；打开本页或点击刷新读取当前输出设备。")
                    self.device_status.setWordWrap(True)
                    form.addRow(self.device_status)
            root.addWidget(group)
        self.status_label = QLabel("设置仅保存到独立智能配置。修改后点击窗口底部“保存并立即生效”。")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)
        root.addStretch()
        self.refresh_profiles()

    @staticmethod
    def _note(layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    def _make_control(self, key, label):
        value = self.config[key]
        if type(value) is bool:
            control = QCheckBox(label)
            control.setChecked(value)
        elif type(value) is int:
            control = QSpinBox()
            low, high, suffix = _BOUNDS[key]
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setValue(value)
        elif key in ("dnd_start", "dnd_end"):
            control = QTimeEdit(QTime.fromString(value, "HH:mm"))
            control.setDisplayFormat("HH:mm")
        elif key == "voice_sources":
            controls = {}
            for source, entry in value.items():
                enabled = QCheckBox(_SOURCES.get(source, source))
                enabled.setChecked(entry["enabled"])
                probability = QSpinBox()
                probability.setRange(0, 100)
                probability.setSuffix(" %")
                probability.setValue(entry["probability"])
                controls[source] = {"enabled": enabled, "probability": probability}
            return controls
        elif key == "allowed_devices":
            control = QListWidget()
            control.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
            control.setMinimumHeight(110)
            control.setMaximumHeight(180)
            self._fill_devices(control, [], value)
        else:
            control = QComboBox()
            control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            control.setMinimumContentsLength(12)
            control.setMinimumWidth(0)
            control.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            if key == "reasoning_mode":
                control.addItem("关闭 / 最低（按模型支持）", "off")
                control.addItem("低（模型必须支持）", "low")
                control.setCurrentIndex(control.findData(value))
        return control

    def refresh_profiles(self):
        combo = self.controls["profile_id"]
        selected = combo.currentData() if combo.count() else self.config["profile_id"]
        combo.clear()
        combo.addItem("请选择既有视觉直连模型", "")
        try:
            for profile in self.profiles_provider():
                combo.addItem(f"{profile['name']} / {profile.get('model', '')}", profile["id"])
            if selected and combo.findData(selected) < 0:
                combo.addItem("已选配置不可用（请重新选择）", selected)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        except (KeyError, TypeError, ValueError, OSError):
            self.status_label.setText("无法刷新模型配置，请检查云端与模型页。")

    @staticmethod
    def _fill_devices(listing, devices, selected):
        listing.clear()
        known = set()
        for device in devices:
            device_id = device["id"]
            known.add(device_id)
            suffix = "（默认）" if device.get("default") else ""
            if not device.get("available", True):
                suffix += "（离线）"
            item = QListWidgetItem(str(device["name"]) + suffix, listing)
            item.setData(Qt.ItemDataRole.UserRole, device_id)
            item.setSelected(device_id in selected)
        for device_id in selected:
            if device_id not in known:
                item = QListWidgetItem("离线已选设备：" + device_id, listing)
                item.setData(Qt.ItemDataRole.UserRole, device_id)
                item.setSelected(True)

    def refresh_devices(self):
        listing = self.controls["allowed_devices"]
        selected = [item.data(Qt.ItemDataRole.UserRole) for item in listing.selectedItems()]
        try:
            from smart_audio import audio_devices

            devices = audio_devices()
            self._fill_devices(listing, devices, selected)
            self.device_status.setText("当前读取到输出设备；设备切换或拔出会停止播放，不能回退到扬声器。" if devices else "未检测到可用输出设备，已保留离线选择。")
            self._devices_loaded = True
        except (ImportError, RuntimeError, OSError, ValueError) as exc:
            self.device_status.setText("输出设备尚未加载（" + type(exc).__name__ + "），请稍后刷新；已选设备保留。")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._devices_loaded:
            self.refresh_devices()

    def save_settings(self):
        updated = copy.deepcopy(self.config)
        for key, control in self.controls.items():
            if isinstance(control, QCheckBox):
                updated[key] = control.isChecked()
            elif isinstance(control, QSpinBox):
                updated[key] = control.value()
            elif isinstance(control, QTimeEdit):
                updated[key] = control.time().toString("HH:mm")
            elif isinstance(control, QComboBox):
                updated[key] = control.currentData()
            elif isinstance(control, QListWidget):
                updated[key] = [item.data(Qt.ItemDataRole.UserRole) for item in control.selectedItems()]
            else:
                updated[key] = {source: {"enabled": entry["enabled"].isChecked(), "probability": entry["probability"].value()} for source, entry in control.items()}
        try:
            self.store.save(updated)
        except (ValueError, OSError) as exc:
            self.status_label.setText("保存失败：" + str(exc))
            return False
        self.config = updated
        self.settings_saved.emit(copy.deepcopy(updated))
        self.status_label.setText("已保存并应用智能陪伴设置。")
        return True
