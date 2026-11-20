"""为原经典/动态控制器追加共用语音输入，不另建对话或感知流程。"""

import logging
import threading
import time
from array import array

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from perception.window_tracker import get_foreground_window_info
from speech_models import check_ready, transcribe


def prepare_pcm(pcm, cfg):
    """16kHz int16单声道的20ms能量检测；裁剪保留两侧150ms。"""
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    voiced = []
    for start in range(0, len(samples), 320):
        frame = samples[start:start + 320]
        if frame and sum(value * value for value in frame) / len(frame) >= cfg["asr_threshold"] ** 2:
            voiced.append(start)
    if cfg["asr_filter_enabled"] and len(voiced) < 3:
        return None
    if cfg["asr_trim_enabled"] and voiced:
        begin, end = max(0, voiced[0] - 2400), min(len(samples), voiced[-1] + 320 + 2400)
        return pcm[begin * 2:end * 2]
    return pcm


class SpeechInputController(QObject):
    request_toggle = pyqtSignal()
    result_ready = pyqtSignal(int, bool, str)
    settled = pyqtSignal(bool)

    def __init__(self, original, store):
        super().__init__()
        self.original, self.store = original, store
        self.closed, self.generation = False, 0
        self.stream, self.worker = None, None
        self.chunks, self.context = [], None
        self.snapshot = None
        self.destination = None
        original._voice_recording = original._voice_transcribing = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.finish)
        self.request_toggle.connect(self.toggle)
        self.result_ready.connect(self._result)

    @property
    def busy(self):
        return self.original._voice_recording or self.original._voice_transcribing

    def status(self, text):
        self.original.pet.show()
        self.original.pet.show_critique(text)

    def toggle(self):
        original = self.original
        if self.closed:
            self.settled.emit(False)
            return
        if original._voice_recording:
            self.finish()
            return
        if self.busy or original._is_analyzing or (original.bridge and original.bridge.summary_busy):
            if not self.busy:
                self.destination = None
                self.settled.emit(False)
            self.status("正在处理上一条请求，请稍候再录音。")
            return
        desktop = getattr(original, 'desktop_control', None)
        if desktop is not None and desktop.busy and self.destination is None:
            self.status('桌面任务进行中，请使用操作窗口的麦克风或先停止任务。')
            self.settled.emit(False)
            return
        try:
            cfg = self.store.load()
            sid = original.config_mgr.get("tencent_secret_id", "").strip()
            key = original.config_mgr.get("tencent_secret_key", "").strip()
            check_ready(cfg, sid, key)
        except (ValueError, OSError) as exc:
            self.destination = None
            self.settled.emit(False)
            self.status(str(exc))
            original.show_settings()
            return
        try:
            import sounddevice as sd

            if hasattr(original, "speaker"):
                original.speaker.stop()
            self.generation += 1
            self.snapshot = cfg, sid, key
            self.context = get_foreground_window_info()
            if self.context.get("is_self"):
                self.context.update(scene="general", rect=None)
            self.chunks = []
            self.audio_bytes = 0
            original._voice_recording = True

            def collect(indata, frames, time_info, status):
                remaining = 16000 * 2 * 55 - self.audio_bytes
                if remaining > 0:
                    chunk = indata.copy().tobytes()[:remaining]
                    self.chunks.append(chunk)
                    self.audio_bytes += len(chunk)

            self.stream = sd.InputStream(samplerate=16000, channels=1, dtype="int16", callback=collect)
            self.stream.start()
            self.timer.start(55000)
            self.status("🎙 正在录音… 再按Ctrl+F1结束并识别（最长55秒）")
        except Exception:  # noqa: BLE001 -- 音频驱动及设备初始化异常统一转为可见提示。
            self.cancel()
            self.status("无法启动麦克风，请检查麦克风设备、占用情况和系统权限。")

    def toggle_for(self, callback):
        """开始前冻结目的地；另一入口结束录音也不得重定向。"""
        if self.closed:
            self.settled.emit(False)
            return
        if not self.busy:
            self.destination = callback
        self.toggle()

    def _close_stream(self):
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def finish(self):
        if self.closed or not self.original._voice_recording:
            return
        self.timer.stop()
        self.original._voice_recording = False
        try:
            self._close_stream()
        except Exception:  # noqa: BLE001 -- 音频驱动失败必须取消录音并恢复忙碌状态。
            self.cancel()
            self.status("停止麦克风失败，本次录音已取消，请重试。")
            return
        pcm, self.chunks = b"".join(self.chunks), []
        if not pcm:
            self.cancel()
            self.status("没有录到语音，请检查麦克风后重试。")
            return
        smart = getattr(self.original, "smart", None)
        if smart is not None:
            pcm = prepare_pcm(pcm, smart.cfg)
            if pcm is None:
                smart.policy.record_asr(0, time.time(), filtered=True)
                self.cancel()
                self.status("本次录音接近静音或太短，已在本地过滤，没有调用识别服务。")
                return
            seconds = len(pcm) / 32000
            if not smart.policy.asr_allow(seconds, time.time()):
                self.cancel()
                self.status("已达到今日语音识别次数或时长上限，本次没有上传。")
                return
            if smart.cfg["asr_confirm"] and QMessageBox.question(
                self.original.settings_win if hasattr(self.original, "settings_win") else None,
                "确认语音识别", f"本次将提交{seconds:.1f}秒录音到所选语音识别服务，继续吗？",
            ) != QMessageBox.StandardButton.Yes:
                self.cancel()
                self.status("已取消，本次没有调用识别服务。")
                return
            # 发送前记账；失败也消耗一次额度，不自动重试。
            smart.policy.record_asr(seconds, time.time())
        self.original._voice_transcribing = True
        generation, snapshot = self.generation, self.snapshot
        self.status("录音结束，正在使用所选语音模型识别…")

        def recognize():
            try:
                result, success = transcribe(pcm, *snapshot), True
            except Exception as exc:  # noqa: BLE001 -- 后台识别边界需始终发失败结果。
                result, success = str(exc) if isinstance(exc, ValueError) else "请检查识别服务和网络", False
            try:
                self.result_ready.emit(generation, success, result)
            except RuntimeError:
                pass

        self.worker = threading.Thread(target=recognize, daemon=True)
        self.worker.start()

    def _result(self, generation, success, text):
        if self.closed or generation != self.generation:
            return
        self.original._voice_transcribing = False
        context, self.context = self.context, None
        destination, self.destination = self.destination, None
        self.snapshot = None
        self.settled.emit(success)
        if not success:
            self.status("语音识别失败：" + text)
            return
        self.status("听到啦：" + text)
        if destination is not None:
            destination(text)
            return
        if self.original.client_mode == "classic":
            self.original.handle_user_reply(text, context)
        else:
            self.original.handle_user_reply(text)

    def cancel(self):
        self.generation += 1
        self.timer.stop()
        try:
            self._close_stream()
        except Exception as exc:  # noqa: BLE001 -- 退出时设备异常不能阻止释放其余资源。
            logging.getLogger(__name__).error("取消录音时关闭音频设备失败：%s", type(exc).__name__)
        self.original._voice_recording = self.original._voice_transcribing = False
        self.chunks, self.context, self.snapshot = [], None, None
        self.destination = None
        self.settled.emit(False)

    def shutdown(self):
        self.closed = True
        self.cancel()
