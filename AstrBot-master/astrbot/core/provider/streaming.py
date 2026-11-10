"""模型请求统一使用流式传输，按分片活动判断超时。"""

from __future__ import annotations

import asyncio
import math

MODEL_IDLE_TIMEOUT_SECONDS = 60
MODEL_TOTAL_TIMEOUT_SECONDS = 600


class ModelStreamTimeout(TimeoutError):
    """只暴露超时类型，不携带模型请求内容或凭据。"""


async def iter_model_response(
    provider, *, idle_seconds=None,
    total_seconds=MODEL_TOTAL_TIMEOUT_SECONDS,
    idle_timeout_cap=MODEL_IDLE_TIMEOUT_SECONDS, **kwargs,
):
    """保护整个流的首包、途中停顿、总边界和取消清理。"""
    if idle_seconds is None:
        configured = kwargs.get("timeout") or getattr(provider, "timeout", None)
        try:
            idle_seconds = float(configured)
        except (TypeError, ValueError):
            idle_seconds = MODEL_IDLE_TIMEOUT_SECONDS
    if not math.isfinite(idle_seconds) or idle_seconds <= 0:
        idle_seconds = MODEL_IDLE_TIMEOUT_SECONDS
    idle_seconds = min(idle_seconds, MODEL_IDLE_TIMEOUT_SECONDS, idle_timeout_cap)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_seconds
    idle_guard = None
    external_activity = kwargs.pop("stream_activity_callback", None)
    kwargs["timeout"] = idle_seconds
    def activity():
        if idle_guard is not None and not idle_guard.expired():
            idle_guard.reschedule(loop.time() + idle_seconds)
        if external_activity is not None:
            external_activity()

    if getattr(provider, "supports_stream_activity_callback", False) is True:
        kwargs["stream_activity_callback"] = activity
    stream = provider.text_chat_stream(**kwargs)
    try:
        while True:
            total_guard = asyncio.timeout_at(deadline)
            idle_guard = asyncio.timeout(idle_seconds)
            try:
                async with total_guard, idle_guard:
                    response = await anext(stream)
            except StopAsyncIteration:
                return
            except ModelStreamTimeout:
                raise
            except TimeoutError:
                kind = "total" if total_guard.expired() else "idle"
                raise ModelStreamTimeout(kind) from None
            finally:
                idle_guard = None
            # 消费者发送消息或处理分片时，不运行网络空闲计时器。
            yield response
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


async def collect_model_response(provider, **kwargs):
    """沿用一次完整响应的调用契约，内部以流式接收，不执行半截工具调用。"""
    final = None
    stream = iter_model_response(provider, **kwargs)
    try:
        async for response in stream:
            if not getattr(response, "is_chunk", False):
                final = response
    finally:
        await stream.aclose()
    if final is None:
        raise RuntimeError("incomplete model stream")
    return final
