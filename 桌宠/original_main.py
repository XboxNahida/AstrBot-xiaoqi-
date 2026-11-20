"""原版扩展启动器，经典与动态两种原程序均保留。"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QLockFile, Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtQuick import QQuickWindow, QSGRendererInterface
from PyQt6.QtWidgets import QApplication


def main():
    parser = argparse.ArgumentParser(description="哈Gemi 原版桌宠 + 云端连接扩展")
    parser.add_argument("--mode", choices=("classic", "dynamic"), default="dynamic")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--quit-after", type=float, default=0)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--state-dir", type=Path, help="隔离验收数据目录")
    args = parser.parse_args()
    root = args.state_dir.resolve() if args.state_dir else Path(__file__).parent
    root.mkdir(parents=True, exist_ok=True)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.Round)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    # 答疑WebEngine不创建D3D呈现窗口，避免被RTSS等游戏OSD识别；普通角色仍用原QPainter绘制。
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Software)
    web_flags = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    for flag in ('--disable-gpu', '--disable-gpu-compositing'):
        if flag not in web_flags.split():
            web_flags = f'{web_flags} {flag}'.strip()
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = web_flags
    app = QApplication(sys.argv)
    app.setApplicationName("哈Gemi 原版扩展")
    app.setQuitOnLastWindowClosed(False)
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

    import app_paths
    import config_manager
    import log_manager

    if args.state_dir:
        app_paths.CONFIG_FILE = config_manager.CONFIG_FILE = str(root / "config.json")
        app_paths.MEMORY_FILE = str(root / "memory.json")
        config_manager.CYBER_WHALE_CONFIG = str(root / "absent_inherited.json")
        log_manager.LOG_FILE = str(root / "desktop_pet.log")
    from companion_config import SettingsStore

    # 首次恢复原程序时保留之前已选的开关/位置，不改已有原版配置。
    if not Path(config_manager.CONFIG_FILE).exists():
        old = SettingsStore(root / "companion_settings.json").load()
        initial = {"auto_mode": old["auto_mode"], "enable_tts": old["tts_enabled"], "cooldown_seconds": old["critique_cooldown"]}
        appearance = root / "companion_appearance.json"
        if appearance.exists():
            saved = json.loads(appearance.read_text(encoding="utf-8"))
            initial.update({k: v for k, v in saved.items() if k in ("pet_pos_x", "pet_pos_y", "target_height", "dynamic_fps")})
        if args.state_dir:
            initial["auto_check_update"] = False
        config_manager.ConfigManager().save(initial)
    from original_runtime import create_runtime

    runtime = create_runtime(args.mode, state_dir=root)
    server = QLocalServer(app)
    QLocalServer.removeServer(pipe_name)
    if not server.listen(pipe_name):
        runtime.shutdown()
        raise RuntimeError("无法建立本工作区的单实例控制通道")

    def incoming():
        socket = server.nextPendingConnection()

        def consume():
            command = bytes(socket.readAll())
            if command == b"stop":
                app.exit(0)
            else:
                runtime.window.open_window()
            socket.disconnectFromServer()
        socket.readyRead.connect(consume)
        if socket.bytesAvailable():
            consume()
    server.newConnection.connect(incoming)
    app.aboutToQuit.connect(runtime.shutdown)
    runtime.pet.show()
    runtime.window.open_window()
    print(f"原版{args.mode}桌宠已启动；完整设置中心与云端扩展就绪。", flush=True)

    if args.snapshot_dir:
        args.snapshot_dir.mkdir(parents=True, exist_ok=True)

        def snapshot():
            runtime.window.tabs.setCurrentIndex(0)
            runtime.window.grab().save(str(args.snapshot_dir / f"{args.mode}-原版设置.png"))
            runtime.window.tabs.setCurrentIndex(runtime.window.extension_tab)
            runtime.window.extension.tabs.setCurrentIndex(1)
            runtime.window.grab().save(str(args.snapshot_dir / f"{args.mode}-新增模型页.png"))
            runtime.window.tabs.setCurrentIndex(0)
            runtime.pet.grab().save(str(args.snapshot_dir / f"{args.mode}-桌宠.png"))
            screen = runtime.pet.screen()
            diagnostic = {
                "pid": os.getpid(),
                "pet_hwnd": int(runtime.pet.winId()),
                "pet_rect": runtime.pet.geometry().getRect(),
                "available_rect": screen.availableGeometry().getRect(),
                "dpr": runtime.pet.devicePixelRatioF(),
            }
            (args.snapshot_dir / "窗口诊断.json").write_text(json.dumps(diagnostic), encoding="utf-8")
        QTimer.singleShot(1200, snapshot)
    if args.quit_after:
        QTimer.singleShot(int(args.quit_after * 1000), lambda: app.exit(0))
    try:
        return app.exec()
    finally:
        runtime.shutdown()
        server.close()
        runtime.tray.hide()
        lock.unlock()


if __name__ == "__main__":
    raise SystemExit(main())
