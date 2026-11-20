import os
from PyQt6.QtWidgets import QWidget, QLabel, QMenu, QApplication
from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QCursor, QAction
from .bubble_widget import SpeechBubble
from .interaction_dialog import InteractionDialog
from config_manager import ConfigManager
from app_paths import ASSETS_DIR

EMOTION_MAP = {
    "confident": "avatar_standing.png",
    "curious": "avatar_bust.png",
    "shocked": "avatar_shocked.png",
    "relieved": "avatar_relieved.png"
}

OUTFIT_LABELS = {
    "confident": "自信傲娇立绘",
    "curious": "呆萌疑惑Q版",
    "shocked": "抱头震惊表情",
    "relieved": "害羞无语擦汗"
}

class DesktopPetWidget(QWidget):
    open_settings = pyqtSignal()
    request_critique = pyqtSignal()
    user_replied = pyqtSignal(str)
    toggle_tts = pyqtSignal()
    toggle_auto = pyqtSignal()
    quit_app = pyqtSignal()

    def __init__(self, avatar_path: str = None, target_height: int = 250, parent=None):
        super().__init__(parent)
        self.config_mgr = ConfigManager()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.target_height = target_height
        if not avatar_path:
            outfits = self.config_mgr.get("outfits", {})
            fn = outfits.get("confident", "avatar_standing.png")
            avatar_path = os.path.join(ASSETS_DIR, fn)

        self.avatar_path = avatar_path
        self.current_filename = os.path.basename(avatar_path)
        self._drag_pos = QPoint()

        # UI: Avatar Label
        self.avatar_label = QLabel(self)
        self.avatar_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._load_avatar(avatar_path)

        # Bubble (Passive Comic Speech Bubble with Typewriter effect)
        self.bubble = SpeechBubble()
        self.bubble.clicked.connect(self.reopen_last_bubble)

        # Interaction Dialog (Interactive reply card)
        self.interaction_dialog = InteractionDialog()
        self.interaction_dialog.user_replied.connect(self.user_replied.emit)

        # Place initially at bottom-right corner of screen
        self._place_at_bottom_right()

        # Subtle hovering animation offset (smooth and lightweight)
        self._hover_offset = 0.0
        self._hover_direction = 1.0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._step_hover)
        self._anim_timer.start(120)

    def _load_avatar(self, path: str):
        self.avatar_path = path
        self.current_filename = os.path.basename(path)
        if os.path.exists(path):
            pixmap = QPixmap(path)
            scaled = pixmap.scaledToHeight(
                self.target_height,
                Qt.TransformationMode.SmoothTransformation
            )
            if scaled.width() > 220:
                scaled = pixmap.scaledToWidth(
                    220,
                    Qt.TransformationMode.SmoothTransformation
                )

            self.avatar_label.setPixmap(scaled)
            self.avatar_label.resize(scaled.size())
            self.resize(scaled.width(), scaled.height() + 10)
            self.avatar_label.move(0, 5)
        else:
            pet_name = self.config_mgr.get("pet_name", "哈Gemi")
            self.avatar_label.setText(pet_name)
            self.avatar_label.resize(160, 220)
            self.resize(160, 220)

    def update_size(self, new_height: int):
        self.target_height = new_height
        self._load_avatar(self.avatar_path)

    def set_emotion(self, emotion: str):
        """Automatically switch outfit/expression based on critique emotion."""
        cfg_outfits = self.config_mgr.get("outfits", {})
        fallback = EMOTION_MAP.get(emotion.lower(), "avatar_standing.png")
        filename = cfg_outfits.get(emotion.lower(), fallback)
        full_path = os.path.join(ASSETS_DIR, filename)
        if os.path.exists(full_path) and self.current_filename != filename:
            print(f"[DesktopPet] Emotion triggered avatar switch: [{emotion}] -> {filename}", flush=True)
            self._load_avatar(full_path)

    def cycle_next_outfit(self):
        """Cycle to the next outfit/expression."""
        cfg_outfits = self.config_mgr.get("outfits", {})
        filenames = [cfg_outfits.get(k, EMOTION_MAP[k]) for k in ["confident", "curious", "shocked", "relieved"]]
        try:
            curr_idx = filenames.index(self.current_filename)
            next_idx = (curr_idx + 1) % len(filenames)
        except ValueError:
            next_idx = 0
        self.change_outfit(filenames[next_idx])

    def change_outfit(self, filename: str):
        full_path = os.path.join(ASSETS_DIR, filename)
        if os.path.exists(full_path):
            self._load_avatar(full_path)

    def _place_at_bottom_right(self):
        screen_obj = self.screen() or QApplication.primaryScreen()
        geom = screen_obj.availableGeometry() if screen_obj else None
        saved_x = self.config_mgr.get("pet_pos_x", None)
        saved_y = self.config_mgr.get("pet_pos_y", None)

        if saved_x is not None and saved_y is not None and geom:
            try:
                sx, sy = int(saved_x), int(saved_y)
                if geom.x() - 100 <= sx <= geom.x() + geom.width() - 50 and \
                   geom.y() - 100 <= sy <= geom.y() + geom.height() - 50:
                    self.move(sx, sy)
                    print(f"[DesktopPet] Restored saved pet position at ({sx}, {sy})", flush=True)
                    return
            except (ValueError, TypeError):
                pass

        if geom:
            x = geom.x() + geom.width() - self.width() - 80
            y = geom.y() + geom.height() - self.height() - 110
        else:
            x, y = 1400, 700
        self.move(x, y)
        print(f"[DesktopPet] Placed pet at ({x}, {y})", flush=True)

    def _step_hover(self):
        self._hover_offset += 0.5 * self._hover_direction
        if abs(self._hover_offset) >= 3:
            self._hover_direction *= -1
        self.avatar_label.move(0, int(5 + self._hover_offset))

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()

    def show_critique(self, text: str):
        self.raise_()
        bw, bh = self.bubble.calculate_size(text)

        # Position bubble nicely above-left of pet based on pre-measured size
        bx = self.x() - bw + 45
        by = self.y() - bh - 6

        # Check screen bounds
        screen = self.screen() or QApplication.primaryScreen()
        if screen:
            geom = screen.availableGeometry()
            if bx < geom.x() + 10:
                bx = self.x() + self.width() - 30
            if by < geom.y() + 10:
                by = self.y() + self.height() + 10

        self.bubble.move(bx, by)
        self.bubble.show_text(text)
        self.bubble.raise_()

    def reopen_last_bubble(self):
        """Reopens the last critique in the interaction dialog for chatting."""
        last_text = self.bubble._full_text
        if not last_text or not last_text.strip():
            self.show_critique("刚才还没有锐评记录喵~ 点击我或按 Ctrl+F9 呼叫锐评吧！")
            return

        self.bubble.hide()
        self.interaction_dialog.open_for_critique(last_text, self.pos())

    # Mouse drag & click & wheel events
    def wheelEvent(self, event):
        """Scroll mouse wheel over pet to cycle outfits instantly!"""
        self.cycle_next_outfit()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.cycle_next_outfit()
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            if self.bubble.isVisible():
                bw = self.bubble.width()
                bh = self.bubble.height()
                self.bubble.move(new_pos.x() - bw + 45, new_pos.y() - bh - 6)
                self.bubble.raise_()
            if self.interaction_dialog.isVisible():
                dw = self.interaction_dialog.width()
                dh = self.interaction_dialog.height()
                self.interaction_dialog.move(new_pos.x() - dw + 45, new_pos.y() - dh - 10)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.config_mgr.save({
                "pet_pos_x": self.x(),
                "pet_pos_y": self.y()
            })
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.request_critique.emit()
            event.accept()

    def _show_context_menu(self, pos: QPoint):
        menu = self._build_context_menu()
        try:
            menu.exec(pos)
        finally:
            menu.deleteLater()

    def _build_context_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: rgba(30, 25, 45, 245);
                color: #ffffff;
                border: 1.5px solid #ff4e88;
                border-radius: 10px;
                padding: 6px;
                font-family: "Microsoft YaHei";
            }
            QMenu::item {
                padding: 6px 20px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ff4e88;
                color: #ffffff;
            }
        """)

        act_settings = QAction("控制台与设置中心", self)
        act_settings.triggered.connect(self.open_settings.emit)
        menu.addAction(act_settings)

        menu.addSeparator()

        pet_name = self.config_mgr.get("pet_name", "哈Gemi")
        act_critique = QAction(f"呼叫{pet_name}锐评 (Ctrl+F9)", self)
        act_critique.triggered.connect(self.request_critique.emit)
        menu.addAction(act_critique)

        act_reopen = QAction("打开刚才的评论框/吐槽互动", self)
        act_reopen.triggered.connect(self.reopen_last_bubble)
        menu.addAction(act_reopen)

        act_switch = QAction("切换下一个立绘 (滚轮/中键/Ctrl+F11)", self)
        act_switch.triggered.connect(self.cycle_next_outfit)
        menu.addAction(act_switch)

        menu.addSeparator()

        # Outfit submenu
        outfit_menu = menu.addMenu("切换造型/差分")
        outfit_menu.setStyleSheet(menu.styleSheet())

        cfg_outfits = self.config_mgr.get("outfits", {})
        for emo in ["confident", "curious", "shocked", "relieved"]:
            fn = cfg_outfits.get(emo, EMOTION_MAP[emo])
            label = OUTFIT_LABELS.get(emo, emo)
            prefix = "✔ " if self.current_filename == fn else "   "
            action = QAction(f"{prefix}{label}", self)
            action.triggered.connect(lambda checked=False, target_fn=fn: self.change_outfit(target_fn))
            outfit_menu.addAction(action)

        menu.addSeparator()

        act_tts = QAction("切换语音吐槽 (Ctrl+F10)", self)
        act_tts.triggered.connect(self.toggle_tts.emit)
        menu.addAction(act_tts)

        act_auto = QAction("自动感知开关", self)
        act_auto.triggered.connect(self.toggle_auto.emit)
        menu.addAction(act_auto)

        menu.addSeparator()

        act_quit = QAction("退出程序", self)
        act_quit.triggered.connect(self.quit_app.emit)
        menu.addAction(act_quit)

        return menu

    def closeEvent(self, event):
        self.bubble.close()
        self.interaction_dialog.close()
        super().closeEvent(event)
