"""扩展业务线程与原桌宠的主线程接线。"""
import copy
import html
import queue
import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QInputDialog

from .backend import FeaturesBackend
from .events import EventGate, action_key
from .runtime_local import LocalFeatures


class FeatureRuntime(LocalFeatures, QObject):
    completed = pyqtSignal(object)
    command_received = pyqtSignal(str)

    def __init__(self, controller, panel, state_dir):
        super().__init__(panel)
        self.controller, self.panel, self.root = controller, panel, state_dir
        self.cfg = copy.deepcopy(panel.config)
        self.gate, self.closed, self.generation = EventGate(), False, 0
        self.jobs = queue.Queue(maxsize=8)
        self.pending_poll = False
        self.snapshot = {}
        self.hotkey_listener = None
        self.tools = None
        self.sound = None
        self.completed.connect(self._complete)
        self.command_received.connect(self.request)
        panel.action_requested.connect(self.request)
        panel.settings_saved.connect(self.apply)
        self.worker = threading.Thread(target=self._work, daemon=True, name="桌宠扩展业务")
        self.worker.start()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.lyric_timer = QTimer(self)
        self.lyric_timer.setInterval(100)
        self.lyric_timer.timeout.connect(self._update_status)
        self._attach_menu()
        if hasattr(controller, "settings_win"):
            self.apply(self.cfg)
            self._mark_user_reply = controller.critique_ready.connect(self._protect_reply) if hasattr(controller, "critique_ready") else None

    def _protect_reply(self, _text):
        self.user_text_until = time.monotonic() + 20

    def _attach_menu(self):
        pet = self.controller.pet
        if not hasattr(pet, "_build_context_menu"):
            return
        original = pet._build_context_menu

        def extended_menu():
            menu = original()
            mirror = menu.addAction('↔ 水平镜像')
            mirror.setCheckable(True)
            mirror.setChecked(self.cfg['resources']['mirrored'])
            mirror.triggered.connect(self._set_mirrored)
            menu.addAction('查询余额').triggered.connect(lambda _checked=False: self.request('balance:refresh'))
            extension = menu.addMenu("扩展功能")
            feed = extension.addMenu("喂食互动")
            from .events import FOODS
            for key, (label, _lines) in FOODS.items():
                action = feed.addAction(label)
                action.triggered.connect(lambda _checked=False, k=key: self.request("feed:" + k))
            for label, command in (("当前天气", "weather:current"), ("音乐与歌词", "media:refresh"),
                                   ("用量统计", "usage:report"), ("睡觉", "tools:sleep"),
                                   ("唤醒", "tools:wake"), ("恢复桌宠窗口", "tools:rescue")):
                extension.addAction(label).triggered.connect(lambda _checked=False, c=command: self.request(c))
            extension.addAction("扩展设置").triggered.connect(self.controller.settings_win.open_features)
            return menu

        pet._build_context_menu = extended_menu

    def _set_mirrored(self, enabled):
        try:
            self.panel.store.update_section('resources', {'mirrored': bool(enabled)})
        except (ValueError, OSError):
            self.panel.feedback.setText('镜像设置未保存，请检查配置文件及写入权限。')
            return
        self.cfg['resources']['mirrored'] = bool(enabled)
        self.panel.config['resources']['mirrored'] = bool(enabled)
        control = self.panel.controls.get(('resources', 'mirrored'))
        if control is not None:
            control.setChecked(bool(enabled))
        pet = self.controller.pet
        pet.manual_mirrored = bool(enabled)
        pet.update()
        if hasattr(pet, 'avatar_label'):
            self._classic_overlay()

    def apply(self, cfg):
        self.cfg = copy.deepcopy(cfg)
        self.generation += 1
        self.configure_local()
        self._configure_voice()
        self._configure_hotkeys()
        if cfg["media"]["enabled"] and cfg["media"]["lyrics_enabled"]:
            self.lyric_timer.start()
        else:
            self.lyric_timer.stop()

    def _configure_voice(self):
        speaker = self.controller.speaker
        if not hasattr(speaker, "config"):
            return
        sources = speaker.config.setdefault("voice_sources", {})
        for name, section in self.cfg.items():
            if isinstance(section, dict) and "voice_enabled" in section:
                sources["feature:" + name] = {"enabled": section["voice_enabled"], "probability": section["voice_probability"]}

    def _configure_hotkeys(self):
        if hasattr(self, 'rebind_hotkeys'):
            self.rebind_hotkeys()
            return
        if self.hotkey_listener:
            self.hotkey_listener.stop()
            self.hotkey_listener = None
        if not self.cfg["hotkeys"]["enabled"]:
            return
        try:
            from pynput.keyboard import GlobalHotKeys
            mappings = {}
            for rule in self.cfg["hotkeys"]["rules"]:
                if rule.get("enabled") and rule.get("keys"):
                    if rule["keys"] in mappings or rule["keys"].casefold() in ("<ctrl>+<f1>", "<ctrl>+<f9>", "<ctrl>+<f10>", "<ctrl>+<f11>"):
                        raise ValueError("快捷键重复或与原快捷键冲突：" + rule["keys"])
                    command = rule.get("action", "phrases:preview")
                    mappings[rule["keys"]] = lambda a=command: self.command_received.emit(a)
            if mappings:
                listener = GlobalHotKeys(mappings)
                listener.start()
                self.hotkey_listener = listener
        except Exception as exc:  # noqa: BLE001 - 注册失败必须留在UI，不阻断桌宠。
            self.panel.feedback.setText("扩展快捷键未注册：" + str(exc))

    def poll(self):
        if self.closed:
            return
        if not self._blocked() and self.gate.pending and not self._text_busy():
            self._display(self.gate.flush())
        active = any(self.cfg[name]["enabled"] for name in ("system", "apps", "reminders", "phrases", "media", "weather", "agent", "usage", "balance"))
        if active and not self.pending_poll:
            self.pending_poll = self._enqueue("poll", None)

    def _enqueue(self, action, argument):
        try:
            self.jobs.put_nowait((action, argument, copy.deepcopy(self.cfg), self.generation))
            return True
        except queue.Full:
            self.panel.feedback.setText("扩展任务队列繁忙，请稍后重试。")
            return False

    def request(self, action):
        if self.closed:
            return
        handler = getattr(self, "handle_local", None)
        if handler and handler(action):
            return
        argument = None
        if action == "weather:search":
            argument, ok = QInputDialog.getText(self.panel, "添加天气城市", "城市名称（支持中文）：")
            if not ok or not argument.strip():
                return
        if action == "media:calibrate":
            seconds, ok = QInputDialog.getDouble(self.panel, "歌词时间校准", "填写偏移秒数；正数延后显示：", self.cfg["media"]["offset_seconds"], -3600, 3600, 2)
            if ok:
                self.panel.config = self.panel.store.update_section("media", {"offset_seconds": seconds})
                self.panel.refresh_section("media")
                self.apply(self.panel.config)
            return
        self.panel.feedback.setText("正在处理…")
        self._enqueue(action, argument)

    def _work(self):
        backend = FeaturesBackend(self.root)
        try:
            while True:
                job = self.jobs.get()
                if job is None:
                    return
                action, argument, cfg, generation = job
                if self.closed or generation != self.generation:
                    self.completed.emit((action, generation, {}, ""))
                    continue
                try:
                    backend.cancelled = lambda g=generation: self.closed or g != self.generation
                    data = backend.poll(cfg) if action == "poll" else backend.perform(action, cfg, argument)
                    error = ""
                except Exception as exc:  # noqa: BLE001 - 独立功能失败不能杀掉Qt或后台线程。
                    data, error = {}, "扩展操作失败：" + str(exc)
                if not self.closed:
                    self.completed.emit((action, generation, data, error))
        finally:
            backend.close()

    def _complete(self, completion):
        action, generation, data, error = completion
        bubble_result = action in ('balance:refresh', 'usage:report', 'balance:ledger', 'balance:adjust')
        if action == "poll":
            self.pending_poll = False
        if self.closed or generation != self.generation:
            return
        if error:
            if bubble_result:
                self._show_query_bubble(error)
            else:
                self.panel.feedback.setText(error)
            return
        self.snapshot.update(data.get("status", {}))
        if "media" in data.get("status", {}):
            self.media_observed_at = time.monotonic()
        if action == "poll" and data.get("status"):
            self._update_status()
        if "cities" in data:
            cities = data["cities"]
            if not cities:
                self.panel.feedback.setText("没有找到城市，请尝试拼音或更完整名称。")
                return
            labels = [f"{c['name']} · {c.get('admin1', '')} {c.get('country', '')}" for c in cities]
            selected, ok = QInputDialog.getItem(self.panel, "添加天气城市", "选择城市：", labels, 0, False)
            if ok:
                city = cities[labels.index(selected)]
                rows = list(self.cfg["weather"]["cities"])
                if not any(c["id"] == city["id"] for c in rows):
                    rows.append(city)
                self.panel.config = self.panel.store.update_section("weather", {"cities": rows})
                self.panel.refresh_section("weather")
                self.apply(self.panel.config)
                self.panel.feedback.setText("已添加城市：" + city["name"])
            return
        chosen_action = False
        for event in sorted(data.get("events", []), key=lambda e: e.get("priority", 0), reverse=True):
            event = dict(event)
            source = {"application": "apps", "reminder": "reminders"}.get(event.get("source"), event.get("source"))
            event["source"] = source
            if chosen_action:
                event["action"] = ""
            accepted = self.gate.accept(event, time.time(), dnd=self._blocked() or (self._text_busy() and not event.get("manual")))
            if accepted:
                self._display(accepted)
                chosen_action = chosen_action or bool(accepted.get("action"))
        if action != "poll":
            if bubble_result:
                self._show_query_bubble(data.get('text', '操作完成。'))
            else:
                self.panel.show_result(data.get("text", "操作完成。"))

    def _show_query_bubble(self, text):
        self.panel.feedback.setText(text)
        self._protect_reply(text)
        self.controller.pet.show_critique(text)

    def _blocked(self):
        smart = getattr(self.controller, "smart", None)
        return bool(smart and smart.blocked())

    def _busy(self):
        pet = self.controller.pet
        return bool(getattr(self.controller, "_is_analyzing", False) or getattr(pet, "is_dragging", False)
                    or getattr(pet, "feature_motion_active", False) or getattr(pet, "feature_sleeping", False)
                    or getattr(pet, "_menu_open", False) or getattr(pet, "_thinking_active", False)
                    or getattr(pet, "_loop_override", None) is True)

    def _text_busy(self):
        dialog = getattr(self.controller.pet, "interaction_dialog", None)
        return self._busy() or time.monotonic() < getattr(self, "user_text_until", 0) or bool(dialog and dialog.isVisible())

    def _display(self, event):
        if not event:
            return
        pet, text = self.controller.pet, event.get("text", "")
        if text:
            pet.show_critique(text)
            if event.get("manual"):
                self._protect_reply(text)
        if event.get("manual") and event.get("source") == "feeding":
            self._play_sound()
        if not self._busy() and event.get("action") and hasattr(pet, "play_action"):
            key = action_key(event["action"], pet.actions)
            priority = event.get("priority", 40)
            active = time.monotonic() < getattr(self, "action_until", 0)
            if key and (event.get("manual") or not active or priority >= getattr(self, "action_priority", 0)):
                pet.play_action(key, loop=False)
                self.action_priority = priority
                row = pet.actions[key]
                duration = min(30, row.get("count", 60) / max(1, row.get("base_fps", 12)))
                self.action_until = time.monotonic() + duration
        section = self.cfg.get(event.get("source"), {})
        if text and section.get("voice_enabled") and not self._blocked():
            self._configure_voice()
            self.controller.speaker.set_source("feature:" + event["source"])
            self.controller.speaker.speak_async(text)

    def _update_status(self):
        if self.closed or not self.panel.isVisible():
            return
        index = self.panel.navigation.currentRow()
        from .ui_schema import CATEGORIES
        sections = CATEGORIES[index][1]
        if "media" in sections and "media" in self.snapshot:
            media = self.snapshot["media"]
            if media.get("available"):
                def clock(seconds):
                    return "--:--" if seconds is None else f"{int(seconds)//60:02}:{int(seconds)%60:02}"
                lyric = self.snapshot.get("lyrics") or {}
                position = media.get("position")
                if position is not None and media.get("playing"):
                    position += max(0, time.monotonic() - getattr(self, "media_observed_at", time.monotonic()))
                    if media.get("duration") is not None:
                        position = min(position, media["duration"])
                from .media import lyric_at
                lyric = lyric_at(self.snapshot.get("lyric_lines", []), position, self.cfg["media"]["offset_seconds"]) or lyric
                lyric_text = html.escape(lyric.get("text", ""))
                if self.cfg["media"]["word_lyrics"] and lyric.get("words") and position is not None:
                    offset = self.cfg["media"]["offset_seconds"]
                    lyric_text = "".join(("<span style='color:#ff4e88'>" + html.escape(w["text"]) + "</span>")
                        if w["time"] + offset <= position else html.escape(w["text"]) for w in lyric["words"])
                self.panel.feedback.setText(f"{media.get('title', '')} · {media.get('artist', '')}  "
                    + clock(position) + "/" + clock(media.get("duration"))
                    + (" ▶" if media.get("playing") else " ⏸") + "<br>" + lyric_text)
            else:
                self.panel.feedback.setText("系统媒体：" + media.get("error", "暂无活动会话"))
        else:
            errors = [name + "：" + value["error"] for name, value in self.snapshot.items()
                      if name in sections and isinstance(value, dict) and value.get("error")]
            if errors:
                self.panel.feedback.setText("\n".join(errors))

    def public_summary(self):
        result = {}
        permissions = self.cfg["cloud"]
        if permissions.get("system"):
            data = self.snapshot.get("system", {})
            result["system"] = {key: data[key] for key in ("cpu", "memory", "gpu") if key in data}
        if permissions.get("weather"):
            result["weather_cities_count"] = len(self.cfg["weather"]["cities"])
        if permissions.get("usage"):
            result["usage_tracking_enabled"] = self.cfg["usage"]["enabled"]
        return result

    def close(self):
        if self.closed:
            return
        window = getattr(self.controller, "settings_win", None)
        if window is not None and self.cfg["appearance"]["enabled"] and self.cfg["appearance"]["remember_size"]:
            try:
                self.panel.store.update_section("appearance", {"width": window.width(), "height": window.height()})
            except (OSError, ValueError):
                pass  # 不让可选外观保存阻断退出。
        self.closed = True
        self.generation += 1
        self.timer.stop()
        self.lyric_timer.stop()
        if hasattr(self, "interaction"):
            self.interaction.close()
        self.controller.pet.removeEventFilter(self)
        if hasattr(self, "resource_overlay"):
            self.resource_overlay.hide()
            self._restore_classic_avatar()
        if self.hotkey_listener:
            self.hotkey_listener.stop()
        if self.tools:
            self.behavior_timer.stop()
            self.tools.close()
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
        self.jobs.put_nowait(None)
