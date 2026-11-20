"""桌宠简易配置页：选模式、填三项、保存即可切换。"""

from __future__ import annotations

import base64
import copy
import json
import threading
import uuid

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from companion_config import PRESETS, SettingsStore, validate_endpoint
from companion_models import DirectModels


class TimeoutSpinBox(QSpinBox):
    def wheelEvent(self, event):
        # 嵌入原设置时由原过滤器转交滚动页；独立打开也不得滚轮改值。
        event.ignore()


class CompanionSettings(QWidget):
    settings_saved = pyqtSignal(dict)
    request_report = pyqtSignal(bool)
    send_message = pyqtSignal(str)
    request_critique = pyqtSignal()
    task_finished = pyqtSignal(str, object, str)

    def __init__(self, store=None):
        super().__init__()
        self.store = store or SettingsStore()
        self.config = self.store.load()
        self.current_id = self.config["active_profile"]
        self.setWindowTitle("小七桌宠 · 连接与模型设置")
        self.resize(760, 720)
        self.setMinimumSize(650, 590)
        self.setStyleSheet(
            'QWidget{background:#1a162b;color:#e4e0f0;font-family:"Microsoft YaHei UI";font-size:14px;}'
            "QTabWidget::pane{border:1px solid #362d52;border-radius:10px;background:#211c38;padding:10px;}"
            "QTabBar::tab{background:#26203f;color:#a49cb8;padding:8px 18px;margin-right:4px;"
            "border-top-left-radius:8px;border-top-right-radius:8px;}"
            "QTabBar::tab:selected{background:#ff4e88;color:white;}"
            "QLineEdit,QComboBox,QSpinBox,QPlainTextEdit{background:#171326;color:white;"
            "border:1px solid #3d335c;border-radius:6px;min-height:29px;padding:2px 6px;}"
            "QLineEdit:focus,QComboBox:focus,QPlainTextEdit:focus{border:1px solid #ff4e88;}"
            "QPushButton{background:#ff4e88;color:white;border:0;border-radius:6px;"
            "min-height:30px;padding:3px 12px;}"
            "QPushButton:hover{background:#ff689a;}"
            "QPushButton:pressed{background:#e03b73;}"
            "QPushButton:disabled{background:#2f2747;color:#a49cb8;}"
            "QGroupBox{border:1px solid #3d335c;border-radius:8px;margin-top:12px;padding-top:14px;color:#ff7aa6;}"
            "QCheckBox{spacing:8px;}"
        )
        root = QVBoxLayout(self)
        title = QLabel("小七桌宠")
        title.setStyleSheet("font-size:25px;font-weight:600;")
        root.addWidget(title)
        self.connection_status = QLabel("未配对：可先配置本机模型")
        root.addWidget(self.connection_status)
        mode = QHBoxLayout()
        mode.addWidget(QLabel("语言请求"))
        self.route_combo = QComboBox()
        self.route_combo.addItem("本机直连模型", "direct")
        self.route_combo.addItem("交给云端小七", "cloud")
        self.route_combo.setCurrentIndex(0 if self.config["route"] == "direct" else 1)
        mode.addWidget(self.route_combo, 1)
        root.addLayout(mode)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._chat_tab()
        self._model_tab()
        self._cloud_tab()
        self._report_tab()
        self._perception_tab()
        self.feedback = QLabel(
            "首次使用：模型配置 → 填写地址、密钥、模型 → 保存并应用。"
        )
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)
        row = QHBoxLayout()
        self.save_button = QPushButton("保存并应用")
        self.save_button.setStyleSheet(
            "background:#ff4e88;color:white;border-radius:6px;"
        )
        self.save_button.clicked.connect(self.save_settings)
        hide_button = QPushButton("收起设置，保留桌宠")
        hide_button.clicked.connect(self.hide)
        row.addWidget(self.save_button)
        row.addWidget(hide_button)
        root.addLayout(row)
        self.task_finished.connect(self._task_result)
        self._refresh_profiles()

    def _chat_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        intro = QLabel("聊天使用当前已保存的模式和模型。点击桌宠气泡也可以输入。")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.chat_log = QPlainTextEdit()
        self.chat_log.setReadOnly(True)
        layout.addWidget(self.chat_log, 1)
        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText("输入消息，与小七聊聊…")
        self.chat_input.returnPressed.connect(self._send_chat)
        layout.addWidget(self.chat_input)
        self.chat_button = QPushButton("发送")
        self.chat_button.clicked.connect(self._send_chat)
        layout.addWidget(self.chat_button)
        self.tabs.addTab(panel, "聊天")

    def _model_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        row = QHBoxLayout()
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._switch_profile)
        row.addWidget(self.profile_combo, 1)
        add = QPushButton("新增一套")
        add.clicked.connect(self.add_profile)
        remove = QPushButton("移除这套")
        remove.clicked.connect(self.remove_profile)
        row.addWidget(add)
        row.addWidget(remove)
        layout.addLayout(row)
        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(PRESETS)
        self.preset_combo.currentTextChanged.connect(self._preset_changed)
        self.base_edit = QLineEdit()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("粘贴API密钥；本机免密接口可以留空")
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        form.addRow("配置名称", self.name_edit)
        form.addRow("服务商", self.preset_combo)
        form.addRow("接口地址", self.base_edit)
        form.addRow("API密钥", self.key_edit)
        form.addRow("模型", self.model_combo)
        self.timeout_spin = TimeoutSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.timeout_spin.setValue(120)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setToolTip("连续未收到任何数据才超时；心跳、思考数据和正文都会续期。")
        form.addRow("无数据超时", self.timeout_spin)
        self.stream_usage_check = QCheckBox('请求流式Token用量统计')
        self.stream_usage_check.setToolTip('默认开启；仅当兼容中转站明确拒绝stream_options参数时关闭，不会自动重试生成。')
        form.addRow('', self.stream_usage_check)
        layout.addLayout(form)
        row = QHBoxLayout()
        self.fetch_button = QPushButton("读取模型列表")
        self.fetch_button.clicked.connect(lambda: self._model_task("models"))
        self.test_button = QPushButton("测试当前模型")
        self.test_button.clicked.connect(lambda: self._model_task("test"))
        row.addWidget(self.fetch_button)
        row.addWidget(self.test_button)
        layout.addLayout(row)
        hint = QLabel(
            "模型可以下拉选择，也能直接输入。测试会发送一次简短请求。\n"
            "无数据超时：5～600秒，每套配置独立；持续收到数据不按整轮耗时中断。建议先用120秒。\n"
            "适用本机聊天、锐评、答疑及操作规划；沿用原版模型时请改“核心与感知”页超时。"
            "云端生成超时由服务器控制；桌面整任务时限另算。\n"
            "多套配置分别保存；切换后点“保存并应用”生效。密钥仅在本机加密保存。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.persona_edit = QPlainTextEdit(self.config["system_prompt"])
        self.persona_edit.setMaximumHeight(110)
        form2 = QFormLayout()
        form2.addRow("本机聊天人设", self.persona_edit)
        layout.addLayout(form2)
        layout.addStretch()
        self.tabs.addTab(panel, "模型配置")

    def _cloud_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        info = QLabel(
            "云端安装桌宠连接插件后，获取配对信息并粘贴到这里。\n无论聊天选择哪条路径，已配对的设备都可保持双向连接。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        self.pair_edit = QPlainTextEdit()
        self.pair_edit.setPlaceholderText("粘贴云端返回的配对信息…")
        self.pair_edit.setMaximumHeight(100)
        layout.addWidget(self.pair_edit)
        pair_button = QPushButton("读取配对信息")
        pair_button.clicked.connect(self.import_pairing)
        layout.addWidget(pair_button)
        group = QGroupBox("连接信息（也可以手动填写）")
        form = QFormLayout(group)
        self.cloud_url = QLineEdit(self.config["cloud"]["url"])
        self.cloud_url.setPlaceholderText("wss://你的服务器/desktop/ws")
        self.device_id = QLineEdit(self.config["cloud"]["device_id"])
        self.cloud_token = QLineEdit(self.config["cloud"]["token"])
        self.cloud_token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("云端地址", self.cloud_url)
        form.addRow("设备标识", self.device_id)
        form.addRow("配对密钥", self.cloud_token)
        layout.addWidget(group)
        layout.addStretch()
        self.tabs.addTab(panel, "云端连接")

    def _perception_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.tts_check = QCheckBox("开启本机语音播报（Edge-TTS，Ctrl+F10切换）")
        self.tts_check.setChecked(self.config["tts_enabled"])
        self.auto_check = QCheckBox("开启自动感知并评论当前应用")
        self.auto_check.setChecked(self.config.get("auto_mode", False))
        self.screenshot_check = QCheckBox("锐评时附带目标窗口区域截图（仅本机直连视觉模型）")
        self.screenshot_check.setChecked(self.config.get("critique_screenshot", False))
        for control in (self.tts_check, self.auto_check, self.screenshot_check):
            layout.addWidget(control)
        self.cooldown_spin = QSpinBox()
        self.cooldown_spin.setRange(15, 3600)
        self.cooldown_spin.setValue(self.config.get("critique_cooldown", 45))
        self.cooldown_spin.setSuffix(" 秒")
        form = QFormLayout()
        form.addRow("自动锐评冷却", self.cooldown_spin)
        layout.addLayout(form)
        info = QLabel(
            "勾选后点击保存并应用。自动感知每2.5秒检查窗口，按冷却时间请求当前模型。\n"
            "默认只分析标题、应用和场景；截图需单独开启，并会发送给所选模型。\n"
            "云端小七目前仅接收文字观察，不采集图片。未配置模型或未连接云端时不自动请求。\n"
            "Ctrl+F9：手动锐评；Ctrl+F10：语音开关；Ctrl+F11：切换动作。\n"
            "菜单或快捷键开关立即生效并保存；设置窗关闭只收起，桌宠继续运行。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        manual = QPushButton("立即呼叫锐评")
        manual.clicked.connect(self.request_critique.emit)
        layout.addWidget(manual)
        layout.addStretch()
        self.tabs.addTab(panel, "感知与语音")

    def _report_tab(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.foreground_check = QCheckBox("汇报当前窗口标题与应用名称")
        self.foreground_check.setChecked(self.config["report_foreground"])
        layout.addWidget(self.foreground_check)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(15, 3600)
        self.interval_spin.setValue(self.config["report_interval"])
        self.interval_spin.setSuffix(" 秒")
        form = QFormLayout()
        form.addRow("状态汇报间隔", self.interval_spin)
        layout.addLayout(form)
        info = QLabel(
            "默认只汇报桌宠运行与连接状态，不自动截图。\n汇报在配对后发送；离线会暂存并在重连后补发。"
        )
        info.setWordWrap(True)
        layout.addWidget(info)
        send = QPushButton("立即汇报当前状态")
        send.clicked.connect(lambda: self.request_report.emit(False))
        summarize = QPushButton("用当前模型总结后汇报")
        summarize.clicked.connect(lambda: self.request_report.emit(True))
        layout.addWidget(send)
        layout.addWidget(summarize)
        self.report_log = QPlainTextEdit()
        self.report_log.setReadOnly(True)
        layout.addWidget(self.report_log, 1)
        self.tabs.addTab(panel, "情况汇报")

    def _capture_profile(self):
        profile = next(p for p in self.config["profiles"] if p["id"] == self.current_id)
        profile.update(
            name=self.name_edit.text().strip(),
            preset=self.preset_combo.currentText(),
            provider=PRESETS[self.preset_combo.currentText()][0],
            base_url=self.base_edit.text().strip(),
            key=self.key_edit.text().strip(),
            model=self.model_combo.currentText().strip(),
            timeout_seconds=self.timeout_spin.value(),
            stream_usage=self.stream_usage_check.isChecked(),
        )
        return profile

    def _refresh_profiles(self):
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self.config["profiles"]:
            self.profile_combo.addItem(profile["name"], profile["id"])
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(self.current_id))
        self.profile_combo.blockSignals(False)
        self._load_profile()

    def _load_profile(self):
        profile = next(p for p in self.config["profiles"] if p["id"] == self.current_id)
        self.name_edit.setText(profile["name"])
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentText(profile.get("preset", "自定义 / 本机模型"))
        self.preset_combo.blockSignals(False)
        self.base_edit.setText(profile["base_url"])
        self.key_edit.setText(profile.get("key", ""))
        self.model_combo.clear()
        self.model_combo.addItems(profile.get("models", []))
        self.model_combo.setEditText(profile["model"])
        self.timeout_spin.setValue(profile.get('timeout_seconds', 120))
        self.stream_usage_check.setChecked(profile.get('stream_usage', True))

    def _switch_profile(self, index):
        if index < 0:
            return
        self._capture_profile()
        self.current_id = self.profile_combo.itemData(index)
        self._load_profile()
        self.feedback.setText("已选择配置，点“保存并应用”用于后续聊天。")

    def _preset_changed(self, preset):
        if preset in PRESETS:
            self.base_edit.setText(PRESETS[preset][1])
            self.model_combo.clear()

    def add_profile(self):
        self._capture_profile()
        self.current_id = uuid.uuid4().hex
        self.config["profiles"].append(
            {
                "id": self.current_id,
                "name": "新模型配置",
                "preset": "自定义 / 本机模型",
                "provider": "openai",
                "base_url": "http://127.0.0.1:11434/v1",
                "key": "",
                "model": "",
            }
        )
        self._refresh_profiles()

    def remove_profile(self):
        if len(self.config["profiles"]) <= 1:
            self.feedback.setText("至少保留一套模型配置。")
            return
        self.config["profiles"] = [
            p for p in self.config["profiles"] if p["id"] != self.current_id
        ]
        self.current_id = self.config["profiles"][0]["id"]
        self._refresh_profiles()
        self.feedback.setText("已移除草稿中的配置，保存后生效。")

    def save_settings(self):
        try:
            self._capture_profile()
            updated = copy.deepcopy(self.config)
            updated.update(
                route=self.route_combo.currentData(),
                active_profile=self.current_id,
                system_prompt=self.persona_edit.toPlainText(),
                report_interval=self.interval_spin.value(),
                report_foreground=self.foreground_check.isChecked(),
                tts_enabled=self.tts_check.isChecked(),
                auto_mode=self.auto_check.isChecked(),
                critique_screenshot=self.screenshot_check.isChecked(),
                critique_cooldown=self.cooldown_spin.value(),
            )
            updated["cloud"] = {
                "url": self.cloud_url.text().strip(),
                "device_id": self.device_id.text().strip(),
                "token": self.cloud_token.text().strip(),
            }
            if bool(updated["cloud"]["url"]) != bool(updated["cloud"]["token"]):
                raise ValueError("云端地址与配对密钥需要一起填写，或都留空")
            self.store.save(updated)
            self.config = updated
            self._refresh_profiles()
            self.settings_saved.emit(copy.deepcopy(updated))
            self.feedback.setText("已保存并应用。可在聊天页发送消息。")
        except (ValueError, OSError) as exc:
            self.feedback.setText("保存失败：" + str(exc))

    def import_pairing(self):
        try:
            value = self.pair_edit.toPlainText().strip()
            if "desktop-link:" in value:
                value = value.split("desktop-link:", 1)[1].split()[0]
            if value.startswith("{"):
                data = json.loads(value)
            else:
                data = json.loads(
                    base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
                )
            url = validate_endpoint(data["url"], websocket=True)
            if not all(
                isinstance(data[k], str) and data[k].strip()
                for k in ("device_id", "token")
            ):
                raise ValueError("缺少设备信息")
            self.cloud_url.setText(url)
            self.device_id.setText(data["device_id"])
            self.cloud_token.setText(data["token"])
            self.pair_edit.clear()
            self.feedback.setText("配对信息已读取，点“保存并应用”连接云端。")
        except (ValueError, KeyError, TypeError, IndexError):
            self.feedback.setText("配对信息不完整或格式不正确，请重新粘贴。")

    def _model_task(self, kind):
        try:
            profile = copy.deepcopy(self._capture_profile())
            client = DirectModels(profile)
        except ValueError as exc:
            self.feedback.setText(str(exc))
            return
        self.fetch_button.setEnabled(False)
        self.test_button.setEnabled(False)
        self.profile_combo.setEnabled(False)
        self.feedback.setText(
            "正在读取模型列表…" if kind == "models" else "正在测试模型，请稍候…"
        )

        def work():
            try:
                result = client.models() if kind == "models" else client.test()
                self.task_finished.emit(
                    kind, {"profile_id": profile["id"], "value": result}, ""
                )
            except Exception as exc:  # noqa: BLE001 -- 后台接口异常统一转成界面提示。
                self.task_finished.emit(
                    kind,
                    None,
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "请求失败，请检查网络和接口配置",
                )

        threading.Thread(target=work, daemon=True).start()

    def _task_result(self, kind, payload, error):
        self.fetch_button.setEnabled(True)
        self.test_button.setEnabled(True)
        self.profile_combo.setEnabled(True)
        if error:
            self.feedback.setText(error)
        elif kind == "models":
            profile = next(
                (
                    p
                    for p in self.config["profiles"]
                    if p["id"] == payload["profile_id"]
                ),
                None,
            )
            if profile is None:
                return
            profile["models"] = payload["value"]
            if self.current_id == payload["profile_id"]:
                current = self.model_combo.currentText()
                self.model_combo.clear()
                self.model_combo.addItems(payload["value"])
                if current:
                    self.model_combo.setEditText(current)
            self.feedback.setText(
                f"已读取{len(payload['value'])}个模型，选择后保存即可。"
            )
        else:
            self.feedback.setText(payload["value"])

    def _send_chat(self):
        text = self.chat_input.text().strip()
        if text:
            self.send_message.emit(text)
            self.chat_input.clear()

    def append_chat(self, speaker, text):
        self.chat_log.appendPlainText(f"{speaker}：{text}\n")

    def open_window(self):
        print("[桌宠界面] 执行设置窗口显示", flush=True)
        # Windows隐藏启动可令Qt可见状态与原生窗口不一致；强制一次重新显示。
        if self.isVisible():
            self.hide()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        print(
            f"[桌宠界面] 设置窗口visible={self.isVisible()} minimized={self.isMinimized()}",
            flush=True,
        )

    def hideEvent(self, event):
        print("[桌宠界面] 设置窗口收到隐藏事件", flush=True)
        super().hideEvent(event)

    def closeEvent(self, event):
        print("[桌宠界面] 设置窗口收到关闭事件，将收起", flush=True)
        self.hide()
        event.ignore()
