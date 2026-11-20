import os
import sys
import time
import math
import random
from PyQt6.QtWidgets import QWidget, QMenu, QApplication
from PyQt6.QtCore import Qt, QTimer, QRect, QPoint, pyqtSignal
from PyQt6.QtGui import QPainter, QPixmap, QColor, QFont, QCursor, QTransform, QAction

from app_paths import ASSETS_DIR
from config_manager import ConfigManager
from pet_actions import ActionSheetCache, LEGACY_ACTIONS, load_action_catalog
from ui.dynamic_bubble_widget import DynamicSpeechBubble
from ui.interaction_dialog import InteractionDialog

DYNAMIC_ACTIONS_DIR = os.path.join(ASSETS_DIR, "dynamic_actions")

ACTION_DEFS = LEGACY_ACTIONS

class FloatingParticle:
    def __init__(self, x, y):
        self.x = x + random.uniform(-18, 18)
        self.y = y + random.uniform(-8, 8)
        self.vx = random.uniform(-1.0, 1.0)
        self.vy = random.uniform(-3.2, -1.6)
        self.alpha = 255.0
        self.size = random.uniform(15, 23)
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

    def __init__(self, parent=None, *, config_mgr=None):
        super().__init__(parent)
        self.config_mgr = config_mgr if config_mgr is not None else ConfigManager()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        target_h = int(self.config_mgr.get("target_height", 250))
        self.scale_factor = max(0.35, min(0.85, target_h / 380.0))
        self.dynamic_fps = int(self.config_mgr.get("dynamic_fps", 15))
        self.playback_speed_multiplier = self.dynamic_fps / 12.0
        self.last_critique_text = ""

        # Action cache
        self.actions = {}
        self._sheet_cache = ActionSheetCache(max_entries=2)
        self._load_actions()

        self.current_action_key = "idle" if "idle" in self.actions else next(iter(self.actions), "idle")
        current = self.actions.get(self.current_action_key)
        self._current_pixmap = self._sheet_cache.get(current["path"]) if current else QPixmap()
        self._loop_override = None
        self.current_frame = 0
        self.frame_accumulator = 0.0
        self.last_update_time = time.perf_counter()

        # 60 FPS Physics State
        self.is_dragging = False
        self._drag_pos = QPoint()
        self.last_global_pos = QPoint()
        self.velocity_x = 0.0
        self.tilt_angle = 0.0
        self.target_tilt = 0.0
        self.scale_y_ease = 1.0
        self.scale_x_ease = 1.0
        self.particles = []
        self._last_happy_feedback_at = float("-inf")
        self.time_counter = 0.0
        self._thinking_active = False
        self._menu_open = False
        self._last_random_key = None
        self._click_count = 0
        self._click_started_at = 0.0
        self._press_global = None
        self._press_moved = False
        self.configure_random_playback(
            self.config_mgr.get("random_actions_enabled", True),
            self.config_mgr.get("random_action_interval", 5),
        )

        # Sub-widgets: Compact Bubble & Interaction Dialog
        self.bubble = DynamicSpeechBubble()
        self.bubble.clicked.connect(self.reopen_last_bubble)

        self.interaction_dialog = InteractionDialog()
        self.interaction_dialog.user_replied.connect(self.user_replied.emit)

        # Place initially at bottom-right
        self._apply_size()
        self._place_at_bottom_right()

        # 60 FPS Render & Physics Loop (16ms)
        self.physics_timer = QTimer(self)
        self.physics_timer.timeout.connect(self._step_60fps)
        self.physics_timer.start(16)

    def _load_actions(self):
        self.actions = load_action_catalog(ASSETS_DIR)
        print(f"[DynamicPet] Loaded {len(self.actions)} dynamic animation action packs.")

    def _apply_size(self):
        act = self.actions.get(self.current_action_key)
        if act:
            w = int(act["frame_w"] * self.scale_factor)
            h = int(act["frame_h"] * self.scale_factor)
            self.resize(w + 30, h + 30)
        else:
            self.resize(240, 240)

    def _restore_visible_position(self):
        """仅启动恢复时纠正旧屏外坐标；运行中不约束用户放置。"""
        screens = QApplication.screens()
        if not screens:
            return
        center = self.geometry().center()
        screen = QApplication.screenAt(center)
        if screen is None:
            def distance(candidate):
                rect = candidate.availableGeometry()
                dx = max(rect.left() - center.x(), 0, center.x() - rect.right())
                dy = max(rect.top() - center.y(), 0, center.y() - rect.bottom())
                return dx * dx + dy * dy
            screen = min(screens, key=distance)
        geom = screen.availableGeometry()
        x = max(geom.left(), min(self.x(), geom.right() - self.width() + 1))
        y = max(geom.top(), min(self.y(), geom.bottom() - self.height() + 1))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _place_at_bottom_right(self):
        screen = self.screen() or QApplication.primaryScreen()
        geom = screen.availableGeometry() if screen else None
        saved_x = self.config_mgr.get("pet_pos_x", None)
        saved_y = self.config_mgr.get("pet_pos_y", None)

        if saved_x is not None and saved_y is not None and geom:
            try:
                sx, sy = int(saved_x), int(saved_y)
                self.move(sx, sy)
                self._restore_visible_position()
                return
            except (ValueError, TypeError):
                pass

        if geom:
            x = geom.x() + geom.width() - self.width() - 80
            y = geom.y() + geom.height() - self.height() - 110
        else:
            x, y = 1400, 700
        self.move(x, y)
        self._restore_visible_position()

    def set_action(self, key: str, *, restart=False):
        if getattr(self, '_desktop_running', False):
            return False
        if key not in self.actions:
            return False
        if self.current_action_key != key or restart:
            try:
                pixmap = self._sheet_cache.get(self.actions[key]["path"])
            except ValueError as exc:
                self.show_critique(str(exc))
                return False
            self._current_pixmap = pixmap
            self.current_action_key = key
            self.current_frame = 0
            self.frame_accumulator = 0.0
            self._apply_size()
            self.update()
        self._loop_override = None
        self._next_random_at = time.monotonic() + self.random_action_interval
        return True

    def play_action(self, key: str, *, loop=False):
        if not self.set_action(key, restart=True):
            return False
        self._loop_override = None if key == "idle" and loop else bool(loop)
        return True

    def configure_random_playback(self, enabled, interval):
        self.random_actions_enabled = bool(enabled)
        try:
            self.random_action_interval = max(5, min(3600, int(interval)))
        except (ValueError, TypeError):
            self.random_action_interval = 5
        self._next_random_at = time.monotonic() + self.random_action_interval

    def begin_desktop_move(self, dx):
        """模型驱动移动时临时占用动作；素材原始朝向为左。"""
        facing = self._body_transform().m11() < 0
        if self.play_action('gemini:原地左转奔跑', loop=True):
            self._desktop_running = True
            self._desktop_run_flipped = dx > 0 if dx else facing
            self.current_frame = 40
            self.update()

    def end_desktop_move(self):
        if not getattr(self, '_desktop_running', False):
            return
        self._desktop_running = False
        self._desktop_run_flipped = None
        self.set_action('thinking' if self._thinking_active else 'idle', restart=True)

    def _maybe_play_random(self):
        if (not self.random_actions_enabled or not self.isVisible() or self.is_dragging
                or getattr(self, "feature_motion_active", False) or getattr(self, "feature_sleeping", False)
                or self._thinking_active or self._menu_open
                or self.interaction_dialog.isVisible() or self.current_action_key != "idle"
                or self._loop_override is not None or time.monotonic() < self._next_random_at):
            return
        choices = [key for key in self.actions if key not in ("idle", self._last_random_key)]
        if choices:
            key = random.choice(choices)
            if self.play_action(key):
                self._last_random_key = key
        self._next_random_at = time.monotonic() + self.random_action_interval

    def set_thinking(self, is_thinking: bool):
        self._thinking_active = bool(is_thinking)
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
        keys = list(self.actions)
        if not keys:
            return
        try:
            curr_idx = keys.index(self.current_action_key)
            next_idx = (curr_idx + 1) % len(keys)
        except ValueError:
            next_idx = 0
        target = keys[next_idx]
        if not self.set_action(target):
            return
        title = self.actions[target]["title"]
        self.show_critique(f"已切换动作：【{title}】喵~")

    def update_size(self, new_height: int):
        self.scale_factor = max(0.35, min(0.85, new_height / 380.0))
        self._apply_size()
        self.update()

    def set_dynamic_fps(self, fps: int):
        self.dynamic_fps = max(6, min(36, int(fps)))
        self.playback_speed_multiplier = self.dynamic_fps / 12.0

    def trigger_happy_reaction(self, x=None, y=None):
        now = time.monotonic()
        if now - self._last_happy_feedback_at < 0.8:
            return
        self._last_happy_feedback_at = now
        if x is None:
            x = self.width() / 2.0
        if y is None:
            y = self.height() / 3.0
        # 低帧率时粒子可能尚未消散，限制总量避免连续反馈堆积。
        for _ in range(min(7, max(0, 14 - len(self.particles)))):
            self.particles.append(FloatingParticle(x, y))
        self.set_action("happy")

    def _calc_bubble_pos(self, bw: int, bh: int) -> tuple[int, int]:
        """Calculates exact bubble position so comic tail tip points directly at the cat ear."""
        # Catgirl's horizontal center in global coordinates
        head_cx = self.x() + int(self.width() * 0.5)
        # Catgirl's head top in global coordinates
        head_top_y = self.y() + int(24 * self.scale_factor)

        # In DynamicSpeechBubble, tail tip is at `bx + bw - 14`
        # We want the tail tip to point at catgirl's left cat ear: `head_cx - 8`
        bx = (head_cx - 8) - (bw - 14)
        # Tail tip touches right above the ear
        by = head_top_y - bh + 14

        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            # If overflowing left screen edge, flip bubble to the right of pet
            if bx < geom.x() + 10:
                bx = head_cx + 15
            # If overflowing top screen edge, place below pet
            if by < geom.y() + 10:
                by = self.y() + self.height() - 20
            bx = max(geom.left(), min(bx, geom.right() - bw + 1))
            by = max(geom.top(), min(by, geom.bottom() - bh + 1))
        return bx, by

    def show_critique(self, text: str):
        self.last_critique_text = text
        self.raise_()
        bw, bh = self.bubble.calculate_size(text)
        bx, by = self._calc_bubble_pos(bw, bh)

        self.bubble.move(bx, by)
        self.bubble.show_text(text)
        self.bubble.raise_()

    def reopen_last_bubble(self):
        last_text = getattr(self, "last_critique_text", "") or self.bubble._full_text
        if not last_text or not last_text.strip():
            self.show_critique("刚才还没有锐评记录喵~ 点击我或按 Ctrl+F9 呼叫锐评吧！")
            return
        self.bubble.hide()
        self.interaction_dialog.open_for_critique(last_text, self.pos())

    def _step_60fps(self):
        self._maybe_play_random()
        now = time.perf_counter()
        dt = min(0.05, now - self.last_update_time)
        self.last_update_time = now
        self.time_counter += dt

        act = self.actions.get(self.current_action_key)
        if act:
            target_fps = act["base_fps"] * self.playback_speed_multiplier
            frame_duration = 1.0 / max(1.0, target_fps)
            self.frame_accumulator += dt
            start, end = (40, 64) if getattr(self, '_desktop_running', False) else (0, act['count'])

            while self.frame_accumulator >= frame_duration:
                self.frame_accumulator -= frame_duration
                self.current_frame += 1
                if self.current_frame >= end:
                    looping = self._loop_override if self._loop_override is not None else act["loop"]
                    if looping:
                        self.current_frame = start
                    else:
                        self.set_action("idle", restart=True)
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
            self._press_global = event.globalPosition().toPoint()
            self._press_moved = False
            self.is_dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.last_global_pos = event.globalPosition().toPoint()
            self.velocity_x = 0.0
            event.accept()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._click_count = 0
            self.cycle_next_outfit()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._click_count = 0
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            curr_pos = event.globalPosition().toPoint()
            if self._press_global is not None and (curr_pos - self._press_global).manhattanLength() >= QApplication.startDragDistance():
                self._press_moved = True
                self._click_count = 0
                if self.current_action_key != "dangling" and not getattr(self, "feature_motion_input", False):
                    self.set_action("dangling")
            if not self._press_moved:
                event.accept()
                return
            self.velocity_x = (curr_pos.x() - self.last_global_pos.x())
            self.last_global_pos = curr_pos
            self.move(curr_pos - self._drag_pos)

            # Move bubble alongside in real time
            if self.bubble.isVisible():
                bx, by = self._calc_bubble_pos(self.bubble.width(), self.bubble.height())
                self.bubble.move(bx, by)
            if hasattr(self, 'interaction_dialog') and self.interaction_dialog.isVisible():
                dw = self.interaction_dialog.width()
                dh = self.interaction_dialog.height()
                self.interaction_dialog.move(self.x() - dw + 45, self.y() - dh - 10)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            is_click = self._press_global is not None and not self._press_moved
            if self._press_global is not None:
                is_click = is_click and (event.globalPosition().toPoint() - self._press_global).manhattanLength() < QApplication.startDragDistance()
            self._press_global = None
            self.is_dragging = False
            self.velocity_x = 0.0
            if (not getattr(self, "feature_motion_input", False)
                    and (self._press_moved or self.current_action_key == "dangling" or self._thinking_active)):
                self.set_action("thinking" if self._thinking_active else "idle")
            self._next_random_at = time.monotonic() + self.random_action_interval
            # Save pet desktop coordinates on drag end
            self.config_mgr.save({
                "pet_pos_x": self.x(),
                "pet_pos_y": self.y()
            })
            if is_click:
                now = time.monotonic()
                if self._click_count == 0 or now - self._click_started_at > 2.0:
                    self._click_count = 0
                    self._click_started_at = now
                self._click_count += 1
                if self._click_count == 5:
                    self._click_count = 0
                    self.request_critique.emit()
            else:
                self._click_count = 0
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseDoubleClickEvent(event)
        # Qt第二下发送双击事件而非普通按下，仍须配对本次释放计一次点击。
        self.mousePressEvent(event)
        self.trigger_happy_reaction(event.position().x(), event.position().y())
        event.accept()

    def wheelEvent(self, event):
        self.cycle_next_outfit()
        event.accept()

    def _build_context_menu(self):
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

        act_reopen = QAction("💬 打开刚才的评论框/吐槽互动", self)
        act_reopen.triggered.connect(self.reopen_last_bubble)
        menu.addAction(act_reopen)

        switch_act = QAction("🎭 切换动作/神态", self)
        switch_act.triggered.connect(self.cycle_next_outfit)
        menu.addAction(switch_act)

        library = menu.addMenu(f"🎬 全部动作（{len(self.actions)}）")
        groups = {}
        for key, row in self.actions.items():
            category = row["category"]
            if category not in groups:
                groups[category] = library.addMenu(category)
            choice = groups[category].addAction(row["title"])
            choice.setData(key)
            choice.setCheckable(True)
            choice.setChecked(key == self.current_action_key)
            choice.triggered.connect(lambda _checked=False, selected=key: self.play_action(selected))

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

        return menu

    def _show_context_menu(self, global_pos: QPoint):
        menu = self._build_context_menu()
        self._menu_open = True
        try:
            menu.exec(global_pos)
        finally:
            self._menu_open = False
            self._next_random_at = time.monotonic() + self.random_action_interval
            menu.deleteLater()

    def _body_transform(self):
        cx = self.width() / 2.0
        cy = self.height() - 15.0

        transform = QTransform()
        transform.translate(cx, cy)
        transform.rotate(self.tilt_angle)
        squash = max(0., min(.18, getattr(self, "feature_squash", 0.)))
        transform.scale(self.scale_x_ease * (1 + squash), self.scale_y_ease * (1 - squash))
        transform.translate(-cx, -cy)
        flipped = getattr(self, '_desktop_run_flipped', None)
        if flipped is None:
            flipped = bool(getattr(self, 'feature_flipped', False)) ^ bool(getattr(self, 'manual_mirrored', False))
        if flipped:
            transform.translate(self.width(), 0)
            transform.scale(-1, 1)
        return transform

    def paintEvent(self, event):
        act = self.actions.get(self.current_action_key)
        if not act or self._current_pixmap.isNull():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setTransform(self._body_transform())

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
        custom_resource = getattr(self, "feature_resource_pixmap", None)
        if custom_resource is not None and not custom_resource.isNull():
            painter.drawPixmap(dst_rect, custom_resource, custom_resource.rect())
        else:
            painter.drawPixmap(dst_rect, self._current_pixmap, src_rect)

        painter.resetTransform()

        for p in self.particles:
            color = QColor(p.color)
            color.setAlpha(max(0, min(255, int(p.alpha))))
            font = QFont("Segoe UI Emoji", int(p.size))
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(int(p.x), int(p.y), p.char)
