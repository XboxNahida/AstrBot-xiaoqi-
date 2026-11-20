import os
import sys
import ctypes

# Force UTF-8 on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import json
import re
import threading
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, Qt
from PyQt6.QtGui import QGuiApplication

from config_manager import ConfigManager
from log_manager import get_log_manager
from perception.window_tracker import get_foreground_window_info
from perception.screen_capture import capture_screen_jpeg
from perception.hotkey_listener import GlobalHotkeyManager
from brain.gemini_client import GeminiCritiqueEngine
from brain.rate_limiter import CritiqueRateLimiter
from tts.edge_speaker import EdgeSpeaker
from ui.pet_widget import DesktopPetWidget
from ui.tray_icon import DesktopPetTray
from ui.settings_window import SettingsWindow

from app_paths import BASE_DIR, ASSETS_DIR, ICON_PATH
from version import VERSION

class DesktopPetController(QObject):
    critique_ready = pyqtSignal(str)
    voice_toggle_requested = pyqtSignal()
    voice_result_ready = pyqtSignal(bool, str)

    def __init__(self, pet_widget: DesktopPetWidget, tray_icon: DesktopPetTray, settings_win: SettingsWindow):
        super().__init__()
        self.pet = pet_widget
        self.tray = tray_icon
        self.settings_win = settings_win

        self.config_mgr = ConfigManager()
        self.logger = get_log_manager()

        # Engines
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
        self._voice_stream = None
        self._voice_chunks = []
        self._voice_recording = False
        self._voice_transcribing = False
        self._voice_context = None

        # Connect signals
        self.critique_ready.connect(self._on_critique_ready)
        self.voice_toggle_requested.connect(self._toggle_voice_input)
        self.voice_result_ready.connect(self._on_voice_result)

        # Pet signals
        self.pet.open_settings.connect(self.show_settings)
        self.pet.request_critique.connect(self.trigger_manual_critique)
        self.pet.user_replied.connect(self.handle_user_reply)
        self.pet.toggle_tts.connect(self.toggle_tts)
        self.pet.toggle_auto.connect(self.toggle_auto)
        self.pet.quit_app.connect(self.quit)

        # Tray signals
        self.tray.open_settings.connect(self.show_settings)
        self.tray.open_update_dialog.connect(self.settings_win.open_update_dialog)
        self.tray.trigger_critique.connect(self.trigger_manual_critique)
        self.tray.reopen_bubble.connect(self.pet.reopen_last_bubble)
        self.tray.cycle_outfit.connect(self.pet.cycle_next_outfit)
        self.tray.toggle_pet_visibility.connect(self.toggle_visibility)
        self.tray.toggle_tts.connect(self.toggle_tts)
        self.tray.quit_requested.connect(self.quit)

        # Settings window signals
        self.settings_win.settings_applied.connect(self.reload_settings)
        self.settings_win.request_manual_critique.connect(self.trigger_manual_critique)

        # Hotkeys
        self.hotkeys = GlobalHotkeyManager(
            on_trigger_critique=self.trigger_manual_critique,
            on_toggle_mute=self.toggle_tts,
            on_switch_outfit=self.pet.cycle_next_outfit,
            on_toggle_voice_input=self.voice_toggle_requested.emit
        )
        self.voice_timeout_timer = QTimer(self)
        self.voice_timeout_timer.setSingleShot(True)
        self.voice_timeout_timer.timeout.connect(self._finish_voice_recording)
        self.hotkeys.start()

        # Active Window Monitor Timer (runs every 2.5s)
        self.monitor_timer = QTimer(self)
        self.monitor_timer.timeout.connect(self._check_desktop_state)
        self.monitor_timer.start(2500)

        # Startup Log & Greet
        pet_name = self.config_mgr.get("pet_name", "哈Gemi")
        mem_count = len(self.engine.context_memory.history)
        if mem_count > 0:
            greet_msg = f"{pet_name}已就位喵~ 已加载 {mem_count} 条前情记忆，随时按 Ctrl+F9 呼叫锐评吧！"
            self.logger.info(f"{pet_name} 启动就位！(版本: {VERSION}, 已加载 {mem_count} 条前情记忆) 双击托盘图标或右键桌宠可打开设置中心。")
        else:
            greet_msg = f"{pet_name}已就位！随时按 Ctrl+F9 接受本喵的锐评吧！"
            self.logger.info(f"{pet_name} 启动就位！(版本: {VERSION}) 双击托盘图标或右键桌宠可打开设置中心。")
        QTimer.singleShot(1000, lambda: self.critique_ready.emit(greet_msg))

        # Auto check update in background (delayed 4s after launch)
        if self.config_mgr.get("auto_check_update", True):
            QTimer.singleShot(4000, self._check_update_on_startup)

    def _check_update_on_startup(self):
        try:
            from update_checker import UpdateChecker
            self._startup_checker = UpdateChecker(current_version=VERSION, parent=self)
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
        """Called when settings are saved in settings_window."""
        self.config_mgr.load()
        cfg = self.config_mgr.config

        self.auto_mode = cfg.get("auto_mode", True)
        self.rate_limiter.cooldown_seconds = float(cfg.get("cooldown_seconds", 45))

        # Update TTS
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
        if hasattr(self.engine, "antigravity_helper"):
            self.engine.antigravity_helper.reload()

        # Update Pet size & appearance
        new_height = int(cfg.get("target_height", 250))
        self.pet.update_size(new_height)

        pet_name = cfg.get("pet_name", "哈Gemi")
        self.pet.bubble.title_label.setText(pet_name)
        self.tray.setToolTip(f"Gemini 赛博猫娘 · {pet_name}\n双击打开控制台与设置中心")

        self.logger.info(f"设置已重新加载，当前桌宠: {pet_name}，冷却: {self.rate_limiter.cooldown_seconds}s")

    def _check_desktop_state(self):
        if not self.auto_mode or self._is_analyzing or self._voice_recording or self._voice_transcribing:
            return

        info = get_foreground_window_info()
        # Ignore our own pet window, terminal, launcher
        if info.get("is_self", False):
            return

        scene = info["scene"]
        title = info["title"]
        proc = info["process"]

        if not title:
            return

        # Check user preferences for scenes
        if scene == "game" and not self.config_mgr.get("comment_on_games", True):
            return
        if scene == "video" and not self.config_mgr.get("comment_on_videos", True):
            return
        if scene == "work" and not self.config_mgr.get("comment_on_work", True):
            return

        if self.rate_limiter.should_trigger_auto(scene, title):
            self.logger.scene(f"自动捕获场景: [{scene}] 窗口: {title} ({proc})")
            self._start_critique_task(scene, title, proc, rect=info.get("rect"))

    def handle_user_reply(self, user_comment: str, window_info=None):
        if self._is_analyzing:
            return
        info = window_info or get_foreground_window_info()
        scene = info.get("scene", "general")
        title = info.get("title", "")
        proc = info.get("process", "")

        self.logger.info(f"主人在气泡回复互动: \"{user_comment}\"")
        self._start_user_interaction_task(scene, title, proc, user_comment, rect=info.get("rect"))

    def _show_voice_status(self, message: str):
        self.pet.show()
        self.pet.show_critique(message)

    def _toggle_voice_input(self):
        if self._voice_recording:
            self._finish_voice_recording()
            return
        if self._voice_transcribing:
            self._show_voice_status("喵呜，正在识别刚才的语音，请稍等一下~")
            return
        if self._is_analyzing:
            self._show_voice_status("我正在回复上一条消息，等一下再语音输入吧~")
            return
        if not self.config_mgr.get("tencent_secret_id", "").strip() or not self.config_mgr.get("tencent_secret_key", "").strip():
            self._show_voice_status("请先在设置中心填写腾讯云 SecretId 和 SecretKey，再按 Ctrl+F1 录音喵~")
            self.show_settings()
            return

        try:
            import sounddevice as sd

            self._voice_context = get_foreground_window_info()
            if self._voice_context.get("is_self", False):
                self._voice_context["scene"] = "general"
                self._voice_context["rect"] = None
            self._voice_chunks = []
            self._voice_recording = True

            def collect_audio(indata, frames, time_info, status):
                self._voice_chunks.append(indata.copy().tobytes())

            self._voice_stream = sd.InputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                callback=collect_audio
            )
            self._voice_stream.start()
            self.voice_timeout_timer.start(55000)
            self._show_voice_status("🎙 正在录音… 再按 Ctrl+F1 结束并识别（最长 55 秒）")
            self.logger.info("语音输入开始录音。")
        except Exception as exc:
            self._voice_recording = False
            if self._voice_stream is not None:
                try:
                    self._voice_stream.close()
                except Exception:
                    pass
                self._voice_stream = None
            self._show_voice_status(f"无法启动麦克风录音：{exc}")
            self.logger.error(f"无法启动麦克风录音: {exc}")

    def _finish_voice_recording(self):
        if not self._voice_recording:
            return
        self.voice_timeout_timer.stop()
        self._voice_recording = False

        stream = self._voice_stream
        self._voice_stream = None
        try:
            if stream is not None:
                stream.stop()
                stream.close()
        except Exception as exc:
            self.logger.error(f"停止麦克风录音时出错: {exc}")

        pcm_data = b"".join(self._voice_chunks)
        self._voice_chunks = []
        if not pcm_data:
            self._show_voice_status("没有录到语音，请检查麦克风后重试喵~")
            return

        self._voice_transcribing = True
        self._show_voice_status("录音结束，正在连接腾讯云识别…")
        secret_id = self.config_mgr.get("tencent_secret_id", "").strip()
        secret_key = self.config_mgr.get("tencent_secret_key", "").strip()

        def recognize():
            try:
                from perception.tencent_asr import transcribe_pcm
                text = transcribe_pcm(pcm_data, secret_id, secret_key)
                self.voice_result_ready.emit(True, text)
            except Exception as exc:
                self.voice_result_ready.emit(False, str(exc))

        threading.Thread(target=recognize, daemon=True).start()

    def _on_voice_result(self, success: bool, text: str):
        self._voice_transcribing = False
        if not success:
            self._voice_context = None
            self._show_voice_status(f"语音识别失败：{text}")
            self.logger.error(f"语音识别失败: {text}")
            return

        self.logger.info(f"腾讯云语音识别结果: {text}")
        self._show_voice_status(f"听到啦：{text}")
        context = self._voice_context
        self._voice_context = None
        self.handle_user_reply(text, context)

    def _start_user_interaction_task(self, scene: str, title: str, proc: str, user_comment: str, rect=None):
        self._is_analyzing = True
        def worker():
            try:
                max_w = 1440 if scene == "video" else 1280
                cap = capture_screen_jpeg(max_width=max_w, quality=80, crop_rect=rect)
                image_b64 = cap[1] if cap else None

                text = self.engine.generate_interactive_reply(scene, title, proc, user_comment, image_b64)
                self.critique_ready.emit(text)
            except Exception as e:
                self.logger.error(f"互动回复任务出错: {e}")
            finally:
                self._is_analyzing = False

        threading.Thread(target=worker, daemon=True).start()

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
        if not force:
            self.rate_limiter.record_critique(scene, title)

        def worker():
            t_start = threading.current_thread().name
            try:
                max_w = 1440 if scene == "video" else 1280
                cap = capture_screen_jpeg(max_width=max_w, quality=80, crop_rect=rect)
                image_b64 = cap[1] if cap else None

                # Generate via Gemini
                text = self.engine.generate_critique(scene, title, proc, image_b64)
                self.critique_ready.emit(text)
            except Exception as e:
                self.logger.error(f"分析任务出错: {e}")
            finally:
                self._is_analyzing = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_critique_ready(self, raw_text: str):
        # Parse emotion tag [shocked], [curious], [relieved], [confident]
        emotion = "confident"
        clean_text = raw_text

        m = re.match(r'^\s*\[([a-zA-Z]+)\]\s*(.*)', raw_text, re.DOTALL)
        if m:
            emotion = m.group(1).lower()
            clean_text = m.group(2).strip()

        pet_name = self.config_mgr.get("pet_name", "哈Gemi")
        self.logger.critique(f"[{emotion.upper()}] {clean_text}")

        # 1. Dynamically switch avatar expression based on emotion
        self.pet.set_emotion(emotion)

        # 2. Show bubble and speak
        self.pet.show()
        self.pet.raise_()
        self.pet.show_critique(clean_text)
        self.speaker.speak_async(clean_text)

    def toggle_tts(self):
        status = self.speaker.toggle()
        msg = "语音播报已开启喵~" if status else "语音播报已静音喵！"
        self.critique_ready.emit(msg)
        self.logger.info(msg)

    def toggle_auto(self):
        self.auto_mode = not self.auto_mode
        msg = "自动感知已开启喵！" if self.auto_mode else "自动感知已休眠（按 Ctrl+F9 手动唤醒）！"
        self.critique_ready.emit(msg)
        self.logger.info(msg)

    def toggle_visibility(self):
        if self.pet.isVisible():
            self.pet.hide()
            self.pet.bubble.hide()
            self.pet.interaction_dialog.hide()
        else:
            self.pet.show()

    def quit(self):
        self.logger.info("退出程序")
        if getattr(self, "voice_timeout_timer", None):
            self.voice_timeout_timer.stop()
        if self._voice_stream is not None:
            try:
                self._voice_stream.stop()
                self._voice_stream.close()
            except Exception:
                pass
            self._voice_stream = None
        self._voice_recording = False
        if hasattr(self, 'monitor_timer') and self.monitor_timer.isActive():
            self.monitor_timer.stop()
        self.hotkeys.stop()
        self.pet.close()
        self.settings_win.close()
        QApplication.instance().quit()

def main():
    # Single Instance Guard (Win32 Mutex on Windows / File Lock on macOS & Linux)
    mutex = None
    lock_file = None
    if sys.platform == "win32":
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "HaGemi_SingleInstance_Mutex_9911")
        last_err = ctypes.windll.kernel32.GetLastError()
        if last_err == ERROR_ALREADY_EXISTS:
            print("[Main] 哈Gemi 已在后台运行中，禁止重复启动多实例。", flush=True)
            sys.exit(0)
    else:
        import fcntl
        import tempfile
        lock_path = os.path.join(tempfile.gettempdir(), "hagemi_single_instance.lock")
        try:
            lock_file = open(lock_path, "w")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            print("[Main] 哈Gemi 已在后台运行中，禁止重复启动多实例。", flush=True)
            sys.exit(0)

    # Fix fractional DPI scaling rasterization issues on layered transparent windows
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Pet & Tray & Settings
    cfg_mgr = ConfigManager()
    init_height = int(cfg_mgr.get("target_height", 250))
    pet = DesktopPetWidget(target_height=init_height)
    tray = DesktopPetTray(icon_path=ICON_PATH)
    settings_win = SettingsWindow()

    pet.show()

    controller = DesktopPetController(pet, tray, settings_win)

    if "--auto-close" in sys.argv:
        QTimer.singleShot(2500, app.quit)

    exit_code = app.exec()

    # Clean up mutex/lock and guarantee immediate process termination without zombie threads
    if sys.platform == "win32" and mutex:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(mutex)
    elif lock_file:
        try:
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass

    os._exit(exit_code)

if __name__ == "__main__":
    main()
