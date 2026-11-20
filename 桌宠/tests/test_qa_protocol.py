import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tests.test_desktop_server import load_server


@pytest.mark.asyncio
async def test_rpc_dedup_records_summary_and_old_commands(tmp_path):
    calls, summaries = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def rpc(device, op, payload):
        calls.append((device, op, payload))
        await asyncio.sleep(0.01)
        return {'text': '完整解答'}
    async def summarize(device, batch):
        summaries.append(batch)
        entered.set()
        await release.wait()
        return '摘要'
    server = load_server()(tmp_path, rpc_handler=rpc, summarizer=summarize)
    pair = server.pair('pc', '电脑')
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect('/desktop/ws', headers={'Authorization': 'Bearer ' + pair['token']})
        hello = await ws.receive_json()
        assert 'records' in hello['capabilities']
        request = {'type': 'rpc', 'id': 'qa-1', 'op': 'qa', 'payload': {'messages': []}}
        await ws.send_json(request)
        assert (await ws.receive_json())['result']['text'] == '完整解答'
        await ws.send_json(request)
        assert (await ws.receive_json())['result']['text'] == '完整解答'
        assert len(calls) == 1
        import time
        for index in range(3):
            await ws.send_json({'type': 'report', 'id': 'o' + str(index), 'observed_at': time.time(),
                               'facts': {'_record_type': 'observation', 'text': '字' * (500 if index < 2 else 1)}})
            assert (await ws.receive_json())['type'] == 'report_ack'
            if index < 2:
                assert not summaries
        await asyncio.wait_for(entered.wait(), 1)
        await ws.send_json({'type': 'report', 'id': 'qa-record', 'observed_at': time.time(),
            'facts': {'_record_type': 'qa_message', 'conversation': 'c', 'message': 'm', 'role': 'user', 'text': '题目'}})
        assert (await ws.receive_json())['type'] == 'report_ack'
        assert not server.reports('pc')  # 新记录不会漏入永久旧reports表。
        assert len(server.extension.records.read('pc')['qa_today']) == 1
        release.set()
        await asyncio.sleep(0.03)
        assert len(server.extension.records.read('pc')['summaries']) == 1
        receipt = await server.command('pc', 'status', {})
        assert (await ws.receive_json())['id'] == receipt['id']
        await ws.close()
    await server.stop()


@pytest.mark.asyncio
async def test_cloud_guard_error_is_actionable_but_provider_error_is_private(tmp_path):
    from cloud_plugin.astrbot_plugin_desktop_link.provider_api import ProviderAPI
    from tests.test_qa_cloud_context import context
    ctx, calls, _ = context()
    api = ProviderAPI(ctx, {})
    async def handler(device, op, payload):
        if payload.get('private_failure'):
            raise ValueError('私密密钥不能回传')
        return await api.rpc(device, op, payload)
    server = load_server()(tmp_path, rpc_handler=handler)
    pair = server.pair('pc', '电脑')
    async with TestClient(TestServer(server.app)) as client:
        ws = await client.ws_connect('/desktop/ws', headers={'Authorization': 'Bearer ' + pair['token']})
        assert 'qa_info' in (await ws.receive_json())['capabilities']
        await ws.send_json({'type': 'rpc', 'id': 'big', 'op': 'qa', 'payload': {
            'messages': [{'role': 'user', 'content': '字' * 50000}], 'system': '人设'}})
        error = (await ws.receive_json())['error']
        assert '当前问题' in error and not calls
        await ws.send_json({'type': 'rpc', 'id': 'private', 'op': 'qa', 'payload': {'private_failure': True}})
        assert '私密密钥' not in (await ws.receive_json())['error']
        await ws.close()
    await server.stop()
