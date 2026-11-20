"""装配原版控制器、原桌宠与原托盘，只附加连接能力。"""

import importlib
import inspect
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QApplication

from app_paths import ICON_PATH
from companion_config import SettingsStore
from original_bridge import OriginalCloudBridge
from perception.hotkey_listener import GlobalHotkeyManager
from pet_features.runtime import FeatureRuntime
from smart_audio import SmartSpeaker
from smart_runtime import SmartCompanion
from speech_input import SpeechInputController
from ui.original_settings import ExtendedSettingsWindow
from ui.tray_icon import DesktopPetTray


class _GuiCallbacks(QObject):
    requested = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.requested.connect(self.dispatch)

    def dispatch(self, callback):
        callback()


class GuiHotkeys(GlobalHotkeyManager):
    """保留原按键与回调，监听线程只发信号，不直接调用 Qt 控件。"""

    def __init__(self, **callbacks):
        self.gui = _GuiCallbacks()
        super().__init__(**{name: (lambda cb=callback: self.gui.requested.emit(cb)) for name, callback in callbacks.items()})


class _OriginalControllerExtensions:
    bridge = None
    voice_input = None
    smart = None
    features = None
    screenshot_qa = None
    desktop_control = None

    def show_settings(self):
        self.settings_win.open_window()

    def _check_desktop_state(self):
        if self.desktop_control is not None and self.desktop_control.busy:
            return
        if self.smart is not None and self.smart.cfg["enabled"]:
            return
        if self.voice_input is not None and self.voice_input.busy:
            return
        if self.bridge is None or not self.bridge.summary_busy:
            super()._check_desktop_state()

    def trigger_manual_critique(self):
        if self.desktop_control is not None and self.desktop_control.busy:
            return
        if self.voice_input is not None and self.voice_input.busy:
            return
        if self.bridge is None or not self.bridge.summary_busy:
            if self.smart is not None:
                self.smart.request_manual()
            else:
                super().trigger_manual_critique()

    def handle_user_reply(self, *args, **kwargs):
        if self.desktop_control is not None and self.desktop_control.busy:
            return
        if self.voice_input is not None and self.voice_input.busy:
            return
        if self.bridge is None or not self.bridge.summary_busy:
            if self.smart is not None:
                self.smart.last_interaction = time.time()
                if self.smart.cfg["user_interrupt"] and not self._is_analyzing:
                    self.speaker.stop()
            if self.bridge is not None and not self._is_analyzing:
                self.bridge.request_epoch += 1
            super().handle_user_reply(*args, **kwargs)

    def _start_critique_task(self, *args, **kwargs):
        if self.bridge is not None:
            self.bridge.request_epoch += 1
        super()._start_critique_task(*args, **kwargs)

    def _on_critique_ready(self, raw_text):
        if getattr(self, "_closing", False):
            return
        source = "reminder" if "发现 GitHub 上发布了新版本" in raw_text else "greeting"
        if hasattr(self, "_reply_lock"):
            with self._reply_lock:
                for tag in self._reply_tags:
                    if tag[0] == raw_text:
                        source = tag[1]
                        self._reply_tags.remove(tag)
                        if len(tag) > 2 and tag[2] is not None and self.smart is not None and tag[2] != self.smart.generation:
                            return
                        break
        if hasattr(self.speaker, "set_source"):
            self.speaker.set_source(source)
        super()._on_critique_ready(raw_text)

    def tag_reply(self, text, source, generation=None):
        with self._reply_lock:
            self._reply_tags.append((text, source, generation))

    def bind_reply_sources(self):
        self._reply_lock, self._reply_tags = threading.Lock(), deque(maxlen=20)
        for name, source in (("generate_interactive_reply", "user"), ("generate_critique", "passive")):
            original_method = getattr(self.engine, name)
            signature = inspect.signature(original_method)

            def tagged(*args, method=original_method, kind=source, parameters=signature, **kwargs):
                generation = None
                if self.smart is not None and self.smart.cfg["enabled"]:
                    generation = self.smart.generation
                    text = self.smart.generate_engine(parameters.bind(*args, **kwargs).arguments, kind)
                else:
                    text = method(*args, **kwargs)
                # 原worker按本条实际返回文字贴标签；智能调用使用原方法并自行按任务贴标签。
                self.tag_reply(text, kind, generation)
                return text

            tagged.original_method = original_method
            setattr(self.engine, name, tagged)

    def toggle_tts(self):
        super().toggle_tts()
        self.config_mgr.save({"enable_tts": self.speaker.enabled})
        self.settings_win.chk_tts.setChecked(self.speaker.enabled)

    def toggle_auto(self):
        super().toggle_auto()
        self.config_mgr.save({"auto_mode": self.auto_mode})
        self.settings_win.chk_auto.setChecked(self.auto_mode)

    def quit(self):
        self._closing = True
        if self.desktop_control is not None:
            self.desktop_control.close()
        if self.screenshot_qa is not None:
            self.screenshot_qa.close()
        if self.features is not None:
            self.features.close()
        if self.smart is not None:
            self.smart.shutdown()
        if self.voice_input is not None:
            self.voice_input.shutdown()
        if self.bridge is not None:
            self.bridge.shutdown()
        self.speaker.shutdown()
        super().quit()
        # 原设置关闭事件收起窗口，明确退出使用 exit 保证事件循环结束。
        QApplication.instance().exit(0)


@dataclass
class OriginalRuntime:
    pet: object
    tray: DesktopPetTray
    window: ExtendedSettingsWindow
    controller: object
    bridge: OriginalCloudBridge

    def shutdown(self):
        self.controller._closing = True
        if self.controller.desktop_control is not None:
            self.controller.desktop_control.close()
        if self.controller.screenshot_qa is not None:
            self.controller.screenshot_qa.close()
        if self.controller.features is not None:
            self.controller.features.close()
        self.controller.smart.shutdown()
        self.controller.voice_input.shutdown()
        self.bridge.shutdown()
        self.controller.monitor_timer.stop()
        self.controller.hotkeys.stop()
        self.controller.speaker.shutdown()
        stream = getattr(self.controller, "_voice_stream", None)
        if stream is not None:
            stream.stop()
            stream.close()
            self.controller._voice_stream = None
        timer = getattr(self.controller, "voice_timeout_timer", None)
        if timer is not None:
            timer.stop()


def create_runtime(mode="dynamic", *, state_dir=None):
    if mode not in ("classic", "dynamic"):
        raise ValueError("请选择经典版或动态版")
    root = Path(state_dir or Path(__file__).parent)
    root.mkdir(parents=True, exist_ok=True)
    module = importlib.import_module("main_dynamic" if mode == "dynamic" else "main")
    if sys.platform == "win32":
        source = Path(__file__).parent
        command = [str(Path(sys.executable).with_name("pythonw.exe")), "-X", "utf8", str(source / "original_main.py"), "--mode", mode]
        if root.resolve() != source.resolve():
            command.extend(["--state-dir", str(root.resolve())])
        module.ConfigManager().autostart_command = subprocess.list2cmdline(command)
    module.GlobalHotkeyManager = GuiHotkeys
    base = module.DynamicPetController if mode == "dynamic" else module.DesktopPetController
    controller_class = type("ExtendedOriginalController", (_OriginalControllerExtensions, base), {})
    if mode == "dynamic":
        pet = module.DynamicDesktopPetWidget()
    else:
        pet = module.DesktopPetWidget(target_height=int(module.ConfigManager().get("target_height", 250)))
    tray = DesktopPetTray(icon_path=ICON_PATH)
    window = ExtendedSettingsWindow(is_dynamic=mode == "dynamic", store=SettingsStore(root / "companion_settings.json"))
    controller = controller_class(pet, tray, window)
    if mode == "dynamic":
        window.action_panel.play_requested.connect(lambda key, loop: pet.play_action(key, loop=loop))
        window.action_panel.random_settings_changed.connect(pet.configure_random_playback)
    controller.client_mode = mode
    controller.bind_reply_sources()
    controller.speaker = SmartSpeaker(controller.speaker, window.smart_panel.config, root)
    controller.voice_input = SpeechInputController(controller, window.speech_panel.store)
    controller.hotkeys.on_toggle_voice_input = controller.voice_input.request_toggle.emit
    window.settings_applied.connect(controller.voice_input.cancel)
    window.extension.settings_saved.connect(lambda _cfg: controller.voice_input.cancel())
    window.smart_panel.settings_saved.connect(lambda _cfg: controller.voice_input.cancel())
    bridge = OriginalCloudBridge(controller, window.extension, state_dir=root)
    controller.bridge = bridge
    controller.smart = SmartCompanion(controller, window.smart_panel, root)
    controller.features = FeatureRuntime(controller, window.features_panel, root)
    window.settings_applied.connect(lambda: controller.smart.apply(controller.smart.cfg))
    window.extension.settings_saved.connect(lambda _cfg: controller.smart.apply(controller.smart.cfg))
    # 保持原右键菜单原封不动；仅在原托盘末尾追加扩展快捷入口。
    menu = tray.contextMenu()
    before_exit = menu.actions()[-2]
    for text, callback in (("扩展功能", window.open_features), ("☁ 云端连接与模型", window.open_extension), ("向云端汇报状态", lambda: bridge.report(False))):
        action = QAction(text, menu)
        action.triggered.connect(callback)
        menu.insertAction(before_exit, action)
    runtime = OriginalRuntime(pet, tray, window, controller, bridge)
    from desktop_bindings import install_bindings
    from qa_menu import install_menu
    from qa_runtime import ScreenshotQA
    controller.screenshot_qa = ScreenshotQA(controller, root)
    from desktop_control.runtime import DesktopControl
    controller.desktop_control = DesktopControl(controller, window.desktop_panel, root)
    bridge.link.desktop = controller.desktop_control
    window.extension.settings_saved.connect(lambda _cfg: controller.desktop_control.stop('模型或连接设置已更新，任务已停止。'))
    window.settings_applied.connect(lambda: controller.desktop_control.stop('原设置已更新，任务已停止。'))
    install_menu(pet, controller)
    install_bindings(runtime, root)
    return runtime
