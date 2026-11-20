"""在请求专用提供商副本的实际查询边界落实预算，绝不改共享提供商。"""

import copy
import json
from types import MethodType


class QAGuardError(ValueError):
    desktop_safe_error = True


async def bounded_generate(provider, messages, system, policy):
    kind = (
        str(provider.provider_config.get("type", "")) + " " + type(provider).__module__
    ).lower()
    if not any(
        name in kind for name in ("openai", "gemini", "google_genai", "anthropic")
    ) or not callable(getattr(provider, "_query", None)):
        raise QAGuardError(
            "当前云端提供商尚无可靠的输出预算适配，请改用OpenAI兼容、Gemini或Anthropic提供商"
        )
    request = copy.copy(provider)
    request.provider_config = copy.deepcopy(provider.provider_config)
    extras = request.provider_config.get("custom_extra_body", {})
    protected = (
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "maxOutputTokens",
        "messages",
        "model",
        "system",
        "system_prompt",
        "contents",
        "systemInstruction",
        "generationConfig",
    )
    if isinstance(extras, dict):
        request.provider_config["custom_extra_body"] = {
            k: v for k, v in extras.items() if k not in protected
        }
    if hasattr(request, "api_keys"):
        request.api_keys = copy.copy(request.api_keys)
    query = request._query
    model = provider.get_model()
    is_openai = "openai" in kind
    name = model.lower().rsplit("/", 1)[-1]
    field = (
        "max_completion_tokens"
        if is_openai and name.startswith(("o1", "o3", "o4", "gpt-5"))
        else "max_tokens"
    )
    attempts = 0
    if any(name in kind for name in ("gemini", "google_genai")):
        prepare_config = getattr(request, "_prepare_query_config", None)
        if not callable(prepare_config):
            raise QAGuardError(
                "当前云端Gemini版本缺少可靠的人格/内部重试保护适配，请更新提供商或使用兼容接口"
            )
        configurations = 0

        async def config_once(
            _self, payloads, tools, tool_choice, system_instruction, *args, **kwargs
        ):
            nonlocal configurations
            if configurations:
                raise QAGuardError(
                    "云端Gemini未完成本次生成，已阻止内部重复调用；可手动重试"
                )
            if (system_instruction or "") != system:
                raise QAGuardError("云端提供商试图改变冻结人格，已停止生成")
            configurations += 1
            return await prepare_config(
                payloads, tools, tool_choice, system_instruction, *args, **kwargs
            )

        request._prepare_query_config = MethodType(config_once, request)

    def capped_body(payload):
        nonlocal attempts
        if attempts:
            raise QAGuardError("云端模型本次生成未完成，未自动重复调用；可手动重试")
        attempts += 1
        body = copy.deepcopy(payload)
        for key in protected[:4]:
            body.pop(key, None)
        body.update(model=model, **{field: policy["output_tokens"]})
        if (
            len(
                json.dumps(
                    {
                        "payload": body,
                        "extra": request.provider_config.get("custom_extra_body", {}),
                    },
                    ensure_ascii=True,
                ).encode("utf-8")
            )
            + 65536
            > policy["max_bytes"]
        ):
            raise QAGuardError(
                "当前问题实际云端请求超过10MiB，未调用模型；请减少截图或内容"
            )
        return body

    async def capped_query(_self, payload, *args, **kwargs):
        return await query(capped_body(payload), *args, **kwargs)

    request._query = MethodType(capped_query, request)
    stream_query = getattr(request, "_query_stream", None)
    if callable(stream_query):

        async def capped_stream(_self, payload, *args, **kwargs):
            body = capped_body(payload)
            async for chunk in stream_query(body, *args, **kwargs):
                yield chunk

        request._query_stream = MethodType(capped_stream, request)
    return await request.text_chat(
        contexts=copy.deepcopy(messages),
        system_prompt=system,
        model=model,
        request_max_retries=1,
    )
