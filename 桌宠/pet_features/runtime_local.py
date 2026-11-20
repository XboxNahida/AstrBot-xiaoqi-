"""用户明确操作的资源、外观与窗口工具，始终在Qt主线程。"""
import time
from pathlib import Path

from PyQt6.QtCore import QEvent, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QImageReader, QPixmap, QTransform
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from .desktop_tools import DesktopTools
from .interaction import PetInteraction
from .interaction_visual import InteractionVisual
from .resources import ResourceLibrary


class LocalFeatures:
    def configure_local(self):
        pet = self.controller.pet
        pet.manual_mirrored = self.cfg['resources']['mirrored']
        if self.tools is None:
            self.tools = DesktopTools(pet, getattr(self.controller, "tray", None))
            pet.installEventFilter(self)
            self.behavior_timer = QTimer(self)
            self.behavior_timer.setInterval(100)
            self.behavior_timer.timeout.connect(lambda: self.tools.tick(.1))
        self.tools.apply(self.cfg["behavior"])
        if self.cfg["behavior"]["enabled"] and self.cfg["behavior"]["mode"] != "stationary":
            self.behavior_timer.start()
        else:
            self.behavior_timer.stop()
        if not hasattr(self, "interaction"):
            self.interaction = PetInteraction(pet, blocked=lambda: bool(
                getattr(pet, '_desktop_auto_moving', False)
                or (self.cfg["behavior"]["enabled"] and self.cfg["behavior"]["locked"])))
            self.interaction.visual = InteractionVisual(pet)
            pet.feature_interaction = self.interaction
        self.interaction.configure(self.cfg["interaction"])
        # 观察器先收按下/释放，再交给可能吞事件的物理过滤器。
        pet.removeEventFilter(self.tools)
        pet.installEventFilter(self.tools)
        if not hasattr(self, "resources"):
            self.resources = ResourceLibrary(Path(self.root) / "user_resources")
        selected = self.cfg["resources"]["selected"]
        try:
            pet.feature_resource_pixmap = self.resources.pixmap(selected) if self.cfg["resources"]["enabled"] and selected else None
        except (ValueError, OSError):
            pet.feature_resource_pixmap = None
            self.panel.feedback.setText("自定义图像不可用，已保留默认角色。")
        pet.update()
        if hasattr(pet, "avatar_label"):
            self._classic_overlay()
        window = getattr(self.controller, "settings_win", None)
        if window is not None:
            self._apply_appearance(window)

    def eventFilter(self, watched, event):
        pet = self.controller.pet
        if watched is pet:
            if event.type() == QEvent.Type.MouseButtonRelease and not getattr(pet, "_drag_moved", False):
                self._play_sound()
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Paint) and hasattr(pet, "avatar_label"):
                self._classic_overlay()
        return super().eventFilter(watched, event)

    def _classic_overlay(self):
        pet = self.controller.pet
        if not hasattr(self, "resource_overlay"):
            self.resource_overlay = QLabel(pet)
            self.resource_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        pixmap = getattr(pet, "feature_resource_pixmap", None)
        flip = bool(getattr(pet, 'feature_flipped', False)) ^ bool(getattr(pet, 'manual_mirrored', False))
        if pixmap is None and flip:
            pixmap = pet.avatar_label.pixmap()
        if pixmap is None or pixmap.isNull():
            self.resource_overlay.hide()
            self._restore_classic_avatar()
            self._overlay_key = None
            return
        if not getattr(self, '_avatar_hidden_for_overlay', False):
            self._avatar_was_hidden = pet.avatar_label.isHidden()
            self._avatar_hidden_for_overlay = True
            pet.avatar_label.hide()
        self.resource_overlay.setGeometry(pet.avatar_label.geometry())
        key = (pixmap.cacheKey(), flip, pet.avatar_label.width(), pet.avatar_label.height())
        if key == getattr(self, "_overlay_key", None):
            return
        self._overlay_key = key
        if flip:
            pixmap = pixmap.transformed(QTransform().scale(-1, 1))
        self.resource_overlay.setGeometry(pet.avatar_label.geometry())
        self.resource_overlay.setPixmap(pixmap.scaled(pet.avatar_label.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.resource_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.resource_overlay.show()
        self.resource_overlay.raise_()

    def _restore_classic_avatar(self):
        if getattr(self, '_avatar_hidden_for_overlay', False):
            self._avatar_hidden_for_overlay = False
            if not self._avatar_was_hidden:
                self.controller.pet.avatar_label.show()

    def _apply_appearance(self, window):
        if not hasattr(self, "original_appearance"):
            self.original_appearance = (window.styleSheet(), window.windowTitle(), window.windowIcon(), window.windowOpacity())
            self.original_fps = getattr(self.controller.pet, "dynamic_fps", 12)
        style, title, icon, opacity = self.original_appearance
        cfg = self.cfg["appearance"]
        if cfg["enabled"]:
            color = QColor(cfg["accent"])
            if color.isValid():
                style += "\nQPushButton:hover {border-color: " + color.name() + ";}"
            image = Path(cfg["background"]) if cfg["background"] else None
            if image and image.is_file():
                from PyQt6.QtCore import QUrl
                uri = QUrl.fromLocalFile(str(image.resolve())).toString()
                style += '\nQWidget#FeatureBackground {border-image: url("' + uri.replace('"', '') + '");}'
                window.setObjectName("FeatureBackground")
            title = cfg["title"] or title
            icon = QIcon(cfg["icon"]) if cfg["icon"] and Path(cfg["icon"]).is_file() else icon
            opacity = max(.2, cfg["opacity"] / 100)
            if cfg["remember_size"]:
                window.resize(max(640, cfg["width"]), max(400, cfg["height"]))
            fps = {"low": 6, "smooth": 24}.get(cfg["performance"], self.original_fps)
            if cfg["reduce_motion"]:
                fps = min(fps, 6)
        else:
            fps = self.original_fps
        window.setStyleSheet(style)
        window.setWindowTitle(title)
        window.setWindowIcon(icon)
        window.setWindowOpacity(opacity)
        pet = self.controller.pet
        if hasattr(pet, "set_dynamic_fps"):
            pet.set_dynamic_fps(fps)

    def _local_save(self, section, values):
        self.panel.config = self.panel.store.update_section(section, values)
        self.panel.refresh_section(section)
        self.apply(self.panel.config)

    def handle_local(self, action):
        if action in ("tools:sleep", "tools:wake"):
            if not hasattr(self, "interaction"):
                self.configure_local()
            if action == "tools:sleep":
                QTimer.singleShot(0, lambda: None if self.closed else self.interaction.sleep())
            else:
                self.interaction.wake()
        elif action == "tools:rescue":
            if self.tools is None:
                self.configure_local()
            self.tools.rescue()
            self._local_save("behavior", {"locked": False, "click_through": False, "enabled": False})
        elif action == "tools:cache":
            cache = getattr(self.controller.pet, "_sheet_cache", None)
            self.panel.show_result("当前已解码动作图：" + str(len(cache) if cache is not None else 0) + "（最多2张）")
        elif action == "resources:import":
            path, _filter = QFileDialog.getOpenFileName(self.panel, "导入自定义静态形象", "", "图像 (*.png *.jpg *.jpeg *.webp *.bmp)")
            if path:
                reader = QImageReader(path)
                reader.setScaledSize(QSize(240, 300))
                preview = QPixmap.fromImage(reader.read())
                if preview.isNull():
                    self.panel.feedback.setText("图像无法读取。")
                elif self._confirm_image(preview):
                    try:
                        item = self.resources.import_image(path)
                        self._local_save("resources", {"enabled": True, "selected": item["id"], "items": self.resources.list()})
                    except (ValueError, OSError) as exc:
                        self.panel.feedback.setText(str(exc))
        elif action == "resources:reset":
            self._local_save("resources", {"enabled": False, "selected": ""})
        elif action == "resources:delete":
            identity = self.cfg["resources"]["selected"]
            if identity:
                try:
                    self.resources.remove(identity)
                    self._local_save("resources", {"selected": "", "enabled": False, "items": self.resources.list()})
                except (ValueError, OSError) as exc:
                    self.panel.feedback.setText(str(exc))
        elif action == "sound:preview":
            path, _filter = QFileDialog.getOpenFileName(self.panel, "选择本地互动音效", "", "音频 (*.wav *.mp3 *.ogg *.m4a)")
            if path:
                self._local_save("sound", {"path": path})
                self._play_sound(preview=True)
        elif action == "appearance:reset":
            self._local_save("appearance", {"enabled": False})
        elif action == "tools:trim":
            return QMessageBox.question(self.panel, "回收后台工作集", "只对后台非关键进程执行一次工作集回收。继续？") != QMessageBox.StandardButton.Yes
        else:
            return False
        return True

    def _confirm_image(self, pixmap):
        dialog = QDialog(self.panel)
        dialog.setWindowTitle("预览自定义形象")
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        layout.addWidget(QLabel("将作为可选静态形象使用，原角色与动作资源保留。"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _play_sound(self, preview=False):
        cfg = self.cfg["sound"]
        if not (preview or cfg["enabled"]) or not cfg["path"]:
            return
        if time.monotonic() - getattr(self, "last_sound", float("-inf")) < cfg["interval_ms"] / 1000:
            return
        self.last_sound = time.monotonic()
        speaker = self.controller.speaker
        if hasattr(speaker, "play_local_file") and not speaker.play_local_file(cfg["path"], cfg["volume"]):
            self.panel.feedback.setText("音效未播放：请检查总语音开关、设备限制、免打扰及当前播放状态。")
