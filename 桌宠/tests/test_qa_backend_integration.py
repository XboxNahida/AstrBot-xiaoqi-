import io
import json

import pytest
from PIL import Image

from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
from qa_models import QAClient
from qa_requests import QARequests
from qa_session import QASession
from tests.test_qa_session import controller


def setup(tmp_path, monkeypatch):
    original, _ = controller()
    profile = {'id': 'one', 'name': '模型', 'model': 'vision', 'provider': 'openai', 'base_url': 'http://localhost:1234/v1'}
    original.bridge.config['profiles'] = [profile]
    state = QASession(original, tmp_path)
    state.change('one', state.personas()[0][0])
    requests = QARequests(original, tmp_path)
    policy = limits_for(profile, overrides={'context_tokens': 128000})
    monkeypatch.setattr(requests.limits, 'discover', lambda _p: policy)
    sent = []
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/json'}  # noqa: RUF012
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def json(self):
            return {'choices': [{'message': {'content': '合成图回答'}, 'finish_reason': 'stop'}]}
    class Session:
        def post(self, _url, **kwargs):
            sent.append(kwargs['json'])
            return Response()
    initialize = QAClient.__init__
    def init(client, *args):
        initialize(client, *args)
        client.model.session = Session()
    monkeypatch.setattr(QAClient, '__init__', init)
    return state, requests, policy, sent


def test_over_10mib_original_processed_in_worker_and_history_retained(tmp_path, monkeypatch):
    state, requests, _policy, sent = setup(tmp_path, monkeypatch)
    output = io.BytesIO()
    Image.effect_noise((2100, 1800), 100).convert('RGB').save(output, 'BMP')
    raw = output.getvalue()
    assert len(raw) > 10 * 1024 * 1024
    state.append(state.current, 'user', '旧' * 100000)
    state.append(state.current, 'assistant', '旧答')
    row = state.append(state.current, 'user', '合成图问题', [raw])
    bindings = []
    text, stats = requests.run(state.prepare(defer_images=True), lambda _s: None, bindings.append)
    assert text == '合成图回答' and stats['dropped_rounds'] == 1 and bindings
    assert len(json.dumps(sent[0]).encode()) <= 10 * 1024 * 1024
    assert state.store.image(row['images'][0]) == raw
    assert len(state.store.messages(state.current)) == 3
    state.store.close()


def test_aggregate_image_budget_rejects_latest_without_network(tmp_path, monkeypatch):
    state, requests, policy, sent = setup(tmp_path, monkeypatch)
    # 合成512图可放进单图预算，但十张的Base64整包超过10MiB。
    policy.update(context_tokens=4000000, image_edge=512, max_image_bytes=1024 * 1024)
    output = io.BytesIO()
    Image.effect_noise((512, 512), 100).convert('RGB').save(output, 'JPEG', quality=100, subsampling=0)
    raw = output.getvalue()
    count = (10 * 1024 * 1024) // (len(raw) * 4 // 3) + 2
    assert count <= 63
    state.append(state.current, 'user', '多张图片', [raw] * count)
    with pytest.raises(ValueError, match='当前问题'):
        requests.run(state.prepare(defer_images=True), lambda _s: None)
    assert not sent
    assert len(state.store.messages(state.current)[-1]['images']) == count
    state.store.close()
