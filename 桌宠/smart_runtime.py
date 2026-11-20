"""本地事件门控与截图任务；只有通过门控才请求模型。"""

import copy
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from perception.screen_capture import capture_screen_jpeg
from perception.window_tracker import get_foreground_window_info
from smart_config import SmartSettingsStore
from smart_models import SmartModelClient, SmartModelError
from smart_policy import SmartPolicy, dnd_active, scene_key
from smart_windows import ForegroundWatcher, microphone_active


class SmartCompanion(QObject):
    completed = pyqtSignal(object)
    usage_changed = pyqtSignal()
    engine_failed = pyqtSignal(int, int, str)

    def __init__(self, original, panel, state_dir):
        super().__init__(original)
        self.original, self.panel = original, panel
        self.cfg = copy.deepcopy(panel.config)
        self.policy = SmartPolicy(self.cfg, Path(state_dir) / "smart_state.json")
        self.generation, self.closed, self.worker = 0, False, None
        self.last_interaction = time.time()
        self._mic_active = microphone_active() if self.cfg["meeting_enabled"] else False
        self._owns_busy = False
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.watcher = ForegroundWatcher(self)
        self.watcher.changed.connect(self.poll)
        self.completed.connect(self._complete)
        self.usage_changed.connect(lambda: self._status("模型用量已更新"))
        self.engine_failed.connect(self._engine_failed)
        panel.settings_saved.connect(self.apply)
        original.speaker.set_blocker(self.blocked)
        self.apply(self.cfg)

    def blocked(self):
        return dnd_active(self.cfg) or (self.cfg["meeting_enabled"] and (
            self._mic_active or getattr(self.original, "_voice_recording", False)))

    def apply(self, config):
        self.generation += 1
        SmartSettingsStore.validate(config)
        self.cfg = copy.deepcopy(config)
        self.policy.cfg = self.cfg
        self.policy.current, self.policy.consumed = None, False
        if self._owns_busy:
            self.original._is_analyzing = False
            self._owns_busy = False
        # 原用户worker仍持有busy至退出，但其旧思考动画现在就要取消。
        self._thinking(False)
        self.original.speaker.apply_config(self.cfg)
        self.watcher.stop()
        if self.closed:
            return
        if self.cfg["enabled"]:
            self.original.monitor_timer.stop()
            if self.cfg["events_enabled"]:
                self.watcher.start()
        else:
            self.original.monitor_timer.start(2500)
        # 低成本本地检测也负责麦克风/免打扰停播；此定时器不直接调用模型。
        self.timer.start()
        self._status("智能模式已启用" if self.cfg["enabled"] else "原固定巡逻模式；播报规则可独立生效")

    def _thinking(self, value):
        if hasattr(self.original.pet, "set_thinking"):
            self.original.pet.set_thinking(value)

    def _status(self, reason):
        with self.policy.lock:
            state = copy.deepcopy(self.policy.state)
        usage = str(state["tokens"]) + ("（有未知用量）" if state["unknown_usage"] else "")
        self.panel.status_label.setText(
            f"{reason}\n模型请求（近24小时）：{len(state['calls'])}；今日Token：{usage}；"
            f"已知输入/输出：{state['input_tokens']}/{state['output_tokens']}；"
            f"语音识别：{state['asr_calls']}次 / {state['asr_seconds']:.1f}秒；过滤：{state['asr_filtered']}次")
        last = state.get("last_request", {})
        if last:
            thinking = f"；思考{last['reasoning_tokens']} Token" if "reasoning_tokens" in last else ""
            reason = last.get("finish_reason")
            ending = f"；结束：{'额度耗尽' if reason in ('length', 'MAX_TOKENS') else reason}" if reason else ""
            self.panel.status_label.setText(self.panel.status_label.text() +
                f"\n最近请求：{last.get('kind', '未知')}，输入{last.get('input_tokens', '未知')} / "
                f"输出{last.get('output_tokens', '未知')} Token{thinking}{ending}；人设{last.get('system_chars', '未知')}字符，"
                f"附加上下文{last.get('context_chars', '未知')}字符；截图{'有' if last.get('has_image') else '无'}。"
                "字符数不是Token，图片Token以服务商usage为准。")

    def poll(self):
        if self.closed:
            return
        desktop = getattr(self.original, 'desktop_control', None)
        if desktop is not None and desktop.busy:
            return
        self._mic_active = microphone_active() if self.cfg["meeting_enabled"] else False
        if self.blocked():
            self.original.speaker.stop()
            self._status("免打扰或麦克风使用中")
            return
        if not self.cfg["enabled"]:
            self._status("原固定巡逻模式；播报规则可独立生效")
            return
        if not self.original.auto_mode:
            self._status("原自动感知总开关已暂停")
            return
        if self.original._is_analyzing or self.original.voice_input.busy or self.original.bridge.summary_busy:
            return
        info, now = get_foreground_window_info(), time.time()
        eligible, reason = self.policy.observe(info, now)
        if eligible and self.cfg["events_enabled"]:
            self.request(info, "passive")
        elif self.cfg["icebreaker_enabled"] and now - self.last_interaction >= self.cfg["icebreaker_minutes"] * 60:
            allowed, why = self.policy.eligible(info, now)
            stable = not self.cfg["debounce_enabled"] or now - self.policy.since >= self.cfg["debounce_seconds"]
            if allowed and stable and info.get("title") and not info.get("is_self"):
                self.request(info, "icebreaker")
            else:
                self._status(why or reason)
        else:
            self._status(reason if self.cfg["events_enabled"] else "场景事件调用已关闭")

    def generate(self, info, image, kind="passive"):
        if kind == "manual" and not self._request_cfg["enabled"]:
            method = self.original.engine.generate_critique
            method = getattr(method, "original_method", method)
            return {"text": method(
                info.get("scene", "general"), info.get("title", ""), info.get("process", ""), image),
                "usage_known": False}
        return SmartModelClient(self.original, self._request_cfg, self._bridge_cfg).generate(info, image, kind)

    def generate_metered(self, info, image, kind, *, user_text="", bridge_config=None):
        """原互动/总结worker共用；网络在锁外，用量提交线程安全。"""
        generation = self.generation
        client = SmartModelClient(self.original, copy.deepcopy(self.cfg),
                                  copy.deepcopy(bridge_config if bridge_config is not None else self.original.bridge.config))
        client.validate_configuration(kind)
        if self.closed or generation != self.generation:
            raise ValueError("配置已变更，本次请求已取消")
        self.policy.reserve(info, time.time(), kind=kind, check_budget=True)
        result, success = {}, False
        try:
            result = client.generate(info, image, kind, user_text=user_text)
            success = True
        except SmartModelError as exc:
            result = exc.result
            raise
        finally:
            self.policy.complete(result, success, time.time())
            self.usage_changed.emit()
        if self.closed or generation != self.generation:
            raise ValueError("配置已变更，已丢弃旧模型回复")
        return result

    def generate_engine(self, arguments, kind):
        info = {"scene": arguments.get("scene", "general"), "title": arguments.get("window_title", ""),
                "process": arguments.get("process_name", "")}
        comment = arguments.get("user_comment", "")
        generation = self.generation
        epoch = self.original.bridge.request_epoch
        try:
            image = arguments.get("image_b64")
            if not image:
                capture = capture_screen_jpeg(max_width=1280, quality=80)
                if not capture or not capture[1]:
                    raise ValueError("未能截取当前画面，本次没有调用模型")
                image = capture[1]
            try:
                result = self.generate_metered(info, image, kind, user_text=comment)
            finally:
                if kind == "user":
                    self.original.engine.context_memory.record_user_comment(info["scene"], info["title"], comment)
            self.original.engine.context_memory.record_critique(
                info["scene"], info["title"], result["text"], self.original.config_mgr.get("pet_name", "哈Gemi"))
            if kind == 'passive':
                self.original.bridge.observation_ready.emit({**info, 'text': result['text'], 'observed_at': time.time()})
            return result["text"]
        except Exception as exc:
            self.engine_failed.emit(generation, epoch, str(exc) if isinstance(exc, ValueError) else "模型请求失败，请检查网络和配置")
            raise

    def _engine_failed(self, generation, epoch, message):
        if not self.closed and generation == self.generation and epoch == self.original.bridge.request_epoch:
            self.original._is_analyzing = False
            self._thinking(False)
            self.original.pet.show_critique(message)
            self.original.logger.error('锐评/互动模型失败：' + message)
            self._status(message)

    def request_manual(self):
        info = get_foreground_window_info()
        if info.get("is_self"):
            info.update(scene="general", rect=None)
        return self.request(info, "manual")

    def request(self, info, kind="passive"):
        desktop = getattr(self.original, 'desktop_control', None)
        if desktop is not None and desktop.busy:
            return False
        if self.closed or self.original._is_analyzing or (self.worker and self.worker.is_alive()):
            return False
        metered = kind != "manual" or self.cfg["enabled"]
        if metered:
            try:
                SmartModelClient(self.original, self.cfg, self.original.bridge.config).validate_configuration()
            except ValueError as exc:
                self.policy.consumed, self.policy.failed_at = True, time.time()
                self.original.pet.show_critique(str(exc))
                self.original.logger.error('锐评配置检查失败：' + str(exc))
                self._status(str(exc))
                return False
        # 同步捕获此刻画面，后台只进行网络调用，不能延后截图。
        try:
            capture = capture_screen_jpeg(max_width=1280, quality=80, crop_rect=info.get("rect"))
            if not capture or not capture[1]:
                raise ValueError("未能截取当前画面，本次没有调用模型")
        except Exception as exc:  # noqa: BLE001 -- 截图失败不得无图继续付费请求。
            self.original.pet.show_critique(str(exc))
            return False
        if metered:
            try:
                self.policy.reserve(info, time.time(), kind=kind, check_budget=True)
            except ValueError as exc:
                self.original.pet.show_critique(str(exc))
                self._status(str(exc))
                return False
        self.last_interaction = time.time()
        self.generation += 1
        generation = self.generation
        self._request_cfg = copy.deepcopy(self.cfg)
        self._bridge_cfg = copy.deepcopy(self.original.bridge.config)
        self.original.bridge.request_epoch += 1
        self.original._is_analyzing, self._owns_busy = True, True
        self._thinking(True)
        image, snapshot = capture[1], copy.deepcopy(info)
        snapshot["_metered"] = metered
        self._status("正在分析当前截图（手动）" if kind == "manual" else "本地门控通过，正在分析截图")

        def work():
            try:
                result, success = self.generate(snapshot, image, kind), True
            except SmartModelError as exc:
                result, success = dict(exc.result, error=str(exc)), False
            except Exception as exc:  # noqa: BLE001 -- 线程边界必须释放思考状态，不自动重试。
                result, success = {"error": str(exc) if isinstance(exc, ValueError) else "模型请求失败，请检查网络和配置"}, False
            try:
                self.completed.emit((generation, snapshot, kind, result, success))
            except RuntimeError:
                pass

        self.worker = threading.Thread(target=work, daemon=True, name="智能视觉请求")
        self.worker.start()
        return True

    def _complete(self, payload):
        generation, info, kind, result, success = payload
        if info.get("_metered", kind != "manual"):
            self.policy.complete(result, success, time.time())
        if self.closed or generation != self.generation:
            return
        self.original._is_analyzing, self._owns_busy = False, False
        self._thinking(False)
        if not success:
            # 失败必须可见，不与换窗口/免打扰导致的过期成功回复一起丢弃。
            self.original.pet.show_critique(result['error'])
            self.original.logger.error('锐评模型失败：' + result['error'])
            self._status(result['error'])
            return
        if kind != "manual" and self.blocked():
            self._status("免打扰或麦克风使用中，已丢弃在途自主回复（用量仍记账）")
            return
        if kind != "manual" and scene_key(get_foreground_window_info(), self.cfg["normalize_title"]) != scene_key(info, self.cfg["normalize_title"]):
            self._status("前台场景已改变，已丢弃过期回复（用量仍记账）")
            return
        if success:
            if info.get('_metered', kind != 'manual'):
                self.original.bridge.record_observation({**info, 'text': result['text'], 'observed_at': time.time()})
            self.original.tag_reply(result["text"], kind)
            self.original.critique_ready.emit(result["text"])
            if info.get("_metered", kind != "manual"):
                self.original.engine.context_memory.record_critique(
                    info.get("scene", "general"), info.get("title", ""), result["text"],
                    self.original.config_mgr.get("pet_name", "哈Gemi"))
            self._status("本次已完成，等待新变化")

    def shutdown(self):
        self.closed = True
        self.generation += 1
        self.timer.stop()
        self.watcher.stop()
