import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from companion_config import SettingsStore
from companion_main import CompanionController
from ui.companion_settings import CompanionSettings


def test_new_pet_uses_separate_appearance_config(tmp_path, monkeypatch):
    import ui.dynamic_pet_widget as dynamic
    from companion_main import CompanionPet

    app = QApplication.instance() or QApplication([])

    def original_config_must_not_load():
        raise AssertionError("新客户端不应读取上游模型密钥配置")

    monkeypatch.setattr(dynamic, "ConfigManager", original_config_must_not_load)
    pet = CompanionPet(appearance_path=tmp_path / "appearance.json")
    assert pet.config_mgr.get("target_height", 250) == 250
    pet.hide()
    app.processEvents()


class Pet(QWidget):
    def __init__(self):
        super().__init__()
        self.text = ""

    def set_emotion(self, value):
        pass

    def set_thinking(self, value):
        pass

    def show_critique(self, text):
        self.text = text


def test_cloud_command_shows_pet_and_reports_completion(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    pet = Pet()
    controller = CompanionController(pet, settings, state_dir=tmp_path)
    message = {
        "type": "command",
        "id": "display-one",
        "kind": "say",
        "payload": {"text": "云端来的消息"},
        "expires_at": time.time() + 20,
    }
    controller.link.journal.begin(message)
    controller.handle_command(message)
    assert pet.text == "云端来的消息"
    cached = controller.link.journal.begin(message)
    assert cached["status"] == "succeeded"
    assert cached["result"]["displayed"] is True
    assert "spoken" not in cached["result"]
    controller.shutdown()
    settings.hide()
    app.processEvents()


def test_unrecognized_operation_is_failed_and_never_executed(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    controller = CompanionController(Pet(), settings, state_dir=tmp_path)
    message = {
        "id": "unknown",
        "kind": "shell",
        "payload": {},
        "expires_at": time.time() + 20,
    }
    controller.link.journal.begin(message)
    controller.handle_command(message)
    assert controller.link.journal.begin(message)["status"] == "failed"
    controller.shutdown()
    app.processEvents()


def test_late_summary_is_not_sent_to_newly_paired_device(tmp_path):
    app = QApplication.instance() or QApplication([])
    settings = CompanionSettings(SettingsStore(tmp_path / "settings.json"))
    controller = CompanionController(Pet(), settings, state_dir=tmp_path)
    controller.config["cloud"]["token"] = "fake"
    ready, release = threading.Event(), threading.Event()

    def generate(*args):
        ready.set()
        release.wait(2)
        return "旧设备情况"

    controller._generate = generate
    controller.report(True)
    assert ready.wait(2)
    controller.link.journal.scope = "new-device"
    release.set()
    until = time.monotonic() + 2
    while time.monotonic() < until:
        app.processEvents()
        if (
            "旧设备情况" in settings.report_log.toPlainText()
            or "配对已变更" in settings.report_log.toPlainText()
        ):
            break
        time.sleep(0.01)
    assert controller.link.journal.reports() == []
    controller.shutdown()
    settings.hide()
