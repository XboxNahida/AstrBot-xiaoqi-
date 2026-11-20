"""只使用模拟及本机HTTP服务验证模型传输。"""
import json
from types import SimpleNamespace

import pytest
import requests


def test_stream_request_sets_idle_timeout_and_collects_native_usage():
    from model_transport import stream_request
    calls, chunks = [], []
    class Response:
        status_code = 200
        headers = {'Content-Type': 'text/event-stream'}  # noqa: RUF012 -- 只读测试响应。
        closed = False
        def iter_content(self, **kwargs):
            for data in [': heartbeat',
                         'data: ' + json.dumps({'candidates': [{'content': {'parts': [{'text': '内部', 'thought': True}, {'text': '你好'}]}}]}),
                         'data: ' + json.dumps({'candidates': [{'finishReason': 'STOP'}], 'usageMetadata': {'totalTokenCount': 9}})]:
                yield (data + '\n\n').encode()
        def close(self):
            self.closed = True
    response = Response()
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response
    data = stream_request(SimpleNamespace(post=post), 'https://example.com/v1beta/models/g:generateContent',
                          {'contents': []}, {}, 17, gemini=True, on_chunk=chunks.append)
    assert calls[0][0].endswith(':streamGenerateContent?alt=sse')
    assert calls[0][1]['timeout'] == (8, 17)
    assert calls[0][1]['stream'] is True
    assert chunks == ['你好'] and response.closed
    assert data['usageMetadata']['totalTokenCount'] == 9
    assert data['candidates'][0]['content']['parts'] == [{'text': '你好'}]


def test_service_error_explains_status_without_exposing_credentials():
    from model_transport import response_error
    response = SimpleNamespace(status_code=503, json=lambda: {'error': {'message':
        'Gemini overloaded; key=secret-value; https://bad.test/?token=secret-value'}})
    message = response_error(response, secrets=['secret-value'])
    assert '503' in message and 'overloaded' in message
    assert 'secret-value' not in message and 'https://' not in message


def test_official_gemini_array_error_is_visible_and_does_not_retry():
    from model_transport import stream_request
    calls = []
    response = SimpleNamespace(status_code=400, json=lambda: [{
        'error': {'code': 400, 'status': 'INVALID_ARGUMENT',
                  'message': 'Request contains an invalid argument. key=private-relay; https://private.test'}}],
        close=lambda: None)
    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return response
    with pytest.raises(ValueError) as captured:
        stream_request(SimpleNamespace(post=post), 'https://relay.example/v1/chat/completions',
                       {}, {'Authorization': 'Bearer private-relay'}, 17)
    message = str(captured.value)
    assert 'invalid argument' in message
    assert 'private-relay' not in message and 'https://' not in message
    assert len(calls) == 1


def test_read_timeout_and_unfinished_stream_are_not_success():
    from model_transport import stream_request
    def post(*args, **kwargs):
        raise requests.ReadTimeout('secret request URL')
    with pytest.raises(ValueError, match='连续17秒未收到任何数据'):
        stream_request(SimpleNamespace(post=post), 'https://example.com/v1/chat/completions', {}, {}, 17)
    response = SimpleNamespace(status_code=200, headers={'Content-Type': 'text/event-stream'},
        iter_content=lambda **kw: iter([b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n']), close=lambda: None)
    with pytest.raises(ValueError, match='未完成'):
        stream_request(SimpleNamespace(post=lambda *a, **kw: response), 'https://example.com/v1/chat/completions', {}, {}, 17)


@pytest.mark.parametrize('silent', [False, True])
def test_real_socket_timeout_counts_all_data_not_total_duration(silent):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from model_transport import stream_request
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            try:
                # 心跳仅原始字节，没有换行；总耗时超过无数据超时四倍。
                for _ in range(8):
                    time.sleep(.07)
                    if not silent:
                        self.wfile.write(b':')
                        self.wfile.flush()
                self.wfile.write(b'\n\ndata: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with requests.Session() as session:
            url = f'http://127.0.0.1:{server.server_port}/chat/completions'
            if silent:
                with pytest.raises(ValueError, match='未收到任何数据'):
                    stream_request(session, url, {}, {}, .2)
            else:
                assert stream_request(session, url, {}, {}, .2)['choices'][0]['message']['content'] == 'ok'
    finally:
        server.shutdown()
        server.server_close()
