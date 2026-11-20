"""追加到原语音区域的模型配置，继承原设置中心样式。"""

import threading
import uuid

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from speech_models import (
    SPEECH_PRESETS,
    SpeechSettingsStore,
    active_profile,
    list_models,
)


class SpeechModelSettings(QWidget):
    models_ready = pyqtSignal(object, object, str)

    def __init__(self, store=None):
        super().__init__()
        self.store = store or SpeechSettingsStore()
        self.config = self.store.load()
        self.current_id = self.config["active_profile"]
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("腾讯云一句话识别（使用上方密钥）", "tencent")
        self.engine_combo.addItem("自定义语音模型（OpenAI / MiMo）", "openai")
        form.addRow("识别方式:", self.engine_combo)
        root.addLayout(form)
        self.custom_panel = QWidget()
        custom = QVBoxLayout(self.custom_panel)
        custom.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.profile_combo = QComboBox()
        row.addWidget(self.profile_combo, 1)
        for text, callback in (("新增一套", self.add_profile), ("移除这套", self.remove_profile)):
            button = QPushButton(text)
            button.setObjectName("SecondaryBtn")
            button.clicked.connect(callback)
            row.addWidget(button)
        custom.addLayout(row)
        fields = QFormLayout()
        self.name_edit, self.base_edit, self.key_edit = QLineEdit(), QLineEdit(), QLineEdit()
        self.preset_combo, self.model_combo = QComboBox(), QComboBox()
        self.protocol_combo = QComboBox()
        self.protocol_combo.addItem("自动（兼容已有配置）", "auto")
        self.protocol_combo.addItem("OpenAI 音频转写", "openai")
        self.protocol_combo.addItem("MiMo 语音识别", "mimo")
        self.preset_combo.addItems(SPEECH_PRESETS)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴API密钥；本机免密接口可以留空")
        self.base_edit.setPlaceholderText("https://服务商地址/v1")
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model_combo.setPlaceholderText("例如 whisper-1；请填写服务商支持的识别模型")
        for label, widget in (("配置名称", self.name_edit), ("服务商", self.preset_combo),
                              ("接口协议", self.protocol_combo), ("接口地址", self.base_edit),
                              ("API密钥", self.key_edit), ("模型", self.model_combo)):
            fields.addRow(label, widget)
        custom.addLayout(fields)
        self.fetch_button = QPushButton("读取模型列表")
        self.fetch_button.setObjectName("SecondaryBtn")
        self.fetch_button.clicked.connect(self.fetch_models)
        custom.addWidget(self.fetch_button)
        hint = QLabel("填写方式与“云端与模型”页相同；支持音频转文字接口。模型也可直接填写。\n读取列表不代表识别测试通过；填写并保存后，按Ctrl+F1录音验证。密钥仅在本机加密保存。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a49cb8; font-size: 11px;")
        custom.addWidget(hint)
        root.addWidget(self.custom_panel)
        self.feedback = QLabel("")
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        self._refresh_profiles()
        self.engine_combo.setCurrentIndex(self.engine_combo.findData(self.config["engine"]))
        self.custom_panel.setVisible(self.config["engine"] == "openai")
        self.engine_combo.currentIndexChanged.connect(lambda: self.custom_panel.setVisible(self.engine_combo.currentData() == "openai"))
        self.profile_combo.currentIndexChanged.connect(self._switch_profile)
        self.preset_combo.currentTextChanged.connect(self._preset_changed)
        self.models_ready.connect(self._models_ready)

    def _capture(self):
        self.config["active_profile"] = self.current_id
        profile = active_profile(self.config)
        profile.update(name=self.name_edit.text().strip(), preset=self.preset_combo.currentText(),
                       base_url=self.base_edit.text().strip(), key=self.key_edit.text().strip(),
                       model=self.model_combo.currentText().strip(), protocol=self.protocol_combo.currentData())

    def _refresh_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.config["profiles"]:
            self.profile_combo.addItem(profile["name"], profile["id"])
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(self.current_id))
        self.profile_combo.blockSignals(False)
        self.config["active_profile"] = self.current_id
        profile = active_profile(self.config)
        self.name_edit.setText(profile["name"])
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(profile.get("preset", "自定义 / 本机语音模型"))
        self.preset_combo.blockSignals(False)
        self.base_edit.setText(profile["base_url"])
        self.key_edit.setText(profile["key"])
        self.model_combo.clear()
        self.model_combo.addItems(profile.get("models", []))
        self.model_combo.setEditText(profile["model"])
        self.protocol_combo.setCurrentIndex(self.protocol_combo.findData(profile.get("protocol", "auto")))

    def _switch_profile(self, index):
        if index < 0:
            return
        target = self.profile_combo.itemData(index)
        self._capture()
        self.current_id = target
        self._refresh_profiles()

    def _preset_changed(self, preset):
        self.base_edit.setText(SPEECH_PRESETS[preset])
        self.model_combo.clear()
        if preset == "OpenAI":
            self.model_combo.addItems(["whisper-1", "gpt-4o-mini-transcribe", "gpt-4o-transcribe"])
        elif preset == "小米 MiMo ASR":
            self.model_combo.addItem("mimo-v2.5-asr")
            self.model_combo.setEditText("mimo-v2.5-asr")
        protocol = "mimo" if preset == "小米 MiMo ASR" else "openai" if preset == "OpenAI" else "auto"
        self.protocol_combo.setCurrentIndex(self.protocol_combo.findData(protocol))

    def add_profile(self):
        self._capture()
        profile = SpeechSettingsStore.defaults()["profiles"][0]
        self.current_id = profile["id"] = uuid.uuid4().hex
        profile["name"] = "新语音模型"
        self.config["profiles"].append(profile)
        self._refresh_profiles()

    def remove_profile(self):
        if len(self.config["profiles"]) == 1:
            self.feedback.setText("至少保留一套语音模型配置。")
            return
        self.config["profiles"] = [p for p in self.config["profiles"] if p["id"] != self.current_id]
        self.current_id = self.config["profiles"][0]["id"]
        self._refresh_profiles()

    def _list_scope(self):
        return self.current_id, self.base_edit.text().strip(), self.key_edit.text().strip()

    def fetch_models(self):
        self._capture()
        profile, scope = dict(active_profile(self.config)), self._list_scope()
        self.fetch_button.setEnabled(False)
        self.feedback.setText("正在读取语音服务的模型列表…")

        def worker():
            try:
                result, error = list_models(profile), ""
            except Exception as exc:  # noqa: BLE001 -- 后台任务必须向界面返回成功或失败。
                result, error = [], str(exc) if isinstance(exc, ValueError) else "读取失败，请检查服务地址和网络"
            try:
                self.models_ready.emit(scope, result, error)
            except RuntimeError:
                pass  # 设置窗口已经销毁。

        threading.Thread(target=worker, daemon=True).start()

    def _models_ready(self, scope, models, error):
        self.fetch_button.setEnabled(True)
        if scope != self._list_scope():
            self.feedback.setText("配置已切换，已忽略旧列表，请重新读取。")
            return
        if error:
            self.feedback.setText(error)
            return
        selected = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setEditText(selected)
        active_profile(self.config)["models"] = models
        self.feedback.setText("列表已读取，请选择语音识别模型；普通聊天模型不一定支持音频转文字。")

    def save_settings(self):
        self._capture()
        self.config["engine"] = self.engine_combo.currentData()
        self.store.save(self.config)
