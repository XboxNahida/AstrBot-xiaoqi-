import asyncio
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from cloud_plugin.astrbot_plugin_desktop_link.server import DesktopLinkServer
from companion_config import SettingsStore
from companion_main import CompanionController
from tests.test_companion_controller import Pet
from ui.companion_settings import CompanionSettings


async def eventually(app, predicate, timeout=6):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        app.processEvents()
        if predicate():
            return
        await asyncio.sleep(0.02)
    assert predicate(), '等待实际双向通道结果超时'


async def test_actual_websocket_cloud_to_pet_receipt_report_and_chat(tmp_path):
    app = QApplication.instance() or QApplication([])

    async def chat_handler(device_id, text):
        return '测试云端回复：' + text

    server = DesktopLinkServer(tmp_path / 'server', chat_handler)
    await server.start(port=0)
    pairing = server.pair('integration-pc', '集成测试设备')
    settings = CompanionSettings(SettingsStore(tmp_path / 'settings.json'))
    settings.config['cloud'] = {'url': f'ws://127.0.0.1:{server.port}/desktop/ws',
                                'device_id': pairing['device_id'], 'token': pairing['token']}
    pet = Pet()
    controller = CompanionController(pet, settings, state_dir=tmp_path)
    try:
        await eventually(app, lambda: controller.link.connected)
        sent = await server.command('integration-pc', 'say', {'text': '真实回环通知'})
        await eventually(app, lambda: server.command_status(sent['id'])['status'] == 'succeeded')
        assert pet.text == '真实回环通知'
        assert server.command_status(sent['id'])['result']['displayed'] is True
        controller.report(False)
        await eventually(app, lambda: len(server.reports('integration-pc')) == 1)
        assert server.reports('integration-pc')[0]['facts']['source'] == 'desktop_client'
        await eventually(app, lambda: not controller.link.journal.reports())
        reply = await asyncio.to_thread(controller.link.chat, '你好')
        assert reply == '测试云端回复：你好'
        status = await server.command('integration-pc', 'status', {})
        await eventually(app, lambda: server.command_status(status['id'])['status'] == 'succeeded')
        assert server.command_status(status['id'])['result']['facts']['cloud_connected'] is True
    finally:
        await asyncio.to_thread(controller.link.stop)
        controller.shutdown()
        settings.hide()
        await server.stop()
        app.processEvents()
