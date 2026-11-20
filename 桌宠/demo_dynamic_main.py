"""
================================================================================
🐾 哈Gemi · 60FPS 丝滑动态连续帧桌宠整合演示版 (Dynamic Interactive Preview)
================================================================================
本脚本为【独立演示版】，结合了：
1. 哈Gemi 核心大脑：Gemini 3.8/4 原生推理、Antigravity CLI 探针、自动截屏感知、全套记忆系统
2. 超自然语音与对话：Edge-TTS / 自定义 OpenAI 兼容语音、吐槽气泡、打字机动画、图文互动卡片
3. 60 FPS 丝滑动态骨骼与连续帧动作库：
   - 待机呼吸休闲 (120帧高刷微动呼吸)
   - 工作状态-思考冒泡 (AI 分析思考时挠头思考冒泡)
   - 点击回应-开心跃动 (锐评完成/摸头跳跃喷发爱心粒子)
   - 被鼠标拖拽悬空反馈 (拖拽时 60fps 惯性钟摆悬空挣扎)
   - 写代码 (代码/工作场景切换)
   - 被吓一跳 (震惊表情)
================================================================================
"""

import os
import sys
import io
import time
import math
import random
import re
import threading

# Ensure proper encoding on Windows
if sys.stdout:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr:
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QMenu
from PyQt6.QtCore import Qt, QTimer, QRect, QPoint, pyqtSignal, QObject
from PyQt6.QtGui import QPainter, QPixmap, QColor, QFont, QCursor, QTransform, QAction

# Import all core Ha-Gemi components
from app_paths import BASE_DIR, ASSETS_DIR, ICON_PATH
from config_manager import ConfigManager
from log_manager import get_log_manager
from brain.gemini_client import GeminiCritiqueEngine
from brain.rate_limiter import CritiqueRateLimiter
from tts.edge_speaker import EdgeSpeaker
from perception.window_tracker import get_foreground_window_info
from perception.screen_capture import capture_screen_jpeg
from perception.hotkey_listener import GlobalHotkeyManager
from ui.bubble_widget import SpeechBubble
from ui.interaction_dialog import InteractionDialog
from ui.tray_icon import DesktopPetTray
from ui.settings_window import SettingsWindow
from version import VERSION

DYNAMIC_ACTIONS_DIR = os.path.join(ASSETS_DIR, "dynamic_actions")

ACTION_DEFS = {
    "idle": {"file": "待机呼吸休闲", "loop": True, "title": "待机呼吸"},
    "thinking": {"file": "工作状态-思考冒泡", "loop": True, "title": "思考冒泡"},
    "happy": {"file": "点击回应-开心跃动", "loop": False, "title": "开心跃动"},
    "dangling": {"file": "被鼠标拖拽悬空反馈", "loop": True, "title": "悬空挣扎"},
    "coding": {"file": "写代码", "loop": True, "title": "写代码"},
    "shocked": {"file": "被吓一跳", "loop": False, "title": "被吓一跳"}
}

class FloatingParticle:
    def __init__(self, x, y):
        self.x = x + random.uniform(-20, 20)
        self.y = y + random.uniform(-10, 10)
        self.vx = random.uniform(-1.2, 1.2)
        self.vy = random.uniform(-3.5, -1.8)
        self.alpha = 255.0
        self.size = random.uniform(16, 26)
        self.char = random.choice(["♥", "✨", "🌸", "⭐", "✦"])
        self.color = random.choice([
            QColor(255, 105, 180),
            QColor(255, 182, 193),
            QColor(255, 215, 0),
            QColor(186, 85, 211),
            QColor(147, 112, 219)
        ])

    def update(self):
        self.x += self.vx
        self.y += self.vy
        self.alpha -= 5.5
        return self.alpha > 0

class DynamicDesktopPetWidget(QWidget):
    open_settings = pyqtSignal()
    request_critique = pyqtSignal()
    user_replied = pyqtSignal(str)
    toggle_tts = pyqtSignal()
    toggle_auto = pyqtSignal()
    quit_app = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_mgr = ConfigManager()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.scale_factor = 0.52
        self.playback_speed_multiplier = 1.25  # ~15 FPS smooth playback

        # Action cache
        self.actions = {}
        self._load_actions()

        self.current_action_key = "idle"
        self.current_frame = 0
        self.frame_accumulator = 0.0
        self.last_update_time = time.perf_counter()

        # 60 FPS Physics
        self.is_dragging = False
        self._drag_pos = QPoint()
        self.last_global_pos = QPoint()
        self.velocity_x = 0.0
        self.tilt_angle = 0.0
        self.target_tilt = 0.0
        self.scale_y_ease = 1.0
        self.scale_x_ease = 1.0
        self.particles = []
        self.time_counter = 0.0

        # Sub-widgets: Bubble & Interaction Dialog
        self.bubble = SpeechBubble()
        self.bubble.clicked.connect(self.reopen_last_bubble)

        self.interaction_dialog = InteractionDialog()
        self.interaction_dialog.user_replied.connect(self.user_replied.emit)

        # Place initially at bottom-right
        self._apply_size()
        self._place_at_bottom_right()

        # 60 FPS Render & Physics Loop
        self.physics_timer = QTimer(self)
        self.physics_timer.timeout.connect(self._step_60fps)
        self.physics_timer.start(16)

    def _load_actions(self):
        for k, info in ACTION_DEFS.items():
            jp = os.path.join(DYNAMIC_ACTIONS_DIR, f"{info['file']}.json")
            wp = os.path.join(DYNAMIC_ACTIONS_DIR, f"{info['file']}.webp")
            if os.path.exists(jp) and os.path.exists(wp):
                with open(jp, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                pix = QPixmap(wp)
                self.actions[k] = {
                    "meta": meta,
                    "pixmap": pix,
                    "frame_w": meta["FrameWidth"],
                    "frame_h": meta["FrameHeight"],
                    "columns": meta["Columns"],
                    "rows": meta["Rows"],
                    "count": meta["FrameCount"],
                    "base_fps": meta.get("SampleFps", 12),
                    "loop": info["loop"],
                    "title": info["title"]
                }
        print(f"[DynamicPet] Loaded {len(self.actions)} dynamic animation action packs.")

    def _apply_size(self):
        act = self.actions.get(self.current_action_key)
        if act:
            w = int(act["frame_w"] * self.scale_factor)
            h = int(act["frame_h"] * self.scale_factor)
            self.resize(w + 30, h + 30)
        else:
            self.resize(240, 240)

    def _place_at_bottom_right(self):
        screen = self.screen() or QApplication.primaryScreen()
        geom = screen.availableGeometry() if screen else None
        if geom:
            x = geom.x() + geom.width() - self.width() - 80
            y = geom.y() + geom.height() - self.height() - 110
        else:
            x, y = 1400, 700
        self.move(x, y)

    def set_action(self, key: str):
        if key in self.actions and self.current_action_key != key:
            self.current_action_key = key
            self.current_frame = 0
            self.frame_accumulator = 0.0
            self._apply_size()
            self.update()

    def set_thinking(self, is_thinking: bool):
        """Called when AI begins/ends reasoning."""
        if is_thinking:
            self.set_action("thinking")
        else:
            if self.current_action_key == "thinking":
                self.set_action("idle")

    def set_emotion(self, emotion: str):
        em = emotion.lower()
        if em == "shocked":
            self.set_action("shocked")
        elif em == "curious":
            self.set_action("thinking")
        elif em in ["confident", "relieved"]:
            self.trigger_happy_reaction()
        else:
            self.set_action("idle")

    def cycle_next_outfit(self):
        keys = list(ACTION_DEFS.keys())
        try:
            curr_idx = keys.index(self.current_action_key)
            next_idx = (curr_idx + 1) % len(keys)
        except ValueError:
            next_idx = 0
        target = keys[next_idx]
        self.set_action(target)
        title = ACTION_DEFS[target]["title"]
        self.show_critique(f"已切换动作：【{title}】喵~")

    def update_size(self, new_height: int):
        self.scale_factor = max(0.35, min(0.85, new_height / 380.0))
        self._apply_size()
        self.update()

    def trigger_happy_reaction(self, x=None, y=None):
        if x is None:
            x = self.width() / 2.0
        if y is None:
            y = self.height() / 3.0
        for _ in range(7):
            self.particles.append(FloatingParticle(x, y))
        self.set_action("happy")

    def show_critique(self, text: str):
        self.raise_()
        bw, bh = self.bubble.calculate_size(text)

        # Position bubble nicely above the pet
        bx = self.x() - bw + 60
        by = self.y() - bh - 8

        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if bx < geom.x() + 10:
                bx = self.x() + self.width() - 20
            if by < geom.y() + 10:
                by = self.y() + self.height() + 10

        self.bubble.move(bx, by)
        self.bubble.show_text(text)
        self.bubble.raise_()

    def reopen_last_bubble(self):
        last_text = self.bubble._full_text
        if not last_text or not last_text.strip():
            self.show_critique("刚才还没有锐评记录喵~ 点击我或按 Ctrl+F9 呼叫锐评吧！")
            return
        self.bubble.hide()
        self.interaction_dialog.open_for_critique(last_text, self.pos())

    def _step_60fps(self):
        now = time.perf_counter()
        dt = min(0.05, now - self.last_update_time)
        self.last_update_time = now
        self.time_counter += dt

        act = self.actions.get(self.current_action_key)
        if act:
            target_fps = act["base_fps"] * self.playback_speed_multiplier
            frame_duration = 1.0 / max(1.0, target_fps)
            self.frame_accumulator += dt

            while self.frame_accumulator >= frame_duration:
                self.frame_accumulator -= frame_duration
                self.current_frame += 1
                if self.current_frame >= act["count"]:
                    if act["loop"]:
                        self.current_frame = 0
                    else:
                        self.set_action("idle")
                        return

        # 60 FPS Physics (Breathing & Swing)
        if not self.is_dragging:
            breath = math.sin(self.time_counter * 3.5) * 0.018
            self.scale_y_ease = 1.0 + breath
            self.scale_x_ease = 1.0 - breath * 0.6
            self.target_tilt = math.sin(self.time_counter * 1.8) * 1.2
        else:
            self.scale_y_ease = 1.06
            self.scale_x_ease = 0.94
            self.target_tilt = max(-22.0, min(22.0, -self.velocity_x * 0.8))

        self.tilt_angle += (self.target_tilt - self.tilt_angle) * 0.22

        if self.particles:
            self.particles = [p for p in self.particles if p.update()]

        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.last_global_pos = event.globalPosition().toPoint()
            self.velocity_x = 0.0
            self.set_action("dangling")
            event.accept()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.cycle_next_outfit()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            curr_pos = event.globalPosition().toPoint()
            self.velocity_x = (curr_pos.x() - self.last_global_pos.x())
            self.last_global_pos = curr_pos
            self.move(curr_pos - self._drag_pos)

            # Move bubble along if visible
            if self.bubble.isVisible():
                self.bubble.move(self.x() - self.bubble.width() + 60, self.y() - self.bubble.height() - 8)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_dragging = False
            self.velocity_x = 0.0
            self.set_action("idle")
            event.accept()

    def mouseDoubleClickEvent(self, event):
        self.trigger_happy_reaction(event.position().x(), event.position().y())
        event.accept()

    def wheelEvent(self, event):
        self.cycle_next_outfit()
        event.accept()

    def _show_context_menu(self, global_pos: QPoint):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2b2b36;
                color: #e0e0e0;
                border: 1px solid #4a4a5a;
                border-radius: 8px;
                padding: 4px;
                font-family: 'PingFang SC', 'Microsoft YaHei', sans-serif;
                font-size: 13px;
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ff4e88;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #4a4a5a;
                margin: 4px 8px;
            }
        """)

        critique_act = QAction("💬 呼叫锐评 (Ctrl+F9)", self)
        critique_act.triggered.connect(self.request_critique.emit)
        menu.addAction(critique_act)

        switch_act = QAction("🎭 切换动作/神态", self)
        switch_act.triggered.connect(self.cycle_next_outfit)
        menu.addAction(switch_act)

        menu.addSeparator()

        settings_act = QAction("⚙️ 设置中心", self)
        settings_act.triggered.connect(self.open_settings.emit)
        menu.addAction(settings_act)

        tts_act = QAction("🔊 切换语音开关", self)
        tts_act.triggered.connect(self.toggle_tts.emit)
        menu.addAction(tts_act)

        auto_act = QAction("👁️ 自动感知开关", self)
        auto_act.triggered.connect(self.toggle_auto.emit)
        menu.addAction(auto_act)

        menu.addSeparator()

        quit_act = QAction("❌ 退出程序", self)
        quit_act.triggered.connect(self.quit_app.emit)
        menu.addAction(quit_act)

        menu.exec(global_pos)

    def paintEvent(self, event):
        act = self.actions.get(self.current_action_key)
        if not act or act["pixmap"].isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        cx = self.width() / 2.0
        cy = self.height() - 15.0

        transform = QTransform()
        transform.translate(cx, cy)
        transform.rotate(self.tilt_angle)
        transform.scale(self.scale_x_ease, self.scale_y_ease)
        transform.translate(-cx, -cy)
        painter.setTransform(transform)

        idx = self.current_frame
        col = idx % act["columns"]
        row = idx // act["columns"]
        src_rect = QRect(
            col * act["frame_w"],
            row * act["frame_h"],
            act["frame_w"],
            act["frame_h"]
        )
        dst_w = int(act["frame_w"] * self.scale_factor)
        dst_h = int(act["frame_h"] * self.scale_factor)
        dst_rect = QRect(15, 15, dst_w, dst_h)
        painter.drawPixmap(dst_rect, act["pixmap"], src_rect)

        painter.resetTransform()

        for p in self.particles:
            color = QColor(p.color)
            color.setAlpha(max(0, min(255, int(p.alpha))))
            font = QFont("Segoe UI Emoji", int(p.size))
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(int(p.x), int(p.y), p.char)

class DynamicPetController(QObject):
    critique_ready = pyqtSignal(str)

    def __init__(self, pet_widget: DynamicDesktopPetWidget, tray: DesktopPetTray, settings_win: SettingsWindow):
        super().__init__()
        self.pet = pet_widget
        self.tray = tray
        self.settings_win = settings_win

        self.config_mgr = ConfigManager()
        self.logger = get_log_manager()

        self.engine = GeminiCritiqueEngine()
        self.rate_limiter = CritiqueRateLimiter(cooldown_seconds=self.config_mgr.get("cooldown_seconds", 45))
        self.speaker = EdgeSpeaker(
            voice=self.config_mgr.get("tts_voice", "zh-TW-HsiaoChenNeural"),
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
        self.tray.trigger_critique.connect(self.trigger_manual_critique)
        self.tray.reopen_bubble.connect(self.pet.reopen_last_bubble)
        self.tray.cycle_outfit.connect(self.pet.cycle_next_outfit)
        self.tray.toggle_pet_visibility.connect(self.toggle_visibility)
        self.tray.toggle_tts.connect(self.toggle_tts)
        self.tray.quit_requested.connect(self.quit)

        self.settings_win.settings_applied.connect(self.reload_settings)
        self.settings_win.request_manual_critique.connect(self.trigger_manual_critique)

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

        # Greet
        pet_name = self.config_mgr.get("pet_name", "哈Gemi")
        greet_msg = f"{pet_name} 动态版就绪喵！现在我是活蹦乱跳的啦~ 随时按 Ctrl+F9 呼叫锐评吧！"
        self.logger.info(f"{pet_name} 动态版就位！(版本: {VERSION})")
        QTimer.singleShot(1000, lambda: self.critique_ready.emit(greet_msg))

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
            voice=cfg.get("tts_voice", "zh-TW-HsiaoChenNeural"),
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
        # Immediately enter Thinking animation!
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
                self.critique_ready.emit("[shocked] 喵呜，网络走神了喵…")
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
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = DynamicDesktopPetWidget()
    pet.show()
    pet.raise_()

    tray = DesktopPetTray(icon_path=ICON_PATH)
    tray.show()

    settings_win = SettingsWindow()
    controller = DynamicPetController(pet, tray, settings_win)

    print(f"[SUCCESS] 哈Gemi 动态版 (v{VERSION}) 已就绪！")
    if "--auto-close" in sys.argv:
        QTimer.singleShot(3000, app.quit)

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
