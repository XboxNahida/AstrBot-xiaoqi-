"""直连和云路由只发送一次，沿用模型硬限制并携带日常模式。"""
from types import SimpleNamespace

import pytest

from cloud_plugin.astrbot_plugin_desktop_link.model_limits import (
    limits_for,
    model_identity,
)
from qa_requests import QARequests, describe_usage


def make_job():
    profile = {'provider': 'openai', 'model': 'gpt-4o', 'base_url': 'http://127.0.0.1/v1'}
    rows = []
    for _ in range(8):
        rows.extend([{'role': 'user', 'content': '题' * 500}, {'role': 'assistant', 'content': '答' * 500}])
    return {'id': 'test', 'model': 'test', 'profile': profile, 'system': '完整人格',
            'settings': {}, 'messages': rows + [{'role': 'user', 'content': '当前问题'}]}


@pytest.mark.parametrize('mode', ['normal', 'retain'])
def test_modes_reach_direct_request_without_extra_calls(tmp_path, monkeypatch, mode):
    import qa_requests as impl
    job, sent = make_job(), []
    job['settings']['context_mode'] = mode
    requests = QARequests(SimpleNamespace(), tmp_path)
    monkeypatch.setattr(requests.limits, 'discover', lambda p: limits_for(p))
    class Client:
        def __init__(self, profile, system):
            self.telemetry = {'prompt_tokens': 1200}
            assert system == job['system']
        def chat(self, messages, on_chunk):
            sent.append(messages)
            return '完整回复'
    monkeypatch.setattr(impl, 'QAClient', Client)
    text, stats = requests.run(job, lambda _: None)
    assert text == '完整回复' and len(sent) == 1
    assert stats['context_mode'] == mode
    assert (len(sent[0]) < len(job['messages'])) == (mode == 'normal')
    assert stats['prompt_tokens'] == 1200 and stats['output_tokens'] == (4096 if mode == 'normal' else 8192)
    assert '普通' in describe_usage(stats) if mode == 'normal' else '保留上下文' in describe_usage(stats)


def test_explicit_output_override_remains_and_unknown_model_stays_guarded(tmp_path, monkeypatch):
    requests, job = QARequests(SimpleNamespace(), tmp_path), make_job()
    requests.limits.override(job['profile'], {'output_tokens': 1500})
    monkeypatch.setattr(requests.limits, 'discover', lambda p: requests.limits.resolve(p))
    captured = []
    import qa_requests as impl
    class Client:
        def __init__(self, profile, _system):
            self.telemetry = {}
            captured.append(profile['_qa_limits']['output_tokens'])
        def chat(self, *_):
            return '答'
    monkeypatch.setattr(impl, 'QAClient', Client)
    requests.run(job, lambda _: None)
    assert captured == [1500]


def test_cloud_gets_only_selected_messages_and_keeps_local_budget_stats(tmp_path, monkeypatch):
    import qa_requests as impl
    job = make_job()
    job.update(model='cloud', cloud={})
    binding = model_identity(job['profile'])
    calls = []
    def rpc(action, payload, **kwargs):
        calls.append((action, payload))
        return {'text': '云端完整答', 'telemetry': {'dropped_rounds': 0, 'prompt_tokens': 3000}}
    requests = QARequests(SimpleNamespace(bridge=SimpleNamespace(link=SimpleNamespace(rpc=rpc))), tmp_path)
    monkeypatch.setattr(requests, 'profile', lambda _: (job['profile'], {'signature': binding}))
    monkeypatch.setattr(impl.DeviceLink, 'scope_for', lambda _: 'test-scope')
    text, stats = requests.run(job, lambda _: None)
    assert text == '云端完整答' and len(calls) == 1
    assert len(calls[0][1]['messages']) < len(job['messages'])
    assert calls[0][1]['context_options']['output_tokens'] == 4096
    assert stats['budget_start'] == stats['dropped_rounds'] > 0
