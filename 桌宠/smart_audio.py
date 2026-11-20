"""概率播报与端点限制：工作线程只合成，Qt主线程决定和播放。"""

import copy
import hashlib
import json
import logging
import os
import random
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import (
    QCoreApplication,
    QObject,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from smart_config import SmartSettingsStore

_LOG = logging.getLogger(__name__)


def _main_thread():
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def audio_devices():
    """仅在主线程读取Qt端点；失败时返回空列表，绝不猜测默认设备。"""
    if not _main_thread():
        return []
    try:
        default_id = bytes(QMediaDevices.defaultAudioOutput().id()).hex()
        return [{"id": bytes(device.id()).hex(), "name": device.description(),
                 "default": bytes(device.id()).hex() == default_id,
                 "available": not device.isNull()} for device in QMediaDevices.audioOutputs()]
    except Exception:  # noqa: BLE001 - 设备故障必须拒绝播放，不能回退默认扬声器。
        _LOG.exception("读取音频端点失败，禁止播放")
        return []


class QtOutput(QObject):
    changed = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.monitor = QMediaDevices(self)
        self.monitor.audioOutputsChanged.connect(self.changed)
        self.player = QMediaPlayer(self)
        if hasattr(self.player, "mediaStatusChanged"):
            self.player.mediaStatusChanged.connect(self._status_changed)
        # 延迟创建，避免构造无设备的默认QAudioOutput而隐式绑定扬声器。
        self.output = None

    def _status_changed(self, status):
        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self.finished.emit()

    def devices(self):
        return audio_devices()

    def play(self, path, device_id, volume):
        if not _main_thread():
            raise RuntimeError("Qt音频操作必须在主线程")
        default = QMediaDevices.defaultAudioOutput()
        devices = QMediaDevices.audioOutputs()
        selected = next((d for d in devices if bytes(d.id()).hex() == device_id and not d.isNull()), None)
        if selected is None or default.isNull() or bytes(default.id()).hex() != device_id:
            self.stop()
            raise RuntimeError("默认音频端点已变化，取消播报")
        self.player.stop()
        if self.output is None:
            self.output = QAudioOutput(selected, self)
            self.player.setAudioOutput(self.output)
        self.output.setDevice(selected)
        self.output.setVolume(max(0, min(100, volume)) / 100)
        self.player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        # 设置文件期间也可能发生端点变化，最后一次检查后才交给播放器。
        if bytes(QMediaDevices.defaultAudioOutput().id()).hex() != device_id:
            self.stop()
            raise RuntimeError("默认音频端点已变化，取消播报")
        self.player.play()

    def stop(self):
        self.player.stop()
        self.player.setSource(QUrl())


class SmartSpeaker(QObject):
    _prepare = pyqtSignal(object)
    _ready = pyqtSignal(object)
    _stop_requested = pyqtSignal()
    _submit = pyqtSignal(object)

    def __init__(self, original_speaker, config, state_dir, *, backend=None, random_fn=None):
        super().__init__()
        if not QCoreApplication.instance() or QThread.currentThread() != QCoreApplication.instance().thread():
            raise RuntimeError("智能播报须在Qt主线程创建")
        self.original = original_speaker
        self.config = SmartSettingsStore.defaults()
        self.config.update(copy.deepcopy(config))
        self.state_dir = Path(state_dir)
        self.backend = backend if backend is not None else QtOutput()
        self._random = random_fn or random.random
        self._condition = threading.Condition(threading.RLock())
        self._generation = 0
        self._source = "user"
        self._pending = None
        self._active = None
        self._closed = False
        self._blocker = lambda: False
        self._last_played = None
        self._playing = False
        self._local = threading.local()
        original_speaker._play_audio = self._capture_audio
        self._prepare.connect(self._prepare_main, Qt.ConnectionType.QueuedConnection)
        self._ready.connect(self._play_main, Qt.ConnectionType.QueuedConnection)
        self._stop_requested.connect(self._stop_main, Qt.ConnectionType.QueuedConnection)
        self._submit.connect(self._submit_main, Qt.ConnectionType.QueuedConnection)
        if hasattr(self.backend, "changed"):
            self.backend.changed.connect(self.devices_changed)
        if hasattr(self.backend, "finished"):
            self.backend.finished.connect(self._playback_finished)
        self._device_snapshot = self._snapshot()
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(250)
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start()
        self.worker = threading.Thread(target=self._work, daemon=True, name="智能播报合成")
        self.worker.start()

    @property
    def enabled(self):
        return self.original.enabled

    @enabled.setter
    def enabled(self, value):
        self.original.enabled = bool(value)
        if not value:
            self.stop()

    def update_config(self, *args, **kwargs):
        self.stop()
        self.original.update_config(*args, **kwargs)

    def toggle(self):
        self.enabled = not self.enabled
        return self.enabled

    def set_source(self, source):
        self._source = str(source)

    def set_blocker(self, callback):
        self._blocker = callback
        self._poll()

    def apply_config(self, cfg):
        self.stop()
        self.config = SmartSettingsStore.defaults()
        self.config.update(copy.deepcopy(cfg))

    def _device(self):
        try:
            rows = self.backend.devices()
            default = next((row for row in rows if row["default"] and row["available"]), None)
            if not default:
                return None
            if self.config["device_filter_enabled"] and default["id"] not in self.config["allowed_devices"]:
                return None
            return default["id"]
        except Exception:  # noqa: BLE001 - 任意设备提供器故障均拒绝播报。
            _LOG.exception("检查默认音频设备失败，禁止播放")
            return None

    def _permitted(self, job):
        try:
            return (not self._closed and self.enabled and self.config["volume"] > 0
                    and job["generation"] == self._generation and not self._blocker()
                    and self._device() == job["device"])
        except Exception:  # noqa: BLE001 - 外部免打扰回调故障也必须拒绝播报。
            _LOG.exception("检查播报条件失败，禁止播放")
            return False

    def speak_async(self, text):
        source, self._source = self._source, "user"
        if not _main_thread():
            self._submit.emit((text, source, self._generation))
            return
        self._speak_main(text, source)

    def play_local_file(self, path, volume):
        """本地音效共用设备与单通道；不发TTS请求、不抢占现有播报。"""
        if not _main_thread() or self._closed:
            return False
        path = Path(path)
        if not path.is_file() or path.suffix.lower() not in (".wav", ".mp3", ".ogg", ".m4a"):
            return False
        device = self._device()
        job = {"generation": self._generation, "device": device}
        with self._condition:
            if not device or self._active or self._pending or self._playing or not self._permitted(job):
                return False
            volume = max(0, min(int(volume), int(self.config["volume"])))
            if not volume:
                return False
            try:
                self.backend.play(str(path), device, volume)
            except Exception:  # noqa: BLE001 - 本地坏音频不能切回其他播放器。
                _LOG.exception("本地音效播放失败")
                return False
            self._playing = True
            self._last_played = time.monotonic()
            return True

    @pyqtSlot(object)
    def _submit_main(self, submission):
        text, source, generation = submission
        if generation == self._generation:
            self._speak_main(text, source)

    def _speak_main(self, text, source):
        if not text or self._closed or not self.enabled:
            return
        device = self._device()
        if device is None:
            self.stop()
            return
        cfg = self.config
        job = {"text": text, "source": source, "device": device, "generation": self._generation}
        if not self._permitted(job):
            self.stop()
            return
        if cfg["voice_rules_enabled"]:
            rule = cfg["voice_sources"].get(source, {"enabled": False, "probability": 0})
            if not rule["enabled"] or self._random() * 100 >= rule["probability"]:
                return
            if (cfg["voice_interval_enabled"] and source not in ("user", "manual")
                    and self._last_played is not None
                    and time.monotonic() - self._last_played < cfg["voice_interval_seconds"]):
                return
        if cfg["clean_speech"]:
            text = self.original._clean_text_for_speech(text)
            text = re.sub(r"!?\[([^\]]*)\]\(https?://[^\s)]+\)", r"\1", text)
            text = re.sub(r"\[[a-zA-Z_ -]+\]", "", text)
            text = re.sub(r"https?://[^\s，。！？；、]+", "", text).strip()
        if cfg["short_speech"]:
            text = text[:cfg["speech_chars"]]
        if not text.strip():
            return
        with self._condition:
            if self._active or self._pending or self._playing:
                if source not in ("user", "manual") or not cfg["user_interrupt"]:
                    return
                self.stop()
            job.update(text=text, generation=self._generation)
            job["cache"] = cfg["voice_cache_enabled"]
            settings = {key: getattr(self.original, key, "") for key in (
                "voice", "rate", "pitch", "engine_type", "api_url", "model",
                "gemini_voice", "gemini_model", "api_key", "gemini_api_key")}
            job["key"] = hashlib.sha256(json.dumps([text, settings], sort_keys=True).encode("utf-8")).hexdigest()
            self._pending = job
            self._condition.notify()

    def stop(self):
        with self._condition:
            self._generation += 1
            self._pending = None
            self.original.stop()
            self._condition.notify()
        if QThread.currentThread() == self.thread():
            self._stop_main()
        else:
            self._stop_requested.emit()

    @pyqtSlot()
    def _stop_main(self):
        self.backend.stop()
        self._playing = False

    @pyqtSlot()
    def _playback_finished(self):
        self._playing = False

    @pyqtSlot()
    def devices_changed(self):
        self._device_snapshot = self._snapshot()
        self.stop()

    def _snapshot(self):
        try:
            return tuple(sorted((r["id"], r["default"], r["available"]) for r in self.backend.devices()))
        except Exception:  # noqa: BLE001 - 设备故障按空快照处理，触发取消。
            _LOG.exception("读取音频设备快照失败，禁止播放")
            return ()

    def _poll(self):
        snapshot = self._snapshot()
        if snapshot != self._device_snapshot:
            self.devices_changed()
        try:
            muted = not self.enabled or self._blocker() or self.config["volume"] <= 0
        except Exception:  # noqa: BLE001 - 外部免打扰回调失败视为静音。
            _LOG.exception("检查免打扰失败，禁止播放")
            muted = True
        if muted and (self._active or self._pending or self._playing):
            self.stop()

    @pyqtSlot(object)
    def _prepare_main(self, request):
        job, event = request
        job["allowed"] = self._permitted(job)
        event.set()

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._pending is not None)
                if self._closed:
                    return
                job, self._pending = self._pending, None
                self._active = job
            event = threading.Event()
            self._prepare.emit((job, event))
            event.wait(3)
            try:
                if job.get("allowed") and job["generation"] == self._generation:
                    self._local.job = job
                    cached = self._cache_hit(job)
                    if cached:
                        self._ready.emit((job, str(cached)))
                    else:
                        with self._condition:
                            generation = self.original._cancel_generation
                            current = job["generation"] == self._generation
                        if current:
                            self.original._run_tts_and_play(job["text"], generation)
            except Exception:  # noqa: BLE001 - 工作线程异常不允许转入原MCI播放。
                _LOG.exception("准备音频失败，取消本次播报")
            finally:
                self._local.job = None
                with self._condition:
                    self._active = None

    def _capture_audio(self, path):
        job = getattr(self._local, "job", None)
        if job and job["generation"] == self._generation:
            directory = self.state_dir / ("voice_cache" if job["cache"] else "voice_playback")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / ((job["key"] if job["cache"] else uuid.uuid4().hex) + Path(path).suffix)
            temporary = target.with_name(target.name + ".tmp")
            try:
                shutil.copyfile(path, temporary)
                os.replace(temporary, target)
                self._trim(directory, 100 if job["cache"] else 3)
                self._ready.emit((job, str(target)))
            finally:
                temporary.unlink(missing_ok=True)

    def _cache_hit(self, job):
        if job["cache"]:
            for path in (self.state_dir / "voice_cache").glob(job["key"] + ".*"):
                if path.suffix in (".mp3", ".wav") and path.stat().st_size:
                    os.utime(path, None)
                    return path
        return None

    @staticmethod
    def _trim(directory, limit):
        files = sorted((p for p in directory.iterdir() if p.suffix in (".mp3", ".wav")),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[limit:]:
            try:
                path.unlink()
            except OSError:
                pass

    @pyqtSlot(object)
    def _play_main(self, result):
        job, path = result
        if self._permitted(job):
            try:
                self.backend.play(path, job["device"], self.config["volume"])
                self._last_played = time.monotonic()
                self._playing = True
            except Exception:  # noqa: BLE001 - 播放器故障统一停播，不回退其他设备。
                _LOG.exception("受限音频播放失败，取消本次播报")
                self.stop()

    def shutdown(self):
        self.stop()
        self._poll_timer.stop()
        with self._condition:
            self._closed = True
            self._condition.notify()
