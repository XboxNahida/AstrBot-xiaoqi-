from types import SimpleNamespace

from brain.gemini_client import GeminiCritiqueEngine
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414


def test_only_successful_screenshot_analysis_is_logged(original_config, monkeypatch):
    original_config.config.update(api_provider='openai_compatible', api_key='fake', model='vision')
    engine = GeminiCritiqueEngine()
    records = []
    engine.observation_callback = records.append
    response = SimpleNamespace(status_code=200, json=lambda: {'choices': [{'message': {'content': '[curious] 合成题图'}}]})
    monkeypatch.setattr('brain.gemini_client.requests.post', lambda *args, **kwargs: response)
    engine.generate_critique('work', '题目', 'test.exe', 'synthetic-base64')
    assert len(records) == 1 and records[0]['text'] == '[curious] 合成题图'
    engine.generate_critique('work', '无图', 'test.exe')
    assert len(records) == 1
    response.status_code, response.text = 404, 'fake error'
    engine.generate_critique('work', '错误', 'test.exe', 'synthetic-base64')
    assert len(records) == 1
