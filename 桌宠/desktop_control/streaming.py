"""操作规划专用双协议SSE；只收正文，不执行动作、不重试生成。"""
import json
import re
import threading
import time
from urllib.parse import quote

import requests

from companion_models import DirectModels
from model_transport import data_error, network_error, response_error


class DesktopStreamModel(DirectModels):
    def chat(self, text, *, system_prompt='', image_b64=None, on_chunk=None, cancel=None):
        cancel = cancel or threading.Event()
        model = self.profile.get('model', '').strip()
        if not model:
            raise ValueError('请先选择或填写操作模型')
        if cancel.is_set():
            raise ValueError('cancelled')
        if self.gemini:
            parts = [{'text': text}]
            if image_b64:
                parts.append({'inline_data': {'mime_type': 'image/jpeg', 'data': image_b64}})
            body = {'contents': [{'role': 'user', 'parts': parts}],
                    'systemInstruction': {'parts': [{'text': system_prompt}]},
                    'generationConfig': {'maxOutputTokens': 2048}}
            path = '/models/' + quote(model.removeprefix('models/'), safe='') + ':streamGenerateContent?alt=sse'
        else:
            content = text if not image_b64 else [
                {'type': 'text', 'text': text},
                {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image_b64}},
            ]
            token_key = 'max_completion_tokens' if re.match(r'(?:o[134](?:-|$)|gpt-5)', model.lower().rsplit('/', 1)[-1]) else 'max_tokens'
            body = {'model': model, 'messages': [{'role': 'system', 'content': system_prompt},
                                                {'role': 'user', 'content': content}],
                    'stream': True, token_key: 2048}
            if self.profile.get('stream_usage', True):
                body['stream_options'] = {'include_usage': True}
            path = '/chat/completions'
        if len(json.dumps(body, ensure_ascii=True).encode('utf-8')) > 10 * 1024 * 1024:
            raise ValueError('操作请求超过10MiB，未请求模型')
        response, done = None, threading.Event()
        # 模型等待只按socket连续无数据计时；整任务截止由运行时独立管理。
        deadline = float('inf')
        try:
            response = self.session.post(self.base + path, json=body, headers=self.headers,
                timeout=(min(8, self.timeout_seconds), self.timeout_seconds), stream=True, allow_redirects=False)
            if cancel.is_set():
                raise ValueError('cancelled')
            if response.status_code != 200:
                raise ValueError(response_error(response, [self.profile.get('key', '')]))
            if 'application/json' in response.headers.get('Content-Type', '').lower():
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ValueError('操作接口返回无效JSON，未执行动作') from exc
                error = data_error(data, [self.profile.get('key', '')])
                if error:
                    raise ValueError(error)
                raise ValueError('操作模型未提供SSE流式响应，请使用支持流式的接口')
            response.encoding = 'utf-8'
            # 阻塞读取时也能由取消信号关闭旧连接，不占用Qt主线程。
            def watch_cancel():
                while not done.wait(.05):
                    if cancel.is_set():
                        response.close()
                        return
            threading.Thread(target=watch_cancel, name='桌面流式取消', daemon=True).start()
            chunks, size, finished = [], 0, False
            for raw in self._events(response, cancel, deadline):
                if raw == '[DONE]':
                    # Gemini必须使用自身finishReason，不能拿兼容协议哨兵当作完成。
                    if not self.gemini:
                        finished = True
                    break
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise ValueError('操作流式响应格式无效，未执行动作') from exc
                if not isinstance(data, dict) or data.get('error'):
                    raise ValueError(data_error(data, [self.profile.get('key', '')]) or '操作流式响应格式无效，未执行动作')
                if self.gemini:
                    rows = data.get('candidates') or []
                    row = rows[0] if rows else {}
                    reason = row.get('finishReason')
                    parts = row.get('content', {}).get('parts', [])
                    delta = ''.join(p.get('text', '') for p in parts
                                    if not p.get('thought') and isinstance(p.get('text', ''), str))
                    if data.get('promptFeedback', {}).get('blockReason'):
                        raise ValueError('操作模型拒绝本次请求，未执行动作')
                else:
                    rows = data.get('choices') or []
                    row = rows[0] if rows else {}
                    reason = row.get('finish_reason')
                    delta = (row.get('delta') or {}).get('content') or ''
                if reason:
                    if reason not in ('STOP', 'stop'):
                        raise ValueError('操作模型输出被截断或未正常结束，未执行动作')
                    finished = True
                if isinstance(delta, str) and delta:
                    chunks.append(delta)
                    size += len(delta)
                    if size > 12000:
                        raise ValueError('操作模型输出过长，未执行动作')
                    if on_chunk:
                        on_chunk(delta)
                if cancel.is_set():
                    raise ValueError('cancelled')
            if cancel.is_set():
                raise ValueError('cancelled')
            if not finished or not ''.join(chunks).strip():
                raise ValueError('操作流式回复未完成或没有正文，未执行动作')
            return ''.join(chunks)
        except requests.RequestException as exc:
            if cancel.is_set():
                raise ValueError('cancelled') from exc
            raise ValueError(network_error(exc, self.timeout_seconds) + '；未执行动作') from exc
        finally:
            done.set()
            if response is not None:
                response.close()

    @staticmethod
    def _events(response, cancel, deadline):
        lines, size = [], 0
        for line in DesktopStreamModel._lines(response, cancel, deadline):
            if cancel.is_set():
                raise ValueError('cancelled')
            if not line:
                if lines:
                    yield '\n'.join(lines)
                lines, size = [], 0
            elif line.startswith('data:'):
                value = line[5:].lstrip(' ')
                size += len(value)
                if size > 256000:
                    raise ValueError('操作流式事件过大，未执行动作')
                lines.append(value)
        if lines:
            yield '\n'.join(lines)

    @staticmethod
    def _lines(response, cancel, deadline):
        pending, total = bytearray(), 0
        # 小块实时读取；不能交给iter_lines无界积累未换行正文。
        for chunk in response.iter_content(chunk_size=1):
            if cancel.is_set():
                raise ValueError('cancelled')
            if time.monotonic() >= deadline:
                raise ValueError('操作流式响应超过时限，未执行动作')
            total += len(chunk)
            if total > 10 * 1024 * 1024:
                raise ValueError('操作流式响应过大，未执行动作')
            if len(pending) + len(chunk) > 256000:
                raise ValueError('操作流式行过大，未执行动作')
            pending.extend(chunk)
            if b'\n' not in chunk:
                continue
            pieces = pending.split(b'\n')
            pending = bytearray(pieces.pop())
            for piece in pieces:
                try:
                    yield piece.rstrip(b'\r').decode('utf-8', errors='strict')
                except UnicodeError as exc:
                    raise ValueError('操作流式响应UTF-8损坏，未执行动作') from exc
        if pending:
            try:
                yield pending.rstrip(b'\r').decode('utf-8', errors='strict')
            except UnicodeError as exc:
                raise ValueError('操作流式响应UTF-8损坏，未执行动作') from exc
