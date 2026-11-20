"""只向原菜单锐评下一项追加答疑，复用原样式与全部项目。"""
from PyQt6.QtGui import QAction


def install_menu(pet, controller):
    original = pet._build_context_menu
    def build():
        menu = original()
        actions = menu.actions()
        keys = getattr(pet, 'shortcut_labels', {})
        inserted = False
        for index, action in enumerate(actions):
            text = action.text()
            for name, old in (('critique', 'Ctrl+F9'), ('voice', 'Ctrl+F1'), ('mute', 'Ctrl+F10'), ('outfit', 'Ctrl+F11')):
                if old in text and name in keys:
                    text = text.replace(old, keys[name] or '未绑定')
            action.setText(text)
            if '锐评' in text and not inserted:
                inserted = True
                key = keys.get('qa', 'Ctrl+Alt+Q')
                qa = QAction('📷 截图答疑' + (' (' + key + ')' if key else ''), menu)
                qa.triggered.connect(lambda _checked=False: controller.screenshot_qa.open())
                before = actions[index + 1] if index + 1 < len(actions) else None
                menu.insertAction(before, qa)
                desktop = QAction('🖱 操作桌面…', menu)
                desktop.triggered.connect(lambda _checked=False: getattr(
                    getattr(controller, 'desktop_control', None), 'open', lambda: None)())
                menu.insertAction(before, desktop)
                stop = QAction('停止桌面操作', menu)
                stop.triggered.connect(lambda _checked=False: getattr(
                    getattr(controller, 'desktop_control', None), 'stop', lambda: None)())
                menu.insertAction(before, stop)
        return menu
    pet._build_context_menu = build
