"""Qt事件泵及假输入验证云端自动接近，不控制用户桌面。"""

import threading
import time
import asyncio
import sys
import os
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication
from test_desktop_control_runtime import make_control, wait_for


def prepare(tmp_path, moving=True):
    control, backend, owner = make_control(tmp_path, None)
    cfg = dict(control.panel.config, cloud_enabled=True, move_pet=moving)
    control.panel.store.save(cfg)
    control.apply(cfg)
    control.external_tool("begin")
    shot = control.external_tool("screenshot")
    return (
        control,
        backend,
        owner,
        {
            "x": 300,
            "y": 200,
            "observation_id": shot["observation_id"],
            "request_id": "one",
        },
    )


def start_call(control, args, **kwargs):
    result = {}

    def work():
        try:
            result["value"] = control.external_tool("click", args, **kwargs)
        except Exception as exc:
            result["error"] = str(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, result


def finish_move(control):
    started, duration, origin, target, action = control._motion
    control._motion = (
        time.monotonic() - duration - 1,
        duration,
        origin,
        target,
        action,
    )
    control._move_tick()


def test_external_click_approaches_before_input_and_duplicate_does_not_move_again(
    tmp_path,
):
    control, backend, owner, args = prepare(tmp_path)
    try:
        thread, result = start_call(control, args)
        wait_for(lambda: control._motion is not None or backend.actions)
        assert control._motion is not None, "云端点击跳过角色移动"
        assert not backend.actions
        finish_move(control)
        wait_for(lambda: not thread.is_alive())
        assert result["value"]["status"] == "sent"
        assert backend.actions[0]["observation_id"] == args["observation_id"]
        assert backend.actions[0]["request_id"] == args["request_id"]
        assert control.external_tool("click", args) == result["value"]
        assert control._motion is None and len(backend.actions) == 1
    finally:
        control.stop()
        control.close()
        owner.pet.close()


@pytest.mark.parametrize("reason", ["stop", "cancel", "deadline", "foreground"])
def test_external_motion_cancel_or_changed_target_never_clicks(tmp_path, reason):
    control, backend, owner, args = prepare(tmp_path)
    cancel = threading.Event()
    try:
        thread, result = start_call(
            control, args, cancel=cancel, deadline=time.time() + 5
        )
        wait_for(lambda: control._motion is not None or not thread.is_alive())
        assert control._motion is not None
        if reason == "stop":
            control.external_tool("stop")
        elif reason == "cancel":
            cancel.set()
        elif reason == "deadline":
            control.external.pending["deadline"] = time.time() - 1
        else:
            backend.foreground = lambda: {"hwnd": 777, "pid": 888}
            finish_move(control)
        wait_for(lambda: not thread.is_alive())
        assert "error" in result
        assert not backend.actions
        assert control._motion is None
    finally:
        control.stop()
        control.close()
        owner.pet.close()


def test_external_disabled_movement_preserves_direct_input(tmp_path):
    control, backend, owner, args = prepare(tmp_path, moving=False)
    try:
        thread, result = start_call(control, args)
        wait_for(lambda: not thread.is_alive())
        assert result["value"]["status"] == "sent"
        assert control._motion is None and len(backend.actions) == 1
    finally:
        control.stop()
        control.close()
        owner.pet.close()


def test_user_grabs_pet_during_cloud_approach_cancels_click_even_without_pause_option(
    tmp_path,
):
    control, backend, owner, args = prepare(tmp_path)
    control.panel.config["pause_on_input"] = False
    try:
        thread, result = start_call(control, args)
        wait_for(lambda: control._motion is not None)
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(10, 10),
            QPointF(20, 20),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(owner.pet, event)
        wait_for(lambda: not thread.is_alive())
        assert not backend.actions and control._motion is None
        assert "error" in result
        assert not owner.pet._desktop_auto_moving
    finally:
        control.stop()
        control.close()
        owner.pet.close()


def test_repeated_claim_preserves_manual_physics_and_approach_stops_it(tmp_path):
    from types import SimpleNamespace

    control, _backend, owner, _args = prepare(tmp_path)
    calls = []
    owner.pet.feature_interaction = SimpleNamespace(stop=lambda: calls.append("stop"))
    try:
        control._claim(True)
        control._claim(True)
        assert not calls, "会话心跳不能反复清空用户甩抛"
        control._approach({"name": "click", "x": 300, "y": 200})
        assert calls == ["stop"] and owner.pet._desktop_auto_moving
        control._end_motion()
        assert not owner.pet._desktop_auto_moving
    finally:
        control.stop()
        control.close()
        owner.pet.close()


@pytest.mark.parametrize("reason", ["close", "revoke"])
def test_close_or_revoked_authorization_cleans_pending_motion(tmp_path, reason):
    control, backend, owner, args = prepare(tmp_path)
    try:
        thread, result = start_call(control, args)
        wait_for(lambda: control._motion is not None)
        if reason == "close":
            control.close()
        else:
            control.panel.config["cloud_enabled"] = False
            control.panel.store.save(control.panel.config)
            control.external.tick()
        wait_for(lambda: not thread.is_alive())
        assert "error" in result and not backend.actions
        assert control.external.pending is None and control._motion is None
        assert not owner.pet._desktop_auto_moving
    finally:
        control.stop()
        control.close()
        owner.pet.close()


def test_late_worker_result_cannot_finish_new_request(tmp_path):
    control, backend, owner, args = prepare(tmp_path, moving=False)
    entered, release = threading.Event(), threading.Event()
    perform = backend.perform

    def blocked(action, cancel):
        entered.set()
        release.wait(3)
        perform(action, cancel)

    backend.perform = blocked
    try:
        thread, result = start_call(control, args)
        wait_for(entered.is_set)
        old = control.external.pending
        control.stop()
        wait_for(lambda: not thread.is_alive())
        assert "error" in result
        release.set()
        wait_for(lambda: not control.tools._operation.locked())
        backend.perform = perform
        control.external_tool("begin")
        shot = control.external_tool("screenshot")
        control.panel.config["move_pet"] = True
        args.update(request_id="new", observation_id=shot["observation_id"])
        new_thread, new_result = start_call(control, args)
        wait_for(lambda: control._motion is not None)
        current = control.external.pending
        control.external._complete((old, {"status": "sent"}, None))
        assert control.external.pending is current and control._motion is not None
        assert not backend.actions
        finish_move(control)
        wait_for(lambda: not new_thread.is_alive())
        assert new_result["value"]["status"] == "sent" and len(backend.actions) == 1
    finally:
        release.set()
        control.stop()
        control.close()
        owner.pet.close()


@pytest.mark.parametrize(
    "name,payload", [("type", {"text": "测试"}), ("hotkey", {"keys": ["CTRL", "A"]})]
)
def test_keyboard_primitives_do_not_move_or_invoke_local_planner(
    tmp_path, name, payload
):
    control, backend, owner, args = prepare(tmp_path)
    try:
        result = control.external_tool(
            name, dict(payload, observation_id=args["observation_id"], request_id=name)
        )
        assert result["status"] == "sent" and len(backend.actions) == 1
        assert control._motion is None and not owner.pet._desktop_auto_moving
    finally:
        control.stop()
        control.close()
        owner.pet.close()


def test_motion_runs_on_qt_thread_while_input_runs_in_worker(tmp_path):
    from PyQt6.QtCore import QThread

    control, backend, owner, args = prepare(tmp_path)
    approach, perform = control._approach, backend.perform
    seen = []

    def gui(action):
        seen.append(("gui", QThread.currentThread() == control.thread()))
        approach(action)

    def worker(action, cancel):
        seen.append(("worker", QThread.currentThread() != control.thread()))
        perform(action, cancel)

    control._approach, backend.perform = gui, worker
    try:
        thread, result = start_call(control, args)
        wait_for(lambda: control._motion is not None)
        finish_move(control)
        wait_for(lambda: not thread.is_alive())
        assert result["value"]["status"] == "sent"
        assert seen == [("gui", True), ("worker", True)]
    finally:
        control.stop()
        control.close()
        owner.pet.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_real_websocket_qt_coordinator_moves_before_click_and_disconnect_cancels(
    tmp_path, cancelled
):
    # 只运行本工作区云端类及Qt假后端，不连接公网/真实桌面。
    source = os.environ.get("ASTRBOT_TEST_SOURCE")
    if not source:
        pytest.skip("仅在指定AstrBot联调源码时运行真实WSS/Qt组合测试")
    sys.path.insert(0, str(Path(source) / "tests"))
    from aiohttp.test_utils import TestClient, TestServer
    from test_desktop_link_connection import ack, connect, server_class
    from desktop_remote import RemoteDesktopTools

    server = server_class()(tmp_path / "server")
    control, backend, owner = make_control(tmp_path / "qt", None)
    capture = backend.capture
    backend.capture = lambda **kwargs: dict(
        capture(**kwargs), source_width=600, source_height=400
    )
    cfg = dict(control.panel.config, cloud_enabled=True, move_pet=True)
    control.panel.store.save(cfg)
    control.apply(cfg)
    remote = RemoteDesktopTools(lambda: control)

    async def qt_events():
        while True:
            QApplication.processEvents()
            await asyncio.sleep(0.005)

    ticking = asyncio.create_task(qt_events())
    pumping = None
    try:
        async with TestClient(TestServer(server.app)) as client:
            ws = await connect(server, client)
            await remote.connected(ws, "scope")
            await remote.receive(await ws.receive_json(timeout=1), ws, "scope")

            async def pump():
                async for packet in ws:
                    message = packet.json()
                    if message["type"] == "command":
                        await ack(ws, message)
                    else:
                        await remote.receive(message, ws, "scope")

            pumping = asyncio.create_task(pump())
            assert (await server.control.call("pc", "owner", "begin", {}))["ok"]
            shot = await server.control.call("pc", "owner", "screenshot", {})
            assert shot.get("ok") and "observation_id" in shot.get("result", {}), shot
            args = {
                "x": 300,
                "y": 200,
                "request_id": "wss-click",
                "observation_id": shot["result"]["observation_id"],
            }
            clicking = asyncio.create_task(
                server.control.call("pc", "owner", "click", args, timeout=3)
            )
            for _ in range(200):
                if control._motion is not None:
                    break
                await asyncio.sleep(0.005)
            assert control._motion is not None and not backend.actions
            if cancelled:
                await remote.disconnected(ws, "scope")
                await ws.close()
            else:
                finish_move(control)
            result = await clicking
            if cancelled:
                assert (
                    not result["ok"] and not backend.actions and control._motion is None
                )
            else:
                assert result["ok"] and len(backend.actions) == 1
                assert backend.actions[0]["observation_id"] == args["observation_id"]
                await remote.disconnected(ws, "scope")
                await ws.close()
            await pumping
    finally:
        ticking.cancel()
        if pumping is not None and not pumping.done():
            pumping.cancel()
        await asyncio.gather(
            ticking, *([pumping] if pumping is not None else []), return_exceptions=True
        )
        await server.stop()
        control.stop()
        control.close()
        owner.pet.close()
