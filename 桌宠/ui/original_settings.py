"""原版完整设置中心：只追加扩展页，不重建原页面或样式。"""

from PyQt6.QtWidgets import QLabel, QPushButton, QScrollArea

from desktop_control.config import ControlStore
from pet_features.config import FeaturesStore
from smart_config import SmartSettingsStore

from .action_settings import ActionLibraryPanel
from .companion_settings import CompanionSettings
from .desktop_control import ControlPanel
from .features_settings import FeaturesSettingsPanel
from .settings_window import SettingsWindow
from .smart_settings import SmartSettingsPanel


class ExtendedSettingsWindow(SettingsWindow):
    def __init__(self, parent=None, is_dynamic=True, store=None):
        super().__init__(parent, is_dynamic=is_dynamic)
        self.extension = CompanionSettings(store)
        self.extension.setMinimumSize(0, 0)
        # 新页继承原设置中心的主题；原页面 stylesheet 不做改动。
        self.extension.setStyleSheet("")
        self.extension.route_combo.insertItem(0, "沿用原版通道（在核心与感知页配置）", "original")
        cfg = self.extension.config
        if not self.extension.store.path.exists() or (
            cfg["route"] == "direct" and not any(p.get("model") for p in cfg["profiles"])
        ):
            cfg["route"] = "original"
        self.extension.route_combo.setCurrentIndex(self.extension.route_combo.findData(cfg["route"]))
        # 重复的扩展版控件不参与原程序配置；原功能全部保留在原页面。
        for index in range(self.extension.tabs.count()):
            if self.extension.tabs.tabText(index) == "感知与语音":
                self.extension.tabs.setTabVisible(index, False)
        form = self.extension.persona_edit.parentWidget().layout().itemAt(4)
        # 人设由原人设页统一管理，扩展模型页不再提供第二份人设。
        self.extension.persona_edit.hide()
        if form is not None and form.layout() is not None:
            label = form.layout().labelForField(self.extension.persona_edit)
            if label is not None:
                label.hide()
        self.extension.chat_input.setPlaceholderText("输入消息，使用原版人设与记忆继续互动…")
        self.extension.feedback.setText("人设、语音、感知仍在原页面设置。这里也可使用窗口底部“保存并立即生效”。云端截图答疑和当日记录需安装新版插件。")
        for label in self.extension.findChildren(QLabel):
            if label.text() == "小七桌宠":
                label.setText("云端连接与模型")
        self.extension.save_button.setText("保存连接与模型")
        for button in self.extension.findChildren(QPushButton):
            if button.text() == "收起设置，保留桌宠":
                button.hide()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.extension)
        self.extension_tab = self.tabs.addTab(scroll, "云端与模型")
        self._protect_combo_wheels(self.extension)
        self.smart_panel = SmartSettingsPanel(
            SmartSettingsStore(self.extension.store.path.with_name("smart_settings.json")),
            profiles_provider=lambda: [
                {key: profile.get(key, "") for key in ("id", "name", "model")}
                for profile in self.extension.config["profiles"]
            ],
        )
        smart_scroll = QScrollArea()
        smart_scroll.setWidgetResizable(True)
        smart_scroll.setWidget(self.smart_panel)
        self.smart_tab = self.tabs.insertTab(self.extension_tab, smart_scroll, "智能陪伴")
        self.extension_tab += 1
        self._protect_combo_wheels(self.smart_panel)
        if is_dynamic:
            self.action_panel = ActionLibraryPanel()
            action_scroll = QScrollArea()
            action_scroll.setWidgetResizable(True)
            action_scroll.setWidget(self.action_panel)
            self.action_tab = self.tabs.insertTab(self.smart_tab, action_scroll, "Gemini动作库")
            self.smart_tab += 1
            self.extension_tab += 1
            self._protect_combo_wheels(self.action_panel)
        self.features_panel = FeaturesSettingsPanel(
            FeaturesStore(self.extension.store.path.with_name("features_settings.json")),
            profiles_provider=lambda: self.extension.config["profiles"],
        )
        self.features_tab = self.tabs.insertTab(self.extension_tab, self.features_panel, "扩展功能")
        self.extension_tab += 1
        self._protect_combo_wheels(self.features_panel)
        self.desktop_panel = ControlPanel(
            ControlStore(self.extension.store.path.with_name("desktop_control_settings.json")),
            profiles_provider=lambda: self.extension.config["profiles"],
        )
        self.desktop_tab = self.tabs.insertTab(self.extension_tab, self.desktop_panel, "桌面操作")
        self.extension_tab += 1
        self._fixed_mode_controls = {
            control: (control.isEnabled(), control.toolTip())
            for control in (self.chk_auto, self.slider_cooldown, self.spin_cooldown,
                            self.lbl_cooldown_val, self.extension.auto_check,
                            self.extension.cooldown_spin)
        }
        self.smart_panel.settings_saved.connect(self._apply_smart_mode)
        self.extension.settings_saved.connect(lambda _cfg: self.smart_panel.refresh_profiles())
        self.extension.settings_saved.connect(lambda _cfg: self.desktop_panel.refresh_profiles())
        self.tabs.currentChanged.connect(self._refresh_smart_tab)
        self._apply_smart_mode(self.smart_panel.config)

    def _refresh_smart_tab(self, index):
        if index == self.smart_tab:
            self.smart_panel.refresh_profiles()
        if index == self.desktop_tab:
            self.desktop_panel.refresh_profiles()

    def _apply_smart_mode(self, config):
        enabled = config["enabled"]
        for control, (was_enabled, tooltip) in self._fixed_mode_controls.items():
            control.setEnabled(was_enabled and not enabled)
            control.setToolTip(
                "智能陪伴已启用，原固定间隔模式停用；请在智能陪伴页调整或关闭智能模式。"
                if enabled else tooltip
            )

    def open_window(self):
        # 真实 SW_HIDE 启动时 Qt 与 Win32 可见状态可能不同，沿用已验收恢复方式。
        # 先消费首次显示的隐藏启动参数，再强制重新显示。
        self.show()
        self.hide()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def save_and_apply(self):
        if hasattr(self, "hotkey_tab") and self.tabs.currentIndex() == self.hotkey_tab:
            self.hotkey_panel.save_settings()
        elif hasattr(self, "desktop_tab") and self.tabs.currentIndex() == self.desktop_tab:
            self.desktop_panel.save_settings()
        elif hasattr(self, "features_tab") and self.tabs.currentIndex() == self.features_tab:
            self.features_panel.save_settings()
        elif hasattr(self, "action_tab") and self.tabs.currentIndex() == self.action_tab:
            self.action_panel.status_label.setText("动作播放即时生效，无需保存。")
        elif hasattr(self, "smart_tab") and self.tabs.currentIndex() == self.smart_tab:
            self.smart_panel.save_settings()
        elif hasattr(self, "extension_tab") and self.tabs.currentIndex() == self.extension_tab:
            self.extension.save_settings()
        else:
            super().save_and_apply()

    def open_extension(self):
        self.tabs.setCurrentIndex(self.extension_tab)
        self.open_window()

    def open_features(self):
        self.tabs.setCurrentIndex(self.features_tab)
        self.open_window()
