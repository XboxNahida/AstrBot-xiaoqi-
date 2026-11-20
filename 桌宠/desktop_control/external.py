"""云端原子操作接入Qt移动流程；不调用本地规划模型。"""

import threading
import time

from PyQt6.QtCore import QEventLoop, QObject, QThread, QTimer, pyqtSignal

from .windows import validate_action


class ExternalTools(QObject):
    requested = pyqtSignal(object)
    completed = pyqtSignal(object)
    aborted = pyqtSignal(object)

    def __init__(self, control):
        super().__init__(control)
        self.control = control
        self.pending = None
        self.requested.connect(self._receive)
        self.completed.connect(self._complete)
        self.aborted.connect(self._abort)

    def call(self, name, payload, cancel=None, deadline=None):
        request = {
            "name": name,
            "payload": dict(payload or {}),
            "cancel": cancel if cancel is not None else threading.Event(),
            "deadline": deadline if deadline is not None else time.time() + 20,
            "done": threading.Event(),
        }
        if name not in ("begin", "stop", "status"):
            request["expected_session"] = self.control.tools.call("status")[
                "session_id"
            ]
        on_gui = QThread.currentThread() == self.thread()
        if on_gui:
            self._receive(request)
        else:
            self.requested.emit(request)
        if on_gui and not request["done"].is_set():
            loop, timer = QEventLoop(), QTimer()
            timer.timeout.connect(
                lambda: loop.quit() if request["done"].is_set() else self.tick()
            )
            timer.start(10)
            loop.exec()
            timer.stop()
        while not request["done"].wait(0.02):
            if time.time() >= request["deadline"] or request["cancel"].is_set():
                request["cancel"].set()
                self.aborted.emit(request)
                if not request["done"].wait(1):
                    raise ValueError("cancelled")
        if "error" in request:
            raise request["error"]
        return request["result"]

    @staticmethod
    def _check(request):
        if request["cancel"].is_set():
            raise ValueError("cancelled")
        if time.time() >= request["deadline"]:
            raise ValueError("expired")

    def _receive(self, request):
        control = self.control
        try:
            self._check(request)
            if control.closed:
                raise ValueError("closed")
            name, payload = request["name"], request["payload"]
            if name == "status":
                request["result"] = control.tools.call(name, payload)
                request["done"].set()
                return
            if name == "stop":
                result = control.tools.stop()
                control.stop()
                request["result"] = result
                request["done"].set()
                return
            if self.pending is not None or control.state not in ("idle", "finished"):
                raise ValueError("busy")
            if name == "begin":
                request["result"] = {"session_id": control.tools.begin("cloud")}
                control._external_active = True
                control._claim(True)
                control._exclude_own_windows()
                request["done"].set()
                return
            control.tools._authorize()
            state = control.tools.call("status")
            if not state["active"] or state["source"] != "cloud":
                raise ValueError("inactive")
            if state["session_id"] != request["expected_session"]:
                raise ValueError("session_changed")
            request["session"] = state["session_id"]
            if name in control.tools.INPUTS:
                validate_action(dict(payload, name=name))
                with control.tools._lock:
                    cached = control.tools._results.get(payload.get("request_id"))
                    if cached is not None:
                        request["result"] = dict(cached)
                        request["done"].set()
                        return
                if (
                    not isinstance(payload.get("request_id"), str)
                    or not payload["request_id"]
                ):
                    raise ValueError("invalid_request_id")
            self.pending = request
            request["action"] = dict(payload, name=name)
            if (
                name in control.tools.INPUTS
                and "x" in payload
                and control.panel.config["move_pet"]
            ):
                control._approach(request["action"])
            else:
                self.execute()
        except Exception as exc:
            if self.pending is request:
                self._finish(request, error=exc)
            else:
                request["error"] = exc
                request["done"].set()

    def execute(self):
        request, control = self.pending, self.control
        if request is None:
            return
        try:
            self._check(request)
            control._hide_capture()

            def run():
                try:
                    self._check(request)
                    state = control.tools.call("status")
                    if not state["active"] or state["session_id"] != request["session"]:
                        raise ValueError("session_changed")
                    # 保留原观察号和输入号；Tools仍实时核对目标，不能刷新ID绕过复核。
                    result = control.tools.call(request["name"], request["payload"])
                    error = None
                except Exception as exc:
                    result, error = None, exc
                try:
                    self.completed.emit((request, result, error))
                except RuntimeError:
                    request["error"] = ValueError("closed")
                    request["done"].set()

            threading.Thread(target=run, name="云端桌面原子操作", daemon=True).start()
        except Exception as exc:
            self._finish(request, error=exc)

    def _complete(self, value):
        request, result, error = value
        if self.pending is request:
            self._finish(request, result, error)

    def _finish(self, request, result=None, error=None):
        self.pending = None
        control = self.control
        control._end_motion()
        control._restore_capture()
        control.state = "idle"
        request["result"] = result
        if error is not None:
            request["error"] = error
        request["done"].set()

    def cancel(self):
        request = self.pending
        if request is not None:
            request["cancel"].set()
            self._finish(request, error=ValueError("cancelled"))

    def _abort(self, request):
        if self.pending is request:
            self.control.tools.stop()
            self._finish(request, error=ValueError("cancelled"))

    def tick(self):
        if self.pending is None:
            return
        try:
            self._check(self.pending)
            self.control.tools._authorize()
        except Exception as exc:
            self.control.tools.stop()
            self._finish(self.pending, error=exc)
