"""可修改组合键配置；新绑定通过校验后替换监听器。"""
import json
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QKeySequence

DEFAULT_KEYS = {"critique": "Ctrl+F9", "voice": "Ctrl+F1", "mute": "Ctrl+F10",
                "outfit": "Ctrl+F11", "qa": "Ctrl+Alt+Q",
                "desktop": "", "desktop_voice": "", "desktop_stop": ""}
KEY_LABELS = {"critique": "立即锐评", "voice": "语音输入", "mute": "切换静音",
              "outfit": "切换动作", "qa": "截图答疑", "desktop": "操作桌面",
              "desktop_voice": "桌面操作语音", "desktop_stop": "停止桌面操作"}


def key_signature(text):
    from pynput.keyboard import HotKey
    value = text if '<' in text else parse_keys(text)
    return frozenset(str(part) for part in HotKey.parse(value)) if value else frozenset()


def parse_keys(text):
    if not text.strip():
        return ""
    seq = QKeySequence.fromString(text, QKeySequence.SequenceFormat.PortableText)
    if seq.isEmpty() or seq.count() != 1:
        raise ValueError("请输入一个按键或组合键，不支持连续多组快捷键")
    value = seq.toString(QKeySequence.SequenceFormat.PortableText)
    parts = value.lower().split("+")
    aliases = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>",
               "space": "<space>", "esc": "<esc>", "return": "<enter>", "enter": "<enter>",
               "tab": "<tab>", "delete": "<delete>", "backspace": "<backspace>",
               "up": "<up>", "down": "<down>", "left": "<left>", "right": "<right>",
               "home": "<home>", "end": "<end>", "pgup": "<page_up>", "pgdown": "<page_down>"}
    if not all(parts):
        raise ValueError("请使用字母、数字、功能键或组合键")
    from pynput.keyboard import HotKey
    result = "+".join(aliases.get(p, "<" + p + ">" if p.startswith("f") and p[1:].isdigit() else p) for p in parts)
    try:
        HotKey.parse(result)
    except ValueError as exc:
        raise ValueError("不支持这个按键，请重新录入") from exc
    return result


class HotkeyStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        return {key: data.get(key, default) for key, default in DEFAULT_KEYS.items()}

    @staticmethod
    def validate(data, extra=()):
        seen = set()
        for value in extra:
            signature = key_signature(value)
            if signature and any(signature <= old or old <= signature for old in seen):
                raise ValueError('扩展快捷键重复')
            if signature:
                seen.add(signature)
        for key in DEFAULT_KEYS:
            value = parse_keys(data.get(key, ""))
            signature = key_signature(value)
            if value and any(signature <= old or old <= signature for old in seen):
                raise ValueError("快捷键重复或与扩展绑定冲突：" + KEY_LABELS[key])
            if value:
                seen.add(signature)
        return data

    def save(self, data):
        self.validate(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)


class ConfiguredHotkeys(QObject):
    invoked = pyqtSignal(str)

    def __init__(self, callbacks, *, listener_factory=None):
        super().__init__()
        self.callbacks, self.bindings, self.listener = callbacks, {}, None
        self.listener_factory = listener_factory
        self.invoked.connect(self._dispatch)

    def _dispatch(self, key):
        callback = self.callbacks.get(key)
        if callback:
            callback()

    def apply(self, bindings, extra=(), *, extra_callbacks=None, persist=None):
        HotkeyStore.validate(bindings, extra)
        factory = self.listener_factory
        if factory is None:
            from pynput.keyboard import GlobalHotKeys
            factory = GlobalHotKeys
        mapping = {parse_keys(value): (lambda k=key: self.invoked.emit(k))
                   for key, value in bindings.items() if value and key in self.callbacks}
        for index, (keys, callback) in enumerate((extra_callbacks or {}).items()):
            name = 'extra:' + str(index)
            mapping[keys] = lambda k=name: self.invoked.emit(k)
        extra_actions = {'extra:' + str(i): cb for i, cb in enumerate((extra_callbacks or {}).values())}
        candidate = factory(mapping) if mapping else None
        try:
            if candidate:
                candidate.daemon = True
                candidate.start()
            if persist:
                persist(bindings)
        except Exception:
            if candidate:
                candidate.stop()
            raise
        self.stop()
        self.callbacks = {k: v for k, v in self.callbacks.items() if not k.startswith('extra:')}
        self.callbacks.update(extra_actions)
        self.listener, self.bindings = candidate, dict(bindings)

    def _handle_voice_input(self):
        self.invoked.emit('voice')

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
