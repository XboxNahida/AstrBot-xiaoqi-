"""同步门面收到网络取消与截止，不操纵真实桌面。"""

import threading
import time

from desktop_remote import RemoteDesktopTools


def test_bridge_passes_cancellation_and_deadline_to_desktop_coordinator():
    class Desktop:
        active = False
        seen = []

        def external_tool(self, name, payload):
            if name == "begin":
                self.active = True
                return {"session_id": "cloud-session"}
            if name == "status":
                return {
                    "active": self.active,
                    "source": "cloud",
                    "session_id": "cloud-session",
                }
            return {"ok": True, "status": "sent"}

        def external_tool_context(self, name, payload, cancel, deadline):
            self.seen.append((name, cancel, deadline))
            return self.external_tool(name, payload)

    desktop = Desktop()
    remote = RemoteDesktopTools(lambda: desktop)
    token = (object(), "scope", threading.Event())
    cancel, deadline = threading.Event(), time.time() + 15
    remote._execute(token, "begin", {}, deadline, cancel)
    remote._execute(token, "click", {"x": 1, "y": 2}, deadline, cancel)
    assert [row[0] for row in desktop.seen] == ["begin", "click"]
    assert all(row[1] is cancel and row[2] == deadline for row in desktop.seen)
