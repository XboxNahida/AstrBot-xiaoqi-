"""操作专用SSE协议与本地回环测试，不请求真实模型。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class Response:
    status_code = 200
    headers = {'Content-Type': 'text/event-stream'}  # noqa: RUF012

    def __init__(self, events):
        self.events, self.closed = events, False

    def iter_lines(self, **_kwargs):
        for event in self.events:
            yield 'data: ' + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False))
            yield ''

    def close(self):
        self.closed = True

    def iter_content(self, **_kwargs):
        for line in self.iter_lines():
            yield (line + '\n').encode('utf-8')


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def model_for(events, *, provider='openai', name='vision'):
    from desktop_control.streaming import DesktopStreamModel
    response = Response(events)
    session = Session(response)
    model = DesktopStreamModel({'provider': provider, 'base_url': 'http://127.0.0.1:1234/v1',
                                'model': name, 'key': 'test'}, session=session)
    return model, session, response


def test_openai_stream_receives_chunks_and_ignores_reasoning():
    model, session, response = model_for([
        {'choices': [{'delta': {'reasoning_content': '不应展示的内部内容'}}]},
        {'choices': [{'delta': {'content': '{"message":"正在'}}]},
        {'choices': [{'delta': {'content': '点击","name":"click","x":1,"y":2}'}, 'finish_reason': 'stop'}]},
        '[DONE]',
    ])
    chunks = []
    text = model.chat('目标', system_prompt='操作协议', image_b64='photo', on_chunk=chunks.append)
    assert text == '{"message":"正在点击","name":"click","x":1,"y":2}'
    assert chunks == ['{"message":"正在', '点击","name":"click","x":1,"y":2}']
    url, options = session.calls[0]
    assert url.endswith('/chat/completions')
    assert options['stream'] is True and options['json']['stream'] is True
    assert options['json']['messages'][0]['content'] == '操作协议'
    assert options['json']['messages'][1]['content'][1]['image_url']['url'].endswith('photo')
    assert options['json']['max_tokens'] == 2048 and response.closed


def test_gemini_uses_native_stream_endpoint_and_drops_thought_parts():
    model, session, response = model_for([
        {'candidates': [{'content': {'parts': [{'text': 'hidden', 'thought': True}, {'text': '{"name":'}]}}]},
        {'candidates': [{'content': {'parts': [{'text': '"ask","text":"选哪个？"}'}]}, 'finishReason': 'STOP'}]},
    ], provider='gemini')
    chunks = []
    assert model.chat('目标', image_b64='picture', on_chunk=chunks.append) == '{"name":"ask","text":"选哪个？"}'
    url, options = session.calls[0]
    assert url.endswith('/models/vision:streamGenerateContent?alt=sse')
    assert options['json']['generationConfig']['maxOutputTokens'] == 2048
    assert options['json']['contents'][0]['parts'][1]['inline_data']['data'] == 'picture'
    assert 'hidden' not in ''.join(chunks) and response.closed


@pytest.mark.parametrize('events', [
    [{'choices': [{'delta': {'content': '{"name":"click","x":1,"y":2}'}}]}],
    [{'choices': [{'delta': {'content': '{"name":"click","x":1,"y":2}'}, 'finish_reason': 'length'}]}, '[DONE]'],
    [{'error': {'message': 'upstream secret'}}],
])
def test_unfinished_truncated_or_error_stream_never_returns_action(events):
    model, session, response = model_for(events)
    with pytest.raises(ValueError):
        model.chat('目标')
    assert len(session.calls) == 1 and response.closed


def test_cancel_during_stream_closes_response_without_retry():
    cancel = threading.Event()
    model, session, response = model_for([
        {'choices': [{'delta': {'content': '{"message":"部分'}}]},
        {'choices': [{'delta': {'content': '内容"}'}, 'finish_reason': 'stop'}]}, '[DONE]',
    ])
    with pytest.raises(ValueError, match='cancelled'):
        model.chat('目标', on_chunk=lambda _text: cancel.set(), cancel=cancel)
    assert response.closed and len(session.calls) == 1


def test_gemini_truncation_is_rejected_even_when_text_is_valid_json():
    model, _session, response = model_for([
        {'candidates': [{'content': {'parts': [{'text': '{"name":"click","x":1,"y":2}'}]}, 'finishReason': 'MAX_TOKENS'}]},
    ], provider='gemini')
    with pytest.raises(ValueError, match='截断'):
        model.chat('目标')
    assert response.closed


def test_multiline_sse_event_and_heartbeat():
    model, session, response = model_for([])
    def lines(**_kwargs):
        yield ': keep-alive'
        yield ''
        yield 'data: {"choices":'
        yield 'data: [{"delta":{"content":"结果"},"finish_reason":"stop"}]}'
        yield ''
        yield 'data: [DONE]'
        yield ''
    response.iter_lines = lines
    assert model.chat('目标') == '结果'
    assert len(session.calls) == 1


def test_pretend_json_response_is_reported_without_nonstream_retry():
    model, session, response = model_for([])
    response.headers = {'Content-Type': 'application/json'}
    response.json = lambda: {'choices': []}
    with pytest.raises(ValueError, match='SSE'):
        model.chat('目标')
    assert response.closed and len(session.calls) == 1


def test_gemini_done_sentinel_without_native_finish_cannot_complete_action():
    model, _session, response = model_for([
        {'candidates': [{'content': {'parts': [{'text': '{"name":"click","x":1,"y":2}'}]}}]}, '[DONE]',
    ], provider='gemini')
    with pytest.raises(ValueError, match='未完成'):
        model.chat('目标')
    assert response.closed


def test_loopback_utf8_chunk_arrives_before_server_finishes():
    from desktop_control.streaming import DesktopStreamModel
    received, callback_seen = [], threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append(payload)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.end_headers()
            first = json.dumps({'choices': [{'delta': {'content': '{"message":"中文'}}]}, ensure_ascii=False)
            self.wfile.write(('data: ' + first + '\n\n').encode())
            self.wfile.flush()
            assert callback_seen.wait(2), '首片段未及时回调，不能等服务端完整响应才显示'
            last = json.dumps({'choices': [{'delta': {'content': '完成","name":"ask","text":"继续？"}'}, 'finish_reason': 'stop'}]}, ensure_ascii=False)
            self.wfile.write(('data: ' + last + '\n\ndata: [DONE]\n\n').encode())
            self.wfile.flush()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    model = DesktopStreamModel({'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'vision'})
    chunks = []
    def chunk(value):
        chunks.append(value)
        callback_seen.set()
    try:
        answer = model.chat('目标', on_chunk=chunk)
        assert json.loads(answer)['message'] == '中文完成'
        assert len(chunks) == 2 and received[0]['stream'] is True
    finally:
        model.session.close()
        server.shutdown()
        server.server_close()


def test_invalid_wire_utf8_cannot_be_replaced_and_typed():
    from desktop_control.streaming import DesktopStreamModel
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            # JSON文本本身是动作，但传输字节损坏，不得替换成U+FFFD再执行。
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"{\\"name\\":\\"type\\",\\"text\\":\\"A\xffB\\"}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    model = DesktopStreamModel({'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'model': 'vision'})
    try:
        with pytest.raises(ValueError, match='UTF-8'):
            model.chat('目标')
    finally:
        model.session.close()
        server.shutdown()
        server.server_close()


def test_missing_newline_has_bounded_buffer_before_consuming_entire_response():
    model, _session, response = model_for([])
    consumed = []
    def content(**_kwargs):
        yield b'data: '
        for index in range(400):
            consumed.append(index)
            yield b'x' * 1024
    response.iter_content = content
    with pytest.raises(ValueError, match='过大'):
        model.chat('目标')
    assert len(consumed) < 260 and response.closed


def test_heartbeats_do_not_obey_old_total_stream_deadline(monkeypatch):
    from desktop_control import streaming
    model, _session, response = model_for([])
    stamp = [0.]
    monkeypatch.setattr(streaming.time, 'monotonic', lambda: stamp[0])
    def content(**_kwargs):
        for _index in range(10):
            stamp[0] += 30
            yield b': keep-alive\n\n'
        yield b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    response.iter_content = content
    assert model.chat('目标') == 'ok'
    assert response.closed
