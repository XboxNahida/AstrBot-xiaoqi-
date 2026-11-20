"""模型HTTP流式传输：以接收数据的空闲时间超时，不重试生成。"""
import copy
import json
import re

import requests


class ModelTransportError(ValueError):
    def __init__(self, message, result):
        super().__init__(message)
        self.result = {k: result[k] for k in ('usage', 'usageMetadata') if isinstance(result.get(k), dict)}


def idle_timeout(profile, default=120):
    value = profile.get('timeout_seconds', default)
    if type(value) is not int or not 5 <= value <= 600:
        raise ValueError('模型无数据超时应为5至600秒整数')
    return value


def safe_detail(value, secrets=()):
    text = value if isinstance(value, str) else ''
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'https?://\S+', '[地址已隐藏]', text, flags=re.IGNORECASE)
    text = re.sub(r'(?i)\bBearer\s+\S+|\b(?:sk-|AIza)[\w-]+', '[已隐藏]', text)
    text = re.sub(r'(?i)(["\']?(?:api[_-]?key|key|token|authorization|secret)["\']?\s*[:=]\s*)[^\s,;]+', r'\1[已隐藏]', text)
    if '<html' in text.lower() or '<!doctype' in text.lower():
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', '', text)).strip()[:240]


def data_error(data, secrets=()):
    # Gemini的流式HTTP失败可能返回[{"error": ...}]，同样保留已脱敏的具体原因。
    if isinstance(data, list):
        for item in data[:8]:
            if isinstance(item, dict) and (detail := data_error(item, secrets)):
                return detail
        return ''
    error = data.get('error') if isinstance(data, dict) else None
    if not error:
        return ''
    if isinstance(error, dict):
        detail = safe_detail(error.get('message', ''), secrets)
        code = safe_detail(str(error.get('code') or error.get('status') or ''), secrets)[:50]
    else:
        detail, code = safe_detail(error, secrets), ''
    return '服务商返回错误' + (f'（{code}）' if code else '') + ('：' + detail if detail else '，请检查服务状态')


def response_error(response, secrets=()):
    status = response.status_code
    hints = {400: '请求参数或模型不兼容', 401: '密钥无效或未填写', 403: '服务拒绝访问，请核对权限',
             404: '地址或模型不存在', 408: '中转站等待上游超时', 429: '限流、额度或余额不足',
             500: '中转站内部错误', 502: '中转站连接上游失败', 503: '模型繁忙或服务不可用', 504: '中转站等待上游超时'}
    try:
        detail = data_error(response.json(), secrets)
    except (ValueError, AttributeError, TypeError):
        detail = ''
    return f'接口返回HTTP {status}：{hints.get(status, "服务暂时无法处理请求")}' + ('；' + detail if detail else '')


def network_error(exc, seconds):
    pending, seen = [exc], set()
    while pending:
        item = pending.pop()
        if not isinstance(item, BaseException) or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, requests.ConnectTimeout):
            return '连接服务商超时，请检查网络、代理和接口地址'
        if isinstance(item, requests.ReadTimeout) or type(item).__name__ == 'ReadTimeoutError':
            return f'流式响应超时：连续{seconds}秒未收到任何数据；本次未自动重试'
        pending.extend([item.__cause__, item.__context__, *item.args])
    if isinstance(exc, requests.Timeout):
        return f'流式响应超时：连续{seconds}秒未收到任何数据；本次未自动重试'
    return '模型连接中断或网络不可用，本次回复未完成；未自动重试'


def empty_reason(data, gemini=False):
    rows = data.get('candidates' if gemini else 'choices') or [{}]
    reason = rows[0].get('finishReason' if gemini else 'finish_reason')
    blocked = (data.get('promptFeedback') or {}).get('blockReason')
    if blocked or reason in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'content_filter'):
        return '模型安全策略拦截本次请求，未返回可用正文'
    if reason in ('MAX_TOKENS', 'length'):
        return '模型生成额度已耗尽，未返回可用正文；请调整输出预算或思考强度'
    return '模型没有返回可显示的文字，本次未标记成功'


def sse_events(response):
    """有界读取UTF-8；注释/空行也会在底层socket读取时刷新空闲超时。"""
    pending, lines, total = bytearray(), [], 0
    def consume(line):
        nonlocal lines
        if not line:
            result, lines = '\n'.join(lines), []
            return result
        if line.startswith('data:'):
            lines.append(line[5:].lstrip(' '))
            if sum(map(len, lines)) > 256000:
                raise ValueError('模型流式事件过大，已停止接收')
        return ''
    # iter_lines仅用于轻量测试适配器；生产requests均有iter_content。
    if not hasattr(response, 'iter_content'):
        for line in response.iter_lines(decode_unicode=True, chunk_size=1):
            if line.startswith('data:') and lines:
                yield '\n'.join(lines)
                lines = []
            value = consume(line)
            if value:
                yield value
    else:
        for chunk in response.iter_content(chunk_size=1):
            total += len(chunk)
            if total > 20 * 1024 * 1024 or len(pending) + len(chunk) > 256000:
                raise ValueError('模型流式响应过大，已停止接收')
            pending.extend(chunk)
            if b'\n' not in chunk:
                continue
            pieces = pending.split(b'\n')
            pending = bytearray(pieces.pop())
            for piece in pieces:
                value = consume(piece.rstrip(b'\r').decode('utf-8', errors='strict'))
                if value:
                    yield value
        if pending:
            consume(pending.decode('utf-8', errors='strict'))
    if lines:
        yield '\n'.join(lines)


def stream_request(session, url, payload, headers, seconds, *, gemini=False, on_chunk=None, include_usage=True):
    body = copy.deepcopy(payload)
    if gemini:
        url = url.replace(':generateContent', ':streamGenerateContent')
        if 'alt=sse' not in url:
            url += ('&' if '?' in url else '?') + 'alt=sse'
    else:
        body['stream'] = True
        if include_usage:
            body['stream_options'] = {'include_usage': True}
    secrets = [headers.get('x-goog-api-key', ''), headers.get('Authorization', '').removeprefix('Bearer ')]
    response, result = None, {}
    try:
        response = session.post(url, json=body, headers=headers, timeout=(min(8, seconds), seconds),
                                stream=True, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(response_error(response, secrets))
        content_type = getattr(response, 'headers', {}).get('Content-Type', '').lower()
        if ('application/json' in content_type or not content_type and hasattr(response, 'json')
                or not hasattr(response, 'iter_lines') and not hasattr(response, 'iter_content')):
            data = response.json()  # 部分中转站忽略stream；接受单次JSON，不额外发起生成。
            if not isinstance(data, dict):
                raise ValueError('模型响应格式无效')
            result = data
            if data_error(data, secrets):
                raise ValueError(data_error(data, secrets))
            if on_chunk:
                if gemini:
                    text = ''.join(p.get('text', '') for r in data.get('candidates', [])[:1]
                                   for p in r.get('content', {}).get('parts', []) if not p.get('thought'))
                else:
                    text = ((data.get('choices') or [{}])[0].get('message') or {}).get('content')
                if isinstance(text, str) and text:
                    on_chunk(text)
            return data
        result, row, chunks, size, finished = {}, {}, [], 0, False
        for raw in sse_events(response):
            if raw == '[DONE]':
                finished = finished or not gemini
                break
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('模型流式响应格式无效')  # noqa: TRY004 -- 协议错误沿用调用方ValueError显示路径。
            for usage_key in ('usage', 'usageMetadata'):
                if isinstance(data.get(usage_key), dict):
                    result[usage_key] = data[usage_key]
            error = data_error(data, secrets)
            if error:
                raise ValueError(error)
            key = 'candidates' if gemini else 'choices'
            result.update({k: v for k, v in data.items() if k != key})
            rows = data.get(key) or []
            first = rows[0] if rows else {}
            row.update(first)
            reason = first.get('finishReason' if gemini else 'finish_reason')
            finished = finished or bool(reason) or bool(data.get('promptFeedback', {}).get('blockReason'))
            if gemini:
                text = ''.join(p.get('text', '') for p in first.get('content', {}).get('parts', [])
                               if not p.get('thought') and isinstance(p.get('text', ''), str))
            else:
                text = (first.get('delta') or {}).get('content') or ''
            if isinstance(text, str) and text:
                chunks.append(text)
                size += len(text)
                if size > 120000:
                    raise ValueError('模型输出过长，已停止接收')
                if on_chunk:
                    on_chunk(text)
        if not finished:
            raise ValueError('模型流式回复未完成，未保存为完整回答；可手动重试')
        if gemini:
            row['content'] = {'parts': [{'text': ''.join(chunks)}]}
        else:
            row['message'] = {'content': ''.join(chunks)}
        result['candidates' if gemini else 'choices'] = [row]
        return result
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ModelTransportError('模型流式响应格式或UTF-8无效，本次回复未完成', result) from exc
    except requests.RequestException as exc:
        raise ModelTransportError(network_error(exc, seconds), result) from exc
    except ValueError as exc:
        raise ModelTransportError(str(exc), result) from exc
    finally:
        if response is not None and hasattr(response, 'close'):
            response.close()
