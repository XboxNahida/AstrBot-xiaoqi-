import os
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction
from PyQt6.QtCore import pyqtSignal

class DesktopPetTray(QSystemTrayIcon):
    open_settings = pyqtSignal()
    open_update_dialog = pyqtSignal()
    trigger_critique = pyqtSignal()
    reopen_bubble = pyqtSignal()
    cycle_outfit = pyqtSignal()
    toggle_pet_visibility = pyqtSignal()
    toggle_tts = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, icon_path: str, parent=None):
        super().__init__(parent)
        if os.path.exists(icon_path):
            self.setIcon(QIcon(icon_path))
        else:
            self.setIcon(QIcon.fromTheme("applications-games"))

        self.setToolTip("Gemini 赛博猫娘 · 哈Gemi\n双击打开控制台与设置中心")

        # Double-click handler
        self.activated.connect(self._on_activated)

        # Create menu
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: rgba(30, 25, 45, 245);
                color: #ffffff;
                border: 1px solid #ff4e88;
                border-radius: 8px;
                padding: 6px;
                font-family: "Microsoft YaHei";
            }
            QMenu::item {
                padding: 6px 18px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ff4e88;
                color: #ffffff;
            }
        """)

        header_title = QAction("✨ 哈Gemi 赛博桌宠", self)
        header_title.setEnabled(False)
        menu.addAction(header_title)

        menu.addSeparator()

        act_settings = QAction("控制台与设置中心", self)
        act_settings.triggered.connect(self.open_settings.emit)
        menu.addAction(act_settings)

        act_update = QAction("检查更新与最新公告...", self)
        act_update.triggered.connect(self.open_update_dialog.emit)
        menu.addAction(act_update)

        menu.addSeparator()

        act_critique = QAction("呼叫哈Gemi锐评 (Ctrl+F9)", self)
        act_critique.triggered.connect(self.trigger_critique.emit)
        menu.addAction(act_critique)

        act_reopen = QAction("打开刚才的评论框/吐槽互动", self)
        act_reopen.triggered.connect(self.reopen_bubble.emit)
        menu.addAction(act_reopen)

        act_cycle = QAction("切换下一个立绘 (Ctrl+F11)", self)
        act_cycle.triggered.connect(self.cycle_outfit.emit)
        menu.addAction(act_cycle)

        menu.addSeparator()

        self.act_vis = QAction("显示/隐藏桌宠", self)
        self.act_vis.triggered.connect(self.toggle_pet_visibility.emit)
        menu.addAction(self.act_vis)

        self.act_tts = QAction("开启/关闭语音 (Ctrl+F10)", self)
        self.act_tts.triggered.connect(self.toggle_tts.emit)
        menu.addAction(self.act_tts)

        menu.addSeparator()

        act_quit = QAction("退出程序", self)
        act_quit.triggered.connect(self.quit_requested.emit)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.show()

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_settings.emit()
