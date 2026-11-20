import json
from types import SimpleNamespace

import pytest
import requests

from tests.test_desktop_control_streaming import Response, Session, model_for
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_smart_models import setup_client


def test_failed_stream_preserves_usage_already_received(monkeypatch):
    from smart_models import SmartModelError
    client, _, _, _ = setup_client(monkeypatch)
    def content(**kwargs):
        yield ('data: ' + json.dumps({'choices': [{'delta': {'content': '半句'}}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}) + '\n\n').encode()
        raise requests.ReadTimeout()
    response = Response([])
    response.iter_content = content
    monkeypatch.setattr(requests.Session, 'post', lambda *a, **kw: response)
    with pytest.raises(SmartModelError) as failed:
        client.generate({}, 'synthetic-image')
    assert failed.value.result['total_tokens'] == 20
    assert failed.value.result['usage_known'] is True
    assert failed.value.result['text'] == ''


def test_desktop_200_json_error_is_not_misreported_as_no_stream():
    model, _, response = model_for([])
    response.headers = {'Content-Type': 'application/json'}
    response.json = lambda: {'error': {'code': 429, 'message': 'quota exhausted'}}
    with pytest.raises(ValueError, match='429.*quota exhausted'):
        model.chat('目标')


def test_failed_original_summary_raises_instead_of_returning_failure_text(original_config, monkeypatch):
    from brain.gemini_client import GeminiCritiqueEngine
    from original_bridge import OriginalCloudBridge
    original_config.config.update(api_provider='openai_compatible', api_key='test-key', model='vision')
    engine = GeminiCritiqueEngine()
    monkeypatch.setattr(requests, 'post', lambda *a, **kw: Response([]))
    bridge = SimpleNamespace(original=SimpleNamespace(engine=engine))
    with pytest.raises(ValueError, match='未完成'):
        OriginalCloudBridge._transport(bridge, '总结', {'route': 'original'}, '人设')


def test_stream_usage_request_can_be_disabled():
    from companion_models import DirectModels
    for enabled in [True, False]:
        session = Session(Response([{'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}, '[DONE]']))
        model = DirectModels({'model': 'vision', 'base_url': 'http://127.0.0.1:1234/v1', 'stream_usage': enabled}, session)
        model.chat('问题')
        assert session.calls[0][1]['json'].get('stream_options', {}).get('include_usage', False) is enabled
