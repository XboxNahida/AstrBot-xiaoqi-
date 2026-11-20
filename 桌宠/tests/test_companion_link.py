import asyncio
import threading
import time

from companion_link import CommandJournal, DeviceLink


def test_duplicate_command_returns_last_result_after_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = CommandJournal(path)
    message = {
        "id": "one",
        "kind": "say",
        "payload": {"text": "你好"},
        "expires_at": time.time() + 30,
    }
    assert journal.begin(message) is None
    journal.complete("one", "succeeded", {"displayed": True})
    journal.close()
    again = CommandJournal(path)
    assert again.begin(message)["status"] == "succeeded"
    assert again.begin(message)["result"]["displayed"] is True
    again.close()


def test_expired_command_never_starts(tmp_path):
    journal = CommandJournal(tmp_path / "journal.sqlite")
    result = journal.begin(
        {"id": "expired", "kind": "say", "expires_at": time.time() - 1}
    )
    assert result["status"] == "failed"
    assert result["result"]["error"] == "指令已过期"
    journal.close()


def test_interrupted_command_not_reexecuted_on_restart(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = CommandJournal(path)
    message = {"id": "in-flight", "expires_at": time.time() + 60}
    journal.begin(message)
    journal.close()
    reopened = CommandJournal(path)
    assert reopened.begin(message)["status"] == "failed"
    assert "中断" in reopened.begin(message)["result"]["error"]
    reopened.close()


def test_reports_and_commands_are_isolated_when_pairing_another_device(tmp_path):
    journal = CommandJournal(tmp_path / "journal.sqlite")
    journal.scope = "device-one"
    journal.queue_report({"id": "report-one", "facts": {"private": "one"}})
    command = {"id": "same-id", "expires_at": time.time() + 30}
    assert journal.begin(command) is None
    journal.complete("same-id", "succeeded", {})
    journal.scope = "device-two"
    assert journal.reports() == []
    assert journal.begin(command) is None
    journal.scope = "device-one"
    assert journal.reports()[0]["id"] == "report-one"
    assert journal.begin(command)["status"] == "succeeded"
    journal.close()


def test_stop_cancels_pending_connection_handshake(tmp_path):
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    link = DeviceLink(tmp_path / "state.sqlite")
    entered = threading.Event()

    async def stalled():
        link.loop = asyncio.get_running_loop()
        link.run_task = asyncio.current_task()
        entered.set()
        await asyncio.sleep(8)

    link._run = stalled
    link.start(
        {"url": "ws://127.0.0.1:12345/desktop/ws", "device_id": "one", "token": "fake"}
    )
    assert entered.wait(2)
    started = time.monotonic()
    try:
        link.stop()
        assert time.monotonic() - started < 2
        assert link.thread is None
    finally:
        if link.loop and link.loop.is_running():
            link.loop.call_soon_threadsafe(link.run_task.cancel)
        if link.thread:
            link.thread.join(2)
        link.journal.close()
        app.processEvents()


def test_token_rotation_changes_pairing_scope():
    cloud = {"url": "wss://example.com/desktop/ws", "device_id": "one", "token": "old"}
    assert DeviceLink.scope_for(cloud) != DeviceLink.scope_for(
        {**cloud, "token": "new"}
    )


def test_cloud_chat_cannot_use_different_pairing_scope(tmp_path):
    import pytest
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    link = DeviceLink(tmp_path / "state.sqlite")
    link.connected = True
    link.journal.scope = "new"
    with pytest.raises(ValueError):
        link.chat("旧任务", expected_scope="old")
    assert link.pending == {}
    link.journal.close()
    app.processEvents()


def test_cloud_chat_rejects_pairing_switch_between_check_and_send(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication

    import companion_link

    app = QApplication.instance() or QApplication([])
    link = DeviceLink(tmp_path / "race.sqlite")
    link.connected = True
    link.journal.scope = "old"
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    link.loop = loop
    entered, release, sent = threading.Event(), threading.Event(), []

    class Socket:
        async def send_json(self, message):
            sent.append(message)
            with link.pending_lock:
                future = link.pending[message["id"]]
            future.set_result("回复")

    link.ws = Socket()
    original_uuid = companion_link.uuid.uuid4

    def paused_uuid():
        entered.set()
        assert release.wait(3)
        return original_uuid()

    monkeypatch.setattr(companion_link.uuid, "uuid4", paused_uuid)
    result = []

    def chat():
        try:
            result.append(link.chat("旧设备私密问题", expected_scope="old"))
        except ValueError:
            result.append("rejected")

    worker = threading.Thread(target=chat, daemon=True)
    try:
        worker.start()
        assert entered.wait(1)
        link.journal.scope = "new"
        link.ws = Socket()
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert sent == []
        assert result == ["rejected"]
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(2)
        loop.close()
        link.loop = None
        link.journal.close()
        app.processEvents()


def test_scheduled_message_does_not_follow_a_replaced_socket(tmp_path):
    import pytest
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    link = DeviceLink(tmp_path / "scheduled.sqlite")
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    link.loop, link.connected = loop, True
    entered, release, sent = threading.Event(), threading.Event(), []

    class Socket:
        async def send_json(self, message):
            sent.append(message)

    def block_loop():
        entered.set()
        release.wait(3)

    try:
        link.ws = Socket()
        loop.call_soon_threadsafe(block_loop)
        assert entered.wait(1)
        sending = link._send({"type": "chat", "text": "旧连接的问题"})
        link.ws = Socket()
        release.set()
        with pytest.raises(ValueError, match="连接已改变"):
            sending.result(timeout=2)
        assert sent == []
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(2)
        loop.close()
        link.loop = None
        link.journal.close()
        app.processEvents()
