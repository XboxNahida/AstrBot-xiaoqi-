"""简单模型直连：兼容接口和 Gemini，模型文字不会当作设备指令执行。"""

from __future__ import annotations

from urllib.parse import quote

import requests

from companion_config import validate_endpoint
from model_transport import (
    data_error,
    empty_reason,
    idle_timeout,
    network_error,
    response_error,
    stream_request,
)


class DirectModels:
    def __init__(self, profile: dict, session=None):
        self.profile = dict(profile)
        self.session = session or requests.Session()
        self.base = validate_endpoint(profile.get("base_url", ""))
        self.base = self.base.removesuffix("/chat/completions")
        self.gemini = profile.get("provider") == "gemini"
        self.timeout_seconds = idle_timeout(profile)
        key = profile.get("key", "").strip()
        self.headers = {"Content-Type": "application/json"}
        if key:
            self.headers["x-goog-api-key" if self.gemini else "Authorization"] = (
                key if self.gemini else "Bearer " + key
            )

    def _request(self, method: str, path: str, **kwargs):
        if method == 'post' and 'json' in kwargs:
            return stream_request(self.session, self.base + path, kwargs['json'], self.headers,
                                  self.timeout_seconds, gemini=self.gemini, on_chunk=kwargs.get('on_chunk'),
                                  include_usage=self.profile.get('stream_usage', True))
        try:
            response = getattr(self.session, method)(
                self.base + path,
                headers=self.headers,
                timeout=(min(8, self.timeout_seconds), self.timeout_seconds),
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise ValueError(network_error(exc, self.timeout_seconds)) from exc
        if response.status_code != 200:
            raise ValueError(response_error(response, [self.profile.get('key', '')]))
        try:
            result = response.json()
        except ValueError as exc:
            raise ValueError("接口返回的不是有效JSON，请核对接口地址") from exc
        if not isinstance(result, dict):
            raise ValueError('模型接口响应格式无效')  # noqa: TRY004 -- 协议失败必须沿ValueError显示到界面。
        if data_error(result, [self.profile.get('key', '')]):
            raise ValueError(data_error(result, [self.profile.get('key', '')]))
        return result

    def models(self) -> list[str]:
        data = self._request("get", "/models")
        if self.gemini:
            result = [
                m["name"].removeprefix("models/")
                for m in data.get("models", [])
                if isinstance(m, dict)
                and "generateContent" in m.get("supportedGenerationMethods", [])
            ]
        else:
            result = [
                m["id"]
                for m in data.get("data", [])
                if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        if not result:
            raise ValueError("未读取到模型列表，可以直接填写模型名称")
        return sorted(set(result))

    def chat(
        self,
        text: str,
        context: list | None = None,
        *,
        system_prompt: str = "",
        image_b64: str | None = None,
    ) -> str:
        model = self.profile.get("model", "").strip()
        if not model:
            raise ValueError("请先选择或填写模型名称")
        if not text.strip():
            raise ValueError("请输入消息")
        history = [
            m
            for m in (context or [])[-20:]
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
        ]
        if self.gemini:
            contents = [
                {
                    "role": "model" if m["role"] == "assistant" else "user",
                    "parts": [{"text": m["content"]}],
                }
                for m in history
            ]
            parts = [{"text": text}]
            if image_b64:
                parts.append(
                    {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}}
                )
            contents.append({"role": "user", "parts": parts})
            body = {"contents": contents}
            if system_prompt:
                body["systemInstruction"] = {"parts": [{"text": system_prompt}]}
            result = self._request(
                "post",
                "/models/"
                + quote(model.removeprefix("models/"), safe="")
                + ":generateContent",
                json=body,
            )
            candidates = result.get("candidates", [])
            answer = "".join(
                part.get("text", "")
                for candidate in candidates[:1]
                for part in candidate.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
        else:
            messages = (
                [{"role": "system", "content": system_prompt}] if system_prompt else []
            ) + history
            content = text
            if image_b64:
                content = [
                    {"type": "text", "text": text},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + image_b64},
                    },
                ]
            messages.append({"role": "user", "content": content})
            result = self._request(
                "post",
                "/chat/completions",
                json={"model": model, "messages": messages, "stream": False},
            )
            choices = result.get("choices", [])
            answer = choices[0].get("message", {}).get("content", "") if choices else ""
            if isinstance(answer, list):
                answer = "".join(
                    part.get("text", "") for part in answer if isinstance(part, dict)
                )
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(empty_reason(result, self.gemini))
        return answer.strip()

    def test(self) -> str:
        reply = self.chat("请只回答：连接正常。")
        return "连接成功：" + reply[:100]
