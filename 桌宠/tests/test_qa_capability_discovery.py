import json

from companion_models import DirectModels
from qa_capabilities import CapabilityStore


def api(monkeypatch, responses):
    paths = []
    class Response:
        def __init__(self, status, data):
            self.status_code, self.data = status, data
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def json(self):
            return self.data
    class Session:
        def get(self, url, **kwargs):
            paths.append(url)
            assert kwargs['allow_redirects'] is False
            return Response(*responses[len(paths) - 1])
    initialize = DirectModels.__init__
    def init(model, profile):
        initialize(model, profile, Session())
    monkeypatch.setattr(DirectModels, '__init__', init)
    return paths


def test_http200_error_falls_back_to_exact_model_list_and_keeps_private_body_out(tmp_path, monkeypatch):
    paths = api(monkeypatch, [(200, {'error': {'message': 'secret-do-not-store'}}), (200, {'data': [
        {'id': 'wrong', 'context_length': 999999}, {'id': 'gemini-4-hydrogen', 'context_length': 65536}]})])
    profile = {'provider': 'openai', 'model': 'gemini-4-hydrogen', 'base_url': 'http://localhost:1234/v1'}
    store = CapabilityStore(tmp_path / 'limits.json')
    assert store.discover(profile)['context_tokens'] == 65536
    assert len(paths) == 2 and paths[-1].endswith('/models')
    assert '模型列表' in store.entry(profile)['diagnostic']
    assert 'secret-do-not-store' not in store.path.read_text(encoding='utf-8')
    store.discover(profile)
    assert len(paths) == 2


def test_nested_detail_is_read_and_empty_legacy_cache_refreshes_once(tmp_path, monkeypatch):
    paths = api(monkeypatch, [(200, {'data': {'id': 'custom', 'context_window': 128000}})])
    profile = {'provider': 'openai', 'model': 'custom', 'base_url': 'http://localhost:1234/v1'}
    store = CapabilityStore(tmp_path / 'limits.json')
    store.remember(profile, {})
    data = json.loads(store.path.read_text(encoding='utf-8'))
    for entry in data.values():
        entry.pop('discovery_version', None)
    store.path.write_text(json.dumps(data), encoding='utf-8')
    assert store.discover(profile)['context_tokens'] == 128000
    assert len(paths) == 1


def test_missing_provider_limits_uses_official_window_and_explains_reason(tmp_path, monkeypatch):
    paths = api(monkeypatch, [(404, {}), (200, {'data': [{'id': 'gemini-4-hydrogen'}]})])
    profile = {'provider': 'openai', 'model': 'gemini-4-hydrogen', 'base_url': 'http://localhost:1234/v1'}
    store = CapabilityStore(tmp_path / 'limits.json')
    assert store.discover(profile)['context_tokens'] == 1000000
    assert '未提供上下文' in store.entry(profile)['diagnostic']
    store.discover(profile)
    assert len(paths) == 2
