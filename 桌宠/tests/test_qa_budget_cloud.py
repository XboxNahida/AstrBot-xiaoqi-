"""长历史云端能力协商：旧端不拒收，新端可按模型窗口保留。"""
from types import SimpleNamespace

import pytest
from test_qa_cloud_context import context

from cloud_plugin.astrbot_plugin_desktop_link.provider_api import ProviderAPI
from qa_requests import QARequests


def rows():
    return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': '短消息'} for i in range(205)]


@pytest.mark.asyncio
async def test_new_server_accepts_more_than_200_short_messages_with_hard_budget():
    ctx, calls, provider = context()
    provider.provider_config = dict(provider.provider_config, context_length=128000)
    api = ProviderAPI(ctx, {})
    info = await api.rpc('pc', 'qa_info', {})
    assert info['max_messages'] == 5000
    result = await api.rpc('pc', 'qa', {'messages': rows(), 'system': '人格', 'expected_model': info['signature']})
    assert result['text'] == '答' and len(calls[0]['contexts']) == 205
    assert result['telemetry']['kept_rounds'] == 103


@pytest.mark.parametrize('new_server', [False, True])
def test_client_respects_old_or_new_cloud_message_cap(tmp_path, monkeypatch, new_server):
    import qa_requests as impl
    profile = {'model': 'gpt-4o', 'provider': 'cloud'}
    info = {'signature': 'stable'}
    if new_server:
        info['max_messages'] = 5000
    sent = []
    def rpc(_op, payload, **_kwargs):
        sent.append(payload)
        return {'text': '完整回答', 'telemetry': {}}
    requests = QARequests(SimpleNamespace(bridge=SimpleNamespace(link=SimpleNamespace(rpc=rpc))), tmp_path)
    monkeypatch.setattr(requests, 'profile', lambda _: (profile, info))
    monkeypatch.setattr(impl.DeviceLink, 'scope_for', lambda _: 'scope')
    job = {'model': 'cloud', 'settings': {'context_mode': 'retain'}, 'messages': rows(), 'system': '人格', 'cloud': {}}
    requests.run(job, lambda _: None)
    assert len(sent[0]['messages']) == (205 if new_server else 199)
