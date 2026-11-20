"""给原控制器增加连接能力，共享原语音，不启动第二套感知/交互系统。"""

import copy
import re
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from brain.prompts import get_system_prompt
from companion_link import DeviceLink
from companion_main import CompanionController
from companion_models import DirectModels


class OriginalCloudBridge(CompanionController):
    observation_ready = pyqtSignal(dict)
    request_completed = pyqtSignal(str, str)
    request_failed = pyqtSignal(str, str, int)
    request_cancelled = pyqtSignal(int)

    def __init__(self, original, settings, *, state_dir):
        # 仅复用协议与汇报实现，不调用简化桌宠的装配构造函数。
        QObject.__init__(self)
        self.original = original
        self.pet, self.settings = original.pet, settings
        self.speaker = original.speaker
        self.config = copy.deepcopy(settings.config)
        self.state_dir = Path(state_dir)
        self.started_at = time.time()
        self.closed = False
        self.summary_busy = False
        self.request_epoch = 0
        self.link_status = "未配对"
        self.link = DeviceLink(self.state_dir / "companion_state.sqlite")
        self.link.status_changed.connect(self.set_link_status)
        self.link.command_received.connect(self.handle_command)
        self.link.report_sent.connect(self._report_acknowledged)
        settings.settings_saved.connect(self.apply_settings)
        settings.send_message.connect(self.chat)
        settings.request_report.connect(self.report)
        settings.request_critique.connect(original.trigger_manual_critique)
        self.summary_ready.connect(self._summary_result)
        self.request_completed.connect(self._report_completion)
        self.request_failed.connect(self._show_request_failure)
        self.request_cancelled.connect(self._finish_cancelled)
        self.observation_ready.connect(self.record_observation)
        original.engine.observation_callback = self.observation_ready.emit
        original.critique_ready.connect(self._append_original_reply)
        self.report_timer = QTimer(self)
        self.report_timer.timeout.connect(lambda: self.report(False, automatic=True))
        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self.write_runtime)
        self.runtime_timer.start(5000)
        self.apply_settings(self.config)

    def _report_acknowledged(self, _identity):
        if not self.closed:
            self.settings.report_log.appendPlainText("云端已确认收到汇报。")

    def _append_original_reply(self, text):
        if not self.closed:
            self.settings.append_chat("桌宠", text)

    @property
    def busy(self):
        return self.original._is_analyzing or getattr(self.original, "_voice_recording", False) or getattr(self.original, "_voice_transcribing", False)

    def apply_settings(self, config):
        previous = self.config.get("cloud", {})
        self.config = copy.deepcopy(config)
        snapshot = copy.deepcopy(config)
        scope = DeviceLink.scope_for(snapshot["cloud"])
        self.original.engine.observation_callback = lambda data: self.observation_ready.emit({**data, '_scope': scope})

        def transport(system, prompt, image, pet_name):
            epoch = self.request_epoch
            if self.closed:
                raise ValueError("桌宠正在退出")
            try:
                answer = self._transport(prompt, snapshot, system, image)
            except Exception as exc:
                if not self.closed:
                    if scope != self.link.journal.scope:
                        self.request_cancelled.emit(epoch)
                    else:
                        hint = str(exc) if isinstance(exc, ValueError) else "请检查模型配置和网络"
                        self.request_failed.emit("请求失败：" + hint, scope, epoch)
                raise ValueError("扩展模型请求未完成") from exc
            if self.closed or scope != self.link.journal.scope:
                if not self.closed:
                    self.request_cancelled.emit(epoch)
                raise ValueError("连接已变更，已丢弃旧请求")
            self.request_completed.emit(answer, scope)
            return answer

        self.original.engine.request_handler = None if config["route"] == "original" else transport
        self.report_timer.start(self.config["report_interval"] * 1000)
        if previous != config["cloud"] or not self.link.thread:
            try:
                self.link.start(config["cloud"])
            except ValueError as exc:
                self.set_link_status(str(exc))
        self.write_runtime()

    def _transport(self, text, config, system, image=None):
        if config["route"] == "cloud":
            if image:
                return self.link.rpc('qa', {'system': system, 'messages': [{'role': 'user', 'content': [
                    {'type': 'text', 'text': text}, {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image}}
                ]}]}, expected_scope=DeviceLink.scope_for(config['cloud']))['text']
            # 无截图的原互动继续沿用已有WebChat文字会话。
            return self.link.chat(
                "桌宠的人设与表达要求：\n" + system + "\n\n桌宠请求（只有文字观察）：\n" + text,
                expected_scope=DeviceLink.scope_for(config["cloud"]),
            )
        if config["route"] == "direct":
            profile = next(p for p in config["profiles"] if p["id"] == config["active_profile"])
            return DirectModels(profile).chat(text, system_prompt=system, image_b64=image)
        # 总结也能使用原版的全部服务商，不改变原模型配置。
        engine = self.original.engine
        dispatch = {
            "openai_compatible": engine._call_openai_compatible,
            "vertex": engine._call_vertex_ai,
            "antigravity": engine._call_antigravity,
            "gemini": engine._call_gemini_native,
        }
        if engine.provider not in ("vertex", "antigravity") and not engine.api_key:
            raise ValueError("请先在核心与感知页配置原版模型")
        answer = dispatch[engine.provider](system, text, image, engine.config_mgr.get("pet_name", "哈Gemi"))
        if answer.startswith('[relieved] 喵呜！'):
            raise ValueError(answer.removeprefix('[relieved] 喵呜！'))
        return answer

    def _generate(self, text, config, history, *, image_b64=None):
        smart = getattr(self.original, "smart", None)
        if smart is not None and smart.cfg["enabled"]:
            return smart.generate_metered({}, image_b64, "summary", user_text=text, bridge_config=config)["text"]
        cfg = self.original.config_mgr
        system = get_system_prompt(cfg.get("pet_name"), cfg.get("catchphrases"), cfg.get("custom_system_prompt", ""), persona_style=cfg.get("persona_style"))
        return self._transport(text, config, system, image_b64)

    def report(self, summarize=False, automatic=False):
        if summarize and not self.summary_busy and not self.busy and self.config["cloud"].get("token"):
            smart = getattr(self.original, "smart", None)
            self._summary_generation = smart.generation if smart is not None else None
        super().report(summarize, automatic=automatic)

    def _summary_result(self, facts, summary, error, scope=None, is_summary=True):
        smart = getattr(self.original, "smart", None)
        generation = getattr(self, "_summary_generation", None)
        if is_summary and generation is not None and smart is not None and generation != smart.generation:
            self.summary_busy = False
            self.settings.report_log.appendPlainText("配置已变更，已丢弃旧模型总结。")
            return
        super()._summary_result(facts, summary, error, scope, is_summary)

    def chat(self, text):
        if self.busy or self.summary_busy:
            self.settings.feedback.setText("原版正在处理请求，请稍候。")
        elif not text.strip() or len(text) > 16000:
            self.settings.feedback.setText("请输入1至16000字的消息。")
        else:
            self.settings.append_chat("我", text)
            self.original.handle_user_reply(text)

    def display(self, text, emotion="confident"):
        raw = text if text.lstrip().startswith("[") else f"[{emotion}] {text}"
        if hasattr(self.original, "tag_reply"):
            self.original.tag_reply(raw, "cloud")
        self.original._on_critique_ready(text if text.lstrip().startswith("[") else f"[{emotion}] {text}")

    def facts(self):
        facts = super().facts()
        facts.update(client_mode=self.original.client_mode, original_program=True, auto_mode=self.original.auto_mode, tts_enabled=self.speaker.enabled)
        features = getattr(self.original, "features", None)
        if features is not None and features.cfg["cloud"]["enabled"]:
            facts["features"] = features.public_summary()
        return facts

    @staticmethod
    def factual_summary(facts):
        if facts["language_route"] == "original":
            return f"原版桌宠运行{facts['uptime_seconds']}秒，语言请求沿用原版通道。"
        return CompanionController.factual_summary(facts)

    def _report_completion(self, reply, scope):
        if not self.closed and scope == self.link.journal.scope and self.config["cloud"].get("token"):
            self.link.report({"source": "desktop_client", "event": "conversation_completed", "observed_at": time.time(), "reply_excerpt": reply[:400]}, "原版桌宠完成一轮模型请求；以下是回复节选，不是完整会话同步。")

    def record_observation(self, data):
        if self.closed or not self.config['cloud'].get('token'):
            return
        if data.get('_scope', self.link.journal.scope) != self.link.journal.scope:
            return
        text = re.sub(r'^\s*\[[a-zA-Z_]+\]\s*', '', data.get('text', '')).strip()
        if not text:
            return
        title = re.sub(r'\s+', ' ', data.get('title', ''))[:100]
        self.link.report({'_record_type': 'observation', 'observed_at': data.get('observed_at', time.time()),
                          'text': (title + '：' + text if title else text)[:1000]})

    def _finish_cancelled(self, epoch):
        if self.closed or epoch != self.request_epoch:
            return
        if self.busy:
            # 等原worker释放忙碌状态；新请求会推进epoch，使旧取消失效。
            QTimer.singleShot(25, lambda: self._finish_cancelled(epoch))
        else:
            self.pet.set_thinking(False)

    def _show_request_failure(self, message, scope, epoch):
        if self.closed or epoch != self.request_epoch:
            return
        if scope != self.link.journal.scope:
            self._finish_cancelled(epoch)
            return
        if self.busy:
            QTimer.singleShot(25, lambda: self._show_request_failure(message, scope, epoch))
            return
        self.settings.feedback.setText(message)
        self.original.pet.set_thinking(False)
        # 仅显示错误，不经过引擎记忆写入或成功汇报。
        self.original.logger.error(message)
        self.settings.append_chat('请求失败', message)
        self.original.pet.show_critique(message)

    def write_runtime(self):
        self.config.update(tts_enabled=self.speaker.enabled, auto_mode=self.original.auto_mode)
        super().write_runtime()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.original.critique_ready.disconnect(self._append_original_reply)
        self.link.report_sent.disconnect(self._report_acknowledged)
        self.report_timer.stop()
        self.runtime_timer.stop()
        self.link.stop()
        self.link.journal.close()
        self.write_runtime()
