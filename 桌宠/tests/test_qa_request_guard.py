import pytest

from qa_models import QAClient


def test_guard_trims_old_rounds_sets_output_cap_and_reads_cache_usage():
    sent = []
    class Response:
        status_code = 200
        headers = {'Content-Type': 'application/json'}  # noqa: RUF012
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def json(self):
            return {'choices': [{'message': {'content': '回答'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 1234, 'prompt_tokens_details': {'cached_tokens': 900}}}
    class Session:
        def post(self, _url, **kwargs):
            sent.append(kwargs['json'])
            return Response()
    client = QAClient({'provider': 'openai', 'model': 'unknown', 'base_url': 'http://127.0.0.1:1234/v1'}, '固定人设')
    client.model.session = Session()
    history = [{'role': 'user', 'content': '旧' * 10000}, {'role': 'assistant', 'content': '旧答'},
               {'role': 'user', 'content': '新问题'}]
    assert client.chat(history) == '回答'
    assert sent[0]['max_tokens'] == 2048 and len(sent[0]['messages']) == 2
    assert client.telemetry['dropped_rounds'] == 1 and client.telemetry['cached_tokens'] == 900
    assert len(history) == 3


def test_oversized_latest_question_stops_before_network():
    client = QAClient({'model': 'unknown', 'base_url': 'http://127.0.0.1:1234/v1'}, '固定人设')
    with pytest.raises(ValueError, match='当前问题'):
        client.chat([{'role': 'user', 'content': '字' * 100000}])


def test_stream_disconnect_does_not_commit_partial_answer():
    class Response:
        status_code = 200
        headers = {}  # noqa: RUF012
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            pass
        def iter_lines(self, **_kwargs):
            yield 'data: {"choices":[{"delta":{"content":"未完"}}]}'
    class Session:
        def post(self, *_args, **_kwargs):
            return Response()
    client = QAClient({'model': 'unknown', 'base_url': 'http://127.0.0.1:1234/v1'}, '人设')
    client.model.session = Session()
    with pytest.raises(ValueError, match='未完成'):
        client.chat([{'role': 'user', 'content': '问题'}])
