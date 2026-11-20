"""
================================================================================
🐾 哈Gemi · 动态版独立主程序 (Ha-Gemi Dynamic Edition)
================================================================================
版本: v1.5.0-dynamic (与经典静态立绘版完全独立并存)
特性:
1. 60 FPS 丝滑微动物理循环 (自然呼吸、悬空惯性钟摆)
2. 100 套多动作生态支持 (待机、思考冒泡、开心跃动、悬空扑腾、写代码、被吓一跳)
3. 精准对位微型漫画对话气泡 (DynamicSpeechBubble，紧凑排版，小尾巴精准对准猫耳)
4. 深度联动 Gemini 3.8/4 原生推理与 Antigravity CLI
5. 独立互斥体 (可与静态版同时开启对比，互不冲突)
================================================================================
"""

import os
import sys
import re
import threading
import ctypes

# Force UTF-8 on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QGuiApplication

from app_paths import BASE_DIR, ASSETS_DIR, ICON_PATH
from config_manager import ConfigManager
from log_manager import get_log_manager
from brain.gemini_client import GeminiCritiqueEngine
from brain.rate_limiter import CritiqueRateLimiter
from tts.edge_speaker import EdgeSpeaker
from perception.window_tracker import get_foreground_window_info
from perception.screen_capture import capture_screen_jpeg
from perception.hotkey_listener import GlobalHotkeyManager

from ui.dynamic_pet_widget import DynamicDesktopPetWidget
from ui.tray_icon import DesktopPetTray
from ui.settings_window import SettingsWindow
from version import VERSION

DYNAMIC_VERSION = f"{VERSION}-dynamic"

class DynamicPetController(QObject):
    critique_ready = pyqtSignal(str)

    def __init__(self, pet_widget: DynamicDesktopPetWidget, tray: DesktopPetTray, settings_win: SettingsWindow):
        super().__init__()
        self.pet = pet_widget
        self.tray = tray
        self.settings_win = settings_win

        self.config_mgr = ConfigManager()
        self.logger = get_log_manager()

        # Brain & TTS Engines
        self.engine = GeminiCritiqueEngine()
        self.rate_limiter = CritiqueRateLimiter(cooldown_seconds=self.config_mgr.get("cooldown_seconds", 45))
        self.speaker = EdgeSpeaker(
            voice=self.config_mgr.get("tts_voice", "zh-CN-XiaoyiNeural"),
            rate=self.config_mgr.get("tts_rate", "+10%"),
            pitch=self.config_mgr.get("tts_pitch", "+10Hz"),
            enabled=self.config_mgr.get("enable_tts", True),
            engine_type=self.config_mgr.get("tts_engine", "edge_tts"),
            api_url=self.config_mgr.get("tts_api_url", ""),
            api_key=self.config_mgr.get("tts_api_key", ""),
            model=self.config_mgr.get("tts_model", ""),
            gemini_api_key=self.config_mgr.get("api_key", ""),
            gemini_voice=self.config_mgr.get("gemini_tts_voice", "Aoede"),
            gemini_model=self.config_mgr.get("gemini_tts_model", "gemini-3.8-flash-tts")
        )

        self.auto_mode = self.config_mgr.get("auto_mode", True)
        self._is_analyzing = False

        # Connect signals
        self.critique_ready.connect(self._on_critique_ready)

        self.pet.open_settings.connect(self.show_settings)
        self.pet.request_critique.connect(self.trigger_manual_critique)
        self.pet.user_replied.connect(self.handle_user_reply)
        self.pet.toggle_tts.connect(self.toggle_tts)
        self.pet.toggle_auto.connect(self.toggle_auto)
        self.pet.quit_app.connect(self.quit)

        self.tray.open_settings.connect(self.show_settings)
        self.tray.open_update_dialog.connect(self.settings_win.open_update_dialog)
        self.tray.trigger_critique.connect(self.trigger_manual_critique)
        self.tray.reopen_bubble.connect(self.pet.reopen_last_bubble)
        self.tray.cycle_outfit.connect(self.pet.cycle_next_outfit)
        self.tray.toggle_pet_visibility.connect(self.toggle_visibility)
        self.tray.toggle_tts.connect(self.toggle_tts)
        self.tray.quit_requested.connect(self.quit)

        self.settings_win.settings_applied.connect(self.reload_settings)
        self.settings_win.request_manual_critique.connect(self.trigger_manual_critique)

        # Global Hotkeys
        self.hotkeys = GlobalHotkeyManager(
            on_trigger_critique=self.trigger_manual_critique,
            on_toggle_mute=self.toggle_tts,
            on_switch_outfit=self.pet.cycle_next_outfit,
            on_toggle_voice_input=lambda: None
        )
        self.hotkeys.start()

        # Desktop perception timer (2.5s)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._check_desktop_state)
        self.monitor_timer.start(2500)

        # Greeting
        pet_name = self.config_mgr.get("pet_name", "哈Gemi")
        greet_msg = f"{pet_name} 动态版就绪喵！现在我是活蹦乱跳的啦~ 随时按 Ctrl+F9 呼叫锐评吧！"
        self.logger.info(f"{pet_name} 动态独立版启动就绪！(版本: {DYNAMIC_VERSION})")
        QTimer.singleShot(1000, lambda: self.critique_ready.emit(greet_msg))

        # Auto check update in background (delayed 4s after launch)
        if self.config_mgr.get("auto_check_update", True):
            QTimer.singleShot(4000, self._check_update_on_startup)

    def _check_update_on_startup(self):
        try:
            from update_checker import UpdateChecker
            self._startup_checker = UpdateChecker(current_version=DYNAMIC_VERSION, parent=self)
            def on_finished(data, is_newer, err):
                if is_newer and not err:
                    new_tag = data.get("tag_name", "")
                    self.logger.info(f"✨ 发现新版本 {new_tag}！")
                    msg = f"[HAPPY] 盯——主人！我发现 GitHub 上发布了新版本 {new_tag} 喵！快去托盘或设置中心看看更新公告吧~ ✨"
                    self.critique_ready.emit(msg)
            self._startup_checker.check_finished.connect(on_finished)
            self._startup_checker.check_async()
        except Exception as e:
            self.logger.warning(f"后台检查更新出错: {e}")

    def show_settings(self):
        self.settings_win.show()
        self.settings_win.raise_()
        self.settings_win.activateWindow()

    def reload_settings(self):
        self.config_mgr.load()
        cfg = self.config_mgr.config
        self.auto_mode = cfg.get("auto_mode", True)
        self.rate_limiter.cooldown_seconds = float(cfg.get("cooldown_seconds", 45))

        self.speaker.update_config(
            voice=cfg.get("tts_voice", "zh-CN-XiaoyiNeural"),
            rate=cfg.get("tts_rate", "+10%"),
            pitch=cfg.get("tts_pitch", "+10Hz"),
            enabled=cfg.get("enable_tts", True),
            engine_type=cfg.get("tts_engine", "edge_tts"),
            api_url=cfg.get("tts_api_url", ""),
            api_key=cfg.get("tts_api_key", ""),
            model=cfg.get("tts_model", ""),
            gemini_api_key=cfg.get("api_key", ""),
            gemini_voice=cfg.get("gemini_tts_voice", "Aoede"),
            gemini_model=cfg.get("gemini_tts_model", "gemini-3.8-flash-tts")
        )
        new_height = int(cfg.get("target_height", 250))
        self.pet.update_size(new_height)
        new_fps = int(cfg.get("dynamic_fps", 15))
        self.pet.set_dynamic_fps(new_fps)

    def _check_desktop_state(self):
        if not self.auto_mode or self._is_analyzing:
            return
        info = get_foreground_window_info()
        if info.get("is_self", False):
            return

        scene = info["scene"]
        title = info["title"]
        proc = info["process"]
        if not title:
            return

        if self.rate_limiter.should_trigger_auto(scene, title):
            self.logger.scene(f"自动捕获场景: [{scene}] 窗口: {title} ({proc})")
            self._start_critique_task(scene, title, proc, rect=info.get("rect"))

    def trigger_manual_critique(self):
        if self._is_analyzing:
            return
        info = get_foreground_window_info()
        if info.get("is_self", False):
            info["scene"] = "general"
            info["rect"] = None
        self.logger.info(f"手动快捷键召唤锐评: {info['title']}")
        self._start_critique_task(info["scene"], info["title"], info["process"], rect=info.get("rect"), force=True)

    def _start_critique_task(self, scene: str, title: str, proc: str, rect=None, force: bool = False):
        self._is_analyzing = True
        # Immediately enter Thinking animation
        self.pet.set_thinking(True)
        if not force:
            self.rate_limiter.record_critique(scene, title)

        def worker():
            try:
                max_w = 1440 if scene == "video" else 1280
                cap = capture_screen_jpeg(max_width=max_w, quality=80, crop_rect=rect)
                image_b64 = cap[1] if cap else None

                text = self.engine.generate_critique(scene, title, proc, image_b64)
                self.critique_ready.emit(text)
            except Exception as e:
                self.logger.error(f"分析出错: {e}")
                self.critique_ready.emit("[shocked] 喵呜，网络开小差了喵…")
            finally:
                self._is_analyzing = False

        threading.Thread(target=worker, daemon=True).start()

    def handle_user_reply(self, user_comment: str):
        if self._is_analyzing:
            return
        self._is_analyzing = True
        self.pet.set_thinking(True)
        info = get_foreground_window_info()
        self.logger.info(f"主人回复互动: \"{user_comment}\"")

        def worker():
            try:
                text = self.engine.generate_interactive_reply(
                    info.get("scene", "general"),
                    info.get("title", ""),
                    info.get("process", ""),
                    user_comment
                )
                self.critique_ready.emit(text)
            except Exception as e:
                self.logger.error(f"互动出错: {e}")
            finally:
                self._is_analyzing = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_critique_ready(self, raw_text: str):
        self.pet.set_thinking(False)
        emotion = "confident"
        clean_text = raw_text

        m = re.match(r'^\s*\[([a-zA-Z]+)\]\s*(.*)', raw_text, re.DOTALL)
        if m:
            emotion = m.group(1).lower()
            clean_text = m.group(2).strip()

        self.logger.critique(f"[{emotion.upper()}] {clean_text}")

        # Trigger reaction & sound
        self.pet.set_emotion(emotion)
        self.pet.show()
        self.pet.raise_()
        self.pet.show_critique(clean_text)
        self.speaker.speak_async(clean_text)

    def toggle_tts(self):
        status = self.speaker.toggle()
        msg = "语音播报已开启喵~" if status else "语音播报已静音喵！"
        self.critique_ready.emit(msg)

    def toggle_auto(self):
        self.auto_mode = not self.auto_mode
        msg = "自动感知已开启喵！" if self.auto_mode else "自动感知已休眠（按 Ctrl+F9 手动唤醒）！"
        self.critique_ready.emit(msg)

    def toggle_visibility(self):
        if self.pet.isVisible():
            self.pet.hide()
            self.pet.bubble.hide()
            self.pet.interaction_dialog.hide()
        else:
            self.pet.show()

    def quit(self):
        self.logger.info("退出动态版哈Gemi")
        try:
            self.hotkeys.stop()
        except:
            pass
        QApplication.quit()

def main():
    # Windows independent single instance mutex (distinct from classic static edition)
    mutex = None
    if sys.platform == "win32":
        ERROR_ALREADY_EXISTS = 183
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "HaGemi_DynamicEdition_Mutex_9911")
        last_err = ctypes.windll.kernel32.GetLastError()
        if last_err == ERROR_ALREADY_EXISTS:
            print("[Main] 哈Gemi 动态版已在后台运行中，禁止重复启动。", flush=True)
            sys.exit(0)

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = DynamicDesktopPetWidget()
    pet.show()
    pet.raise_()

    tray = DesktopPetTray(icon_path=ICON_PATH)
    tray.show()

    settings_win = SettingsWindow(is_dynamic=True)
    controller = DynamicPetController(pet, tray, settings_win)

    print(f"[SUCCESS] 哈Gemi 动态独立版 ({DYNAMIC_VERSION}) 启动就位！")
    if "--auto-close" in sys.argv:
        QTimer.singleShot(2500, app.quit)

    exit_code = app.exec()

    if sys.platform == "win32" and mutex:
        ctypes.windll.kernel32.CloseHandle(mutex)

    os._exit(exit_code)

if __name__ == "__main__":
    main()
