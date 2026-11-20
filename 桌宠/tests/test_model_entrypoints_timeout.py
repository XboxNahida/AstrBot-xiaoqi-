"""各本机模型入口共用可配置空闲超时，不真实调用模型。"""
import pytest

from tests.test_desktop_control_streaming import Response, Session


@pytest.mark.parametrize('entry', ['direct', 'qa', 'desktop'])
@pytest.mark.parametrize('provider', ['openai', 'gemini'])
def test_entrypoints_send_stream_and_use_profile_idle_timeout(entry, provider):
    profile = {'base_url': 'http://127.0.0.1:1234/v1', 'model': 'vision',
               'provider': provider, 'timeout_seconds': 21}
    events = ([{'candidates': [{'content': {'parts': [{'text': '完整回答'}]}, 'finishReason': 'STOP'}]}]
              if provider == 'gemini' else [{'choices': [{'delta': {'content': '完整回答'}, 'finish_reason': 'stop'}]}, '[DONE]'])
    class ContextResponse(Response):
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.close()
    session = Session(ContextResponse(events))
    if entry == 'qa':
        from qa_models import QAClient
        client = QAClient(profile, '原人设')
        client.model.session = session
        chunks = []
        assert client.chat([{'role': 'user', 'content': '问题'}], chunks.append) == '完整回答'
        assert chunks == ['完整回答']
    else:
        from companion_models import DirectModels
        from desktop_control.streaming import DesktopStreamModel
        client = (DirectModels if entry == 'direct' else DesktopStreamModel)(profile, session=session)
        assert client.chat('问题') == '完整回答'
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert kwargs['timeout'] == (8, 21) and kwargs['stream'] is True
    assert ':streamGenerateContent?alt=sse' in url if provider == 'gemini' else kwargs['json']['stream'] is True
