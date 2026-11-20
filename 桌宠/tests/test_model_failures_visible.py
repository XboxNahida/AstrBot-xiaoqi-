from types import SimpleNamespace

import pytest
import requests
from PyQt6.QtWidgets import QApplication

from tests.test_desktop_control_streaming import Response
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414
from tests.test_qa_lifecycle import Original
from tests.test_smart_runtime import smart_runtime as smart_runtime  # noqa: PLC0414


@pytest.mark.parametrize('provider', ['gemini', 'openai_compatible', 'vertex'])
def test_original_generation_is_streamed_with_configured_idle_timeout(original_config, monkeypatch, provider):
    from brain.gemini_client import GeminiCritiqueEngine
    original_config.config.update(api_provider=provider, api_key='fake-key', model='vision', api_timeout=48,
                                  custom_api_url='https://example.com/v1')
    engine = GeminiCritiqueEngine()
    engine.vertex_auth = SimpleNamespace(get_auth=lambda **kw: ('fake-token', 'project', '', ''))
    gemini = provider != 'openai_compatible'
    events = ([{'candidates': [{'content': {'parts': [{'text': '完整回答'}]}, 'finishReason': 'STOP'}]}]
              if gemini else [{'choices': [{'delta': {'content': '完整回答'}, 'finish_reason': 'stop'}]}, '[DONE]'])
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(events)
    monkeypatch.setattr(requests, 'post', post)
    method = {'gemini': engine._call_gemini_native, 'vertex': engine._call_vertex_ai,
              'openai_compatible': engine._call_openai_compatible}[provider]
    assert method('完整人设', '提问', 'picture', '桌宠') == '完整回答'
    assert calls[0][1]['stream'] and calls[0][1]['timeout'] == (8, 48)


def test_failed_qa_keeps_partial_visible_per_session_without_archiving(tmp_path):
    from qa_runtime import ScreenshotQA
    app = QApplication.instance() or QApplication([])
    qa = ScreenshotQA(Original(), tmp_path)
    identity = qa.state.current
    qa.state.append(identity, 'user', '题目')
    qa.active_id, qa.busy, qa.stream = identity, True, '已收到的半句话'
    qa._complete((identity, '', '连续120秒未收到任何数据'))
    assert qa.failures[identity]['partial'] == '已收到的半句话'
    assert len(qa.state.store.messages(identity)) == 1
    assert 'telemetry' not in qa.state.store.settings(identity)
    rendered = []
    qa.window = SimpleNamespace(renderer=SimpleNamespace(render=lambda rows, *_args: rendered.extend(rows)))
    qa.render()
    assert '未完成' in rendered[-1]['text'] and '半句话' in rendered[-1]['text']
    qa.window = None
    qa.close()
    app.processEvents()


def test_passive_failure_is_visible_after_foreground_changed(smart_runtime, monkeypatch):
    smart = smart_runtime.controller.smart
    bubbles = []
    monkeypatch.setattr(smart_runtime.pet, 'show_critique', bubbles.append)
    monkeypatch.setattr('smart_runtime.get_foreground_window_info', lambda: {'title': '另一个窗口'})
    smart._complete((smart.generation, {'title': '原窗口'}, 'passive', {'error': 'HTTP 503：Gemini overloaded'}, False))
    assert bubbles == ['HTTP 503：Gemini overloaded']
