"""小七双向桌宠入口。保留上游动态组件，使用独立配置与云端连接。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QLockFile, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from app_paths import ICON_PATH
from companion_config import SettingsStore
from companion_link import DeviceLink
from companion_models import DirectModels
from companion_perception import CompanionPerception
from tts.edge_speaker import EdgeSpeaker
from ui.companion_settings import CompanionSettings
from ui.dynamic_pet_widget import DynamicDesktopPetWidget


class CompanionController(QObject):
    result_ready = pyqtSignal(str, str, str)
    summary_ready = pyqtSignal(dict, str, str, str)

    def __init__(self, pet, settings, *, state_dir=None):
        super().__init__()
        self.pet = pet
        self.settings = settings
        self.config = copy.deepcopy(settings.config)
        self.state_dir = Path(state_dir or Path(__file__).parent)
        self.started_at = time.time()
        self.history = {}
        self.busy = False
        self.summary_busy = False
        self.closed = False
        self.link = DeviceLink(self.state_dir / "companion_state.sqlite")
        self.link.status_changed.connect(self.set_link_status)
        self.link.command_received.connect(self.handle_command)
        self.link.report_sent.connect(
            lambda _: self.settings.report_log.appendPlainText("云端已确认收到汇报。")
        )
        self.speaker = EdgeSpeaker(enabled=self.config["tts_enabled"])
        self.settings.settings_saved.connect(self.apply_settings)
        self.settings.send_message.connect(self.chat)
        self.settings.request_report.connect(self.report)
        self.result_ready.connect(self._chat_result)
        self.summary_ready.connect(self._summary_result)
        self.report_timer = QTimer(self)
        self.report_timer.timeout.connect(lambda: self.report(False, automatic=True))
        self.runtime_timer = QTimer(self)
        self.runtime_timer.timeout.connect(self.write_runtime)
        self.runtime_timer.start(5000)
        self.link_status = "未配对"
        self.apply_settings(self.config)
        self.perception = CompanionPerception(self)
        self.settings.request_critique.connect(self.perception.trigger_manual)

    def apply_settings(self, config):
        previous = self.config.get("cloud", {})
        self.config = copy.deepcopy(config)
        self.speaker.update_config(enabled=self.config["tts_enabled"])
        if not self.config["tts_enabled"]:
            self.speaker.stop()
        self.report_timer.start(self.config["report_interval"] * 1000)
        if previous != self.config["cloud"] or not self.link.thread:
            try:
                self.link.start(self.config["cloud"])
            except ValueError as exc:
                self.set_link_status(str(exc))
        self.write_runtime()

    def set_link_status(self, status):
        self.link_status = status
        self.settings.connection_status.setText(status)
        self.write_runtime()

    def facts(self):
        facts = {
            "source": "desktop_client",
            "observed_at": time.time(),
            "uptime_seconds": int(time.time() - self.started_at),
            "cloud_connected": self.link.connected,
            "language_route": self.config["route"],
            "model_request_in_progress": self.busy,
        }
        if self.config["report_foreground"]:
            from perception.window_tracker import get_foreground_window_info

            info = get_foreground_window_info()
            facts["foreground"] = {
                "title": info.get("title", ""),
                "process": info.get("process", ""),
                "scene": info.get("scene", "general"),
            }
        return facts

    def handle_command(self, command):
        if command.get("_scope", self.link.journal.scope) != self.link.journal.scope:
            return
        try:
            if float(command.get("expires_at", 0)) <= time.time():
                raise ValueError("指令在处理前已过期")
            kind, payload = command.get("kind"), command.get("payload", {})
            if kind == "say":
                text = payload.get("text")
                if not isinstance(text, str) or not 0 < len(text) <= 4000:
                    raise ValueError("消息文字为空或过长")
                self.display(text, payload.get("emotion", "confident"))
                self.settings.append_chat("云端通知", text)
                result = {"displayed": True, "tts_requested": self.speaker.enabled}
            elif kind in ("status", "report"):
                facts = self.facts()
                result = {"facts": facts}
                if kind == "report":
                    result["report_id"] = self.link.report(
                        facts, self.factual_summary(facts)
                    )
            else:
                raise ValueError("客户端当前不支持此操作")
            self.link.complete(command["id"], "succeeded", result)
        except (ValueError, TypeError, KeyError) as exc:
            self.link.complete(command["id"], "failed", {"error": str(exc)})

    @staticmethod
    def factual_summary(facts):
        route = "云端处理" if facts["language_route"] == "cloud" else "本机直连"
        text = f"桌宠运行{facts['uptime_seconds']}秒，语言请求使用{route}。"
        if facts.get("foreground"):
            text += (
                "当前应用："
                + facts["foreground"]["process"]
                + "；窗口："
                + facts["foreground"]["title"]
            )
        return text

    def display(self, text, emotion="confident"):
        match = re.match(
            r"^\s*\[(confident|curious|shocked|relieved)\]\s*(.*)", text, re.DOTALL
        )
        if match:
            emotion, text = match.groups()
        self.pet.set_emotion(emotion)
        self.pet.show()
        self.pet.show_critique(text)
        self.speaker.speak_async(text)

    def _generate(self, text, config, history, *, image_b64=None):
        if config["route"] == "cloud":
            return self.link.chat(
                text, expected_scope=DeviceLink.scope_for(config["cloud"])
            )
        profile = next(
            p for p in config["profiles"] if p["id"] == config["active_profile"]
        )
        return DirectModels(profile).chat(
            text, history, system_prompt=config["system_prompt"], image_b64=image_b64
        )

    def chat(self, text):
        text = text.strip()
        if self.busy or self.summary_busy:
            self.settings.feedback.setText("上一条消息正在处理，请稍候。")
            return
        if not text or len(text) > 16000:
            self.settings.feedback.setText("请输入1至16000字的消息。")
            return
        config = copy.deepcopy(self.config)
        scope = self.link.journal.scope
        route_key = config["route"] + ":" + config["active_profile"]
        context = list(self.history.get(route_key, []))
        self.busy = True
        self.settings.chat_button.setEnabled(False)
        self.pet.set_thinking(True)
        self.settings.append_chat("我", text)

        def work():
            try:
                reply = self._generate(text, config, context)
                self.result_ready.emit(
                    json.dumps(
                        {
                            "text": text,
                            "key": route_key,
                            "route": config["route"],
                            "scope": scope,
                        },
                        ensure_ascii=False,
                    ),
                    reply,
                    "",
                )
            except Exception as exc:  # noqa: BLE001 -- 网络线程边界，转成界面错误不泄漏原响应。
                self.result_ready.emit(
                    "",
                    "",
                    str(exc)
                    if isinstance(exc, ValueError)
                    else "请求失败，请检查模型配置和网络",
                )

        threading.Thread(target=work, name="桌宠语言请求", daemon=True).start()

    def _chat_result(self, metadata, reply, error):
        if self.closed:
            return
        self.busy = False
        self.pet.set_thinking(False)
        self.settings.chat_button.setEnabled(True)
        if error:
            self.settings.append_chat("连接提示", error)
            self.settings.feedback.setText(error)
            return
        data = json.loads(metadata)
        history = self.history.setdefault(data["key"], [])
        history.extend(
            [
                {"role": "user", "content": data["text"]},
                {"role": "assistant", "content": reply},
            ]
        )
        del history[:-20]
        self.display(reply)
        self.settings.append_chat("小七", reply)
        if (
            data["route"] == "direct"
            and self.config["cloud"].get("token")
            and data.get("scope") == self.link.journal.scope
        ):
            self.link.report(
                {
                    "source": "local_model",
                    "event": "conversation_completed",
                    "reply_excerpt": reply[:400],
                },
                "本机完成一轮对话；以下为回复节选，不是完整会话同步。",
            )
        self.write_runtime()

    def report(self, summarize=False, automatic=False):
        if not self.config["cloud"].get("token"):
            if not automatic:
                self.settings.report_log.appendPlainText(
                    "尚未配对云端，请先粘贴配对信息。"
                )
            return
        facts = self.facts()
        scope = self.link.journal.scope
        if not summarize:
            self._summary_result(facts, self.factual_summary(facts), "", scope, False)
            return
        if self.summary_busy or self.busy:
            self.settings.report_log.appendPlainText(
                "当前请求正在处理，请完成后再生成总结。"
            )
            return
        self.summary_busy = True
        config = copy.deepcopy(self.config)
        self.settings.report_log.appendPlainText("正在请求当前模型总结观察…")

        def work():
            try:
                prompt = (
                    "请仅根据以下JSON观察生成一段简短中文汇报。区分事实与推测，不推断未提供的电脑状态：\n"
                    + json.dumps(facts, ensure_ascii=False)
                )
                summary = self._generate(prompt, config, [])
                self.summary_ready.emit(
                    facts, "模型总结（以原始观察为准）：" + summary, "", scope
                )
            except Exception as exc:  # noqa: BLE001 -- 后台请求失败转成界面提示。
                self.summary_ready.emit(
                    facts,
                    "",
                    str(exc) if isinstance(exc, ValueError) else "总结请求失败",
                    scope,
                )

        threading.Thread(target=work, name="桌宠情况总结", daemon=True).start()

    def _summary_result(self, facts, summary, error, scope=None, is_summary=True):
        if is_summary:
            self.summary_busy = False
        if self.closed:
            return
        if scope is not None and scope != self.link.journal.scope:
            self.settings.report_log.appendPlainText(
                "配对已变更，旧设备的总结不会发送到新设备。"
            )
            return
        if error:
            self.settings.report_log.appendPlainText("总结失败：" + error)
            return
        self.link.report(facts, summary)
        self.settings.report_log.appendPlainText(
            time.strftime("%H:%M:%S") + " " + summary + "\n已提交汇报，等待云端确认。"
        )

    def write_runtime(self):
        data = {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "route": self.config["route"],
            "cloud_connected": self.link.connected,
            "connection_status": self.link_status,
            "running": not self.closed,
            "tts_enabled": self.config["tts_enabled"],
            "auto_mode": self.config.get("auto_mode", False),
            "critique_in_progress": bool(
                getattr(getattr(self, "perception", None), "busy", False)
                or getattr(getattr(self, "original", None), "_is_analyzing", False)
            ),
        }
        path = self.state_dir / "companion_runtime.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.perception.stop()
        self.report_timer.stop()
        self.runtime_timer.stop()
        self.link.stop()
        self.speaker.stop()
        self.link.journal.close()
        self.write_runtime()


class CompanionPet(DynamicDesktopPetWidget):
    """保留上游完整菜单和样式，仅追加云端入口。"""

    open_cloud_settings = pyqtSignal()
    request_cloud_report = pyqtSignal()

    def __init__(self, *, appearance_path=None):
        super().__init__(config_mgr=AppearanceSettings(appearance_path))

    def _build_context_menu(self):
        menu = super()._build_context_menu()
        before_exit = menu.actions()[-2]
        cloud = QAction("☁️ 云端连接与模型", menu)
        cloud.triggered.connect(self.open_cloud_settings.emit)
        menu.insertAction(before_exit, cloud)
        report = QAction("📤 向云端汇报状态", menu)
        report.triggered.connect(self.request_cloud_report.emit)
        menu.insertAction(before_exit, report)
        return menu

    def mouseDoubleClickEvent(self, event):
        self.open_settings.emit()
        event.accept()


class AppearanceSettings:
    """只保存外观位置，避免复用上游配置时载入无关密钥。"""

    def __init__(self, path=None):
        self.path = Path(path or Path(__file__).with_name("companion_appearance.json"))
        self.values = {}
        if self.path.exists():
            self.values = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save(self, changes):
        self.values.update(
            {
                key: value
                for key, value in changes.items()
                if key in {"pet_pos_x", "pet_pos_y", "dynamic_fps", "target_height"}
            }
        )
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.values), encoding="utf-8")
        os.replace(temporary, self.path)


def connect_pet_actions(pet, controller):
    pet.request_critique.connect(controller.perception.trigger_manual)
    pet.toggle_tts.connect(controller.perception.toggle_tts)
    pet.toggle_auto.connect(controller.perception.toggle_auto)


def main():
    parser = argparse.ArgumentParser(description="小七双向桌宠")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--quit-after", type=float, default=0)
    parser.add_argument("--snapshot-dir", type=Path)
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("小七双向桌宠")
    app.setQuitOnLastWindowClosed(False)
    root = Path(__file__).parent
    pipe_name = "xiaoqi-desktop-" + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    lock = QLockFile(str(root / ".companion.lock"))
    if args.stop or not lock.tryLock(0):
        client = QLocalSocket()
        client.connectToServer(pipe_name)
        if client.waitForConnected(1500):
            client.write(b"stop" if args.stop else b"show")
            client.waitForBytesWritten(1500)
            client.disconnectFromServer()
            return 0
        return 0 if args.stop else 1
    try:
        settings = CompanionSettings(SettingsStore())
    except ValueError as exc:
        QMessageBox.critical(None, "配置读取失败", str(exc))
        return 1
    pet = CompanionPet()
    controller = CompanionController(pet, settings)

    def show_settings():
        print("[桌宠界面] 收到打开设置请求", flush=True)
        settings.open_window()

    def show_cloud_settings():
        settings.tabs.setCurrentIndex(2)
        show_settings()

    def quit_application():
        # 设置窗口关闭时会收起并忽略事件；明确退出不能依赖窗口接受关闭。
        app.exit(0)

    pet.open_settings.connect(show_settings)
    pet.open_cloud_settings.connect(show_cloud_settings)
    pet.request_cloud_report.connect(lambda: controller.report(False))
    pet.user_replied.connect(controller.chat)
    pet.quit_app.connect(quit_application)
    connect_pet_actions(pet, controller)
    controller.perception.start_hotkeys()
    tray = QSystemTrayIcon(QIcon(ICON_PATH), app)
    tray.setToolTip("小七双向桌宠 · 双击打开模型设置")
    menu = QMenu()
    menu.addAction("聊天与模型设置", show_settings)
    menu.addAction("立即汇报状态", lambda: controller.report(False))
    menu.addAction("显示桌宠", pet.show)
    menu.addSeparator()
    menu.addAction("退出桌宠", quit_application)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: (
            show_settings()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick
            else None
        )
    )
    tray.show()
    server = QLocalServer(app)
    QLocalServer.removeServer(pipe_name)
    server.listen(pipe_name)

    def incoming():
        socket = server.nextPendingConnection()

        def consume():
            command = bytes(socket.readAll())
            print("[桌宠界面] 收到本地控制请求：" + repr(command), flush=True)
            if command == b"stop":
                quit_application()
            else:
                show_settings()
            socket.disconnectFromServer()

        socket.readyRead.connect(consume)
        if socket.bytesAvailable():
            consume()

    server.newConnection.connect(incoming)
    app.aboutToQuit.connect(controller.shutdown)
    pet.show()
    settings.show()
    settings.open_window()
    settings.tabs.setCurrentIndex(1)
    print("小七双向桌宠已启动；模型配置窗口已打开。", flush=True)

    if args.snapshot_dir:
        args.snapshot_dir.mkdir(parents=True, exist_ok=True)

        def snapshot():
            settings.grab().save(str(args.snapshot_dir / "模型配置.png"))
            pet.grab().save(str(args.snapshot_dir / "桌宠.png"))

        QTimer.singleShot(1200, snapshot)
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), quit_application)
    result = app.exec()
    server.close()
    tray.hide()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
