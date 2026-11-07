"""无真实模型请求的后台流式、超时与取消验证。"""
import asyncio
from types import SimpleNamespace

import pytest

try:
    from task_model_stream import collect_task_model_response
except ImportError:
    async def collect_task_model_response(*args, **kwargs):
        raise AssertionError("尚未实现后台流式收集")


class StreamProvider:
    supports_stream_activity_callback = True

    def __init__(self, *, mode="active", count=8):
        self.mode, self.count = mode, count
        self.closed = False
        self.kwargs = None
        self.final = SimpleNamespace(is_chunk=False, completion_text="完整结果", calls=["完整参数"])

    async def text_chat_stream(self, **kwargs):
        self.kwargs = kwargs
        try:
            if self.mode == "first_stall":
                await asyncio.sleep(10)
            for _ in range(self.count):
                kwargs["stream_activity_callback"]()
                if self.mode == "mid_stall":
                    await asyncio.sleep(10)
                await asyncio.sleep(0.01)
                if self.mode == "text":
                    yield SimpleNamespace(is_chunk=True, completion_text="正文片段")
            if self.mode != "truncated":
                yield self.final
        finally:
            self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["active", "text"])
async def test_continuous_tool_argument_or_text_activity_can_exceed_idle_window(mode):
    provider = StreamProvider(mode=mode)
    result = await collect_task_model_response(provider, idle_seconds=0.04, total_seconds=0.5)
    assert result is provider.final
    assert provider.closed
    assert provider.kwargs["timeout"] == 0.04
    assert provider.kwargs["request_max_retries"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["first_stall", "mid_stall"])
async def test_first_and_midstream_silence_timeout_and_close(mode):
    provider = StreamProvider(mode=mode)
    with pytest.raises(TimeoutError, match="idle"):
        await collect_task_model_response(provider, idle_seconds=0.03, total_seconds=0.5)
    assert provider.closed


@pytest.mark.asyncio
async def test_total_boundary_still_stops_an_endless_active_stream():
    provider = StreamProvider(count=1000)
    with pytest.raises(TimeoutError, match="total"):
        await collect_task_model_response(provider, idle_seconds=0.04, total_seconds=0.09)
    assert provider.closed


@pytest.mark.asyncio
async def test_partial_stream_is_never_a_complete_response():
    provider = StreamProvider(mode="truncated", count=1)
    with pytest.raises(RuntimeError, match="incomplete"):
        await collect_task_model_response(provider, idle_seconds=0.04, total_seconds=0.5)
    assert provider.closed


@pytest.mark.asyncio
async def test_external_cancellation_closes_stream_without_becoming_timeout():
    provider = StreamProvider(mode="first_stall")
    task = asyncio.create_task(collect_task_model_response(provider))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.closed


@pytest.mark.asyncio
async def test_default_deadlines_are_sixty_idle_and_six_hundred_total(monkeypatch):
    observed = []
    real_timeout = asyncio.timeout
    def timeout(delay):
        observed.append(delay)
        return real_timeout(delay)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    provider = StreamProvider(count=0)
    await collect_task_model_response(provider)
    assert 60 in observed
    assert provider.kwargs["timeout"] == 60


@pytest.mark.asyncio
async def test_consumer_processing_is_not_model_network_silence():
    from astrbot.core.provider.streaming import iter_model_response

    provider = StreamProvider(mode="text", count=2)
    stream = iter_model_response(provider, idle_seconds=0.04, total_seconds=1)
    try:
        async for _ in stream:
            await asyncio.sleep(0.06)
    finally:
        await stream.aclose()
    assert provider.closed
