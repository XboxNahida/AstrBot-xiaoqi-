"""独立多轮图文答疑客户端；不使用锐评的短输出模板。"""
import base64
import copy
import json
from urllib.parse import quote

from cloud_plugin.astrbot_plugin_desktop_link.context_policy import ContextPlanner
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
from companion_models import DirectModels
from model_transport import (
    data_error,
    empty_reason,
    network_error,
    response_error,
    sse_events,
)

_CONTEXT = ContextPlanner()


def build_messages(messages, image_reader, *, enforce_limit=True):
    output = []
    total = 0
    for message in messages:
        total += len(message['text'].encode('utf-8'))
        content = [{"type": "text", "text": message["text"]}]
        for image in message.get("images", []):
            raw = image_reader(image)
            total += len(raw) * 4 // 3 + 4
            if enforce_limit and total > 10 * 1024 * 1024:
                raise ValueError('本轮图文上下文超过10MiB，请开启新对话；原历史仍完整保留')
            encoded = base64.b64encode(raw).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}})
        output.append({"role": message["role"], "content": content if len(content) > 1 else message["text"]})
    if enforce_limit and total > 10 * 1024 * 1024:
        raise ValueError('本轮上下文过长，请开启新对话；原历史仍保留')
    return output


class QAClient:
    def __init__(self, profile, system):
        if not profile or not str(profile.get('model', '')).strip():
            raise ValueError('请先在原设置或云端与模型页填写模型名称')
        self.model = DirectModels(profile)
        self.system = system
        self.policy = profile.get('_qa_limits') or limits_for(profile)
        self.telemetry = {}
        if profile.get('access_token'):
            self.model.headers['Authorization'] = 'Bearer ' + profile['access_token']

    def chat(self, messages, on_chunk=None, *, image_reader=None):
        model = self.model
        prepared = _CONTEXT.prepare(messages, self.system, self.policy, image_reader=image_reader)
        messages = prepared['messages']
        self.telemetry = {**prepared['stats'], 'cached_tokens': None, 'prompt_tokens': None}
        if model.gemini:
            contents = []
            for message in messages:
                parts = []
                content = message["content"]
                for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
                    if part["type"] == "text":
                        parts.append({"text": part["text"]})
                    else:
                        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                                       "data": part["image_url"]["url"].split(",", 1)[1]}})
                contents.append({"role": "model" if message["role"] == "assistant" else "user", "parts": parts})
            payload = {"contents": contents,
                                  "systemInstruction": {"parts": [{"text": self.system}]},
                                  "generationConfig": {"maxOutputTokens": self.policy['output_tokens']}}
            self._guard_payload(payload)
            data = model._request("post", "/models/" + quote(model.profile["model"].removeprefix("models/"), safe='') +
                                  ":generateContent", json=payload, on_chunk=on_chunk)
            self._usage(data)
            self.telemetry['truncated'] = any(r.get('finishReason') == 'MAX_TOKENS' for r in data.get('candidates', []))
            reply = "".join(part.get("text", "") for row in data.get("candidates", [])[:1]
                            for part in row.get("content", {}).get("parts", []) if not part.get("thought"))
        else:
            import requests
            payload = {"model": model.profile["model"], "messages":
                       [{"role": "system", "content": self.system}] + copy.deepcopy(messages), 'max_tokens': self.policy['output_tokens'],
                       "stream": True}
            # 自定义兼容网关未必支持stream_options，不为获取统计额外重试生成。
            if model.profile.get('stream_usage', True):
                payload['stream_options'] = {'include_usage': True}
            name = model.profile['model'].lower().rsplit('/', 1)[-1]
            if name.startswith(('o1', 'o3', 'o4', 'gpt-5')):
                payload['max_completion_tokens'] = payload.pop('max_tokens')
            self._guard_payload(payload)
            try:
                with model.session.post(model.base + "/chat/completions", json=payload, headers=model.headers,
                                        timeout=(min(8, model.timeout_seconds), model.timeout_seconds), stream=True, allow_redirects=False) as response:
                    if response.status_code != 200:
                        raise ValueError(response_error(response, [model.profile.get('key', '')]))
                    chunks = []
                    length = 0
                    finished = False
                    response.encoding = "utf-8"
                    if 'application/json' in response.headers.get('Content-Type', ''):
                        data = response.json()
                        if data_error(data, [model.profile.get('key', '')]):
                            raise ValueError(data_error(data, [model.profile.get('key', '')]))
                        self._usage(data)
                        self.telemetry['truncated'] = any(r.get('finish_reason') == 'length' for r in data.get('choices', []))
                        reply = ((data.get('choices') or [{}])[0].get('message') or {}).get('content', '')
                        if not isinstance(reply, str) or not reply.strip():
                            raise ValueError(empty_reason(data))
                        if len(reply) > 120000:
                            raise ValueError('答疑输出过长，已停止接收')
                        if on_chunk:
                            on_chunk(reply)
                        return reply
                    for raw in sse_events(response):
                        if raw == "[DONE]":
                            finished = True
                            break
                        data = json.loads(raw)
                        if data.get('error'):
                            raise ValueError(data_error(data, [model.profile.get('key', '')]))
                        self._usage(data)
                        choices = data.get("choices") or []
                        if choices and choices[0].get('finish_reason'):
                            finished = True
                        if choices and choices[0].get('finish_reason') == 'length':
                            self.telemetry['truncated'] = True
                        text = (choices[0].get("delta") or {}).get("content") if choices else None
                        if isinstance(text, str):
                            chunks.append(text)
                            length += len(text)
                            if length > 120000:
                                raise ValueError("答疑输出过长，已停止接收")
                            if on_chunk:
                                on_chunk(text)
                    if not finished:
                        raise ValueError('答疑流式回复未完成，内容未归档；可手动重试')
                    reply = "".join(chunks)
            except json.JSONDecodeError as exc:
                raise ValueError('答疑流式响应格式不兼容，本次回复未完成') from exc
            except requests.RequestException as exc:
                raise ValueError(network_error(exc, model.timeout_seconds)) from exc
        if len(reply) > 120000:
            raise ValueError('答疑输出过长，已停止接收')
        if not reply.strip():
            raise ValueError(empty_reason(data, model.gemini))
        return reply

    def _guard_payload(self, payload):
        if len(json.dumps(payload, ensure_ascii=True).encode('utf-8')) > self.policy['max_bytes']:
            raise ValueError('当前问题实际请求超过10MiB，未调用模型；请减少截图或内容')

    def _usage(self, data):
        usage = data.get('usage') or data.get('usageMetadata') or {}
        prompt = usage.get('prompt_tokens', usage.get('promptTokenCount'))
        cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens',
                  usage.get('cachedContentTokenCount', usage.get('prompt_cache_hit_tokens')))
        if type(prompt) is int and prompt >= 0:
            self.telemetry['prompt_tokens'] = prompt
        if type(cached) is int and cached >= 0:
            self.telemetry['cached_tokens'] = cached
