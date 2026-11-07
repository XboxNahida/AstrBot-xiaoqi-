import asyncio
from types import SimpleNamespace

import pytest

from astrbot.core.agent.response import AgentResponse
from astrbot.core.astr_agent_run_util import _simulated_stream_tts, run_agent
from astrbot.core.message.message_event_result import MessageChain


class _FakeEvent:
    """Minimal event surface used by the agent stream bridge."""

    def is_stopped(self) -> bool:
        return False

    def get_extra(self, key: str):
        del key
        return None

    def get_platform_name(self) -> str:
        return "test"


class _StreamingErrorRunner:
    """Agent runner that finishes with one provider error response."""

    streaming = True
    req = None

    def __init__(self, error_text: str) -> None:
        self.error_text = error_text
        self.finished = False
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=_FakeEvent()))

    async def step(self):
        self.finished = True
        yield AgentResponse(
            type="err",
            data={"chain": MessageChain().message(self.error_text)},
        )

    def done(self) -> bool:
        return self.finished


class _MalformedStreamingErrorRunner(_StreamingErrorRunner):
    """Agent runner that returns an invalid provider error payload."""

    async def step(self):
        self.finished = True
        yield AgentResponse(type="err", data={})


class _MultiStepRunner:
    streaming = False

    def __init__(self, *, autonomous):
        self.req = SimpleNamespace(model_managed_loop=autonomous, func_tool=object())
        self.count = 0
        self.aborted = False
        self.event = _FakeEvent()
        self.event.set_result = lambda _: None
        self.event.clear_result = lambda: None
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=self.event), messages=[])

    def done(self):
        return self.count >= 35 or self.aborted

    def request_stop(self):
        self.aborted = True

    async def step(self):
        self.count += 1
        if self.count in (2, 35):
            yield AgentResponse(type="llm_result", data={"chain": MessageChain().message(f"阶段{self.count}")})
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_model_managed_chat_can_continue_without_forced_tool_removal_and_emit_early():
    runner = _MultiStepRunner(autonomous=True)
    output = []
    async for chain in run_agent(runner, max_step=2, buffer_intermediate_messages=True):
        output.append((runner.count, chain.get_plain_text()))
    assert output == [(2, "阶段2"), (35, "阶段35")]
    assert runner.req.func_tool is not None
    assert not runner.run_context.messages


@pytest.mark.asyncio
async def test_regular_agent_still_obeys_its_original_step_bound():
    runner = _MultiStepRunner(autonomous=False)
    _ = [chain async for chain in run_agent(runner, max_step=2)]
    assert runner.count == 3
    assert runner.req.func_tool is None


@pytest.mark.asyncio
async def test_model_managed_agent_still_stops_on_user_cancellation():
    runner = _MultiStepRunner(autonomous=True)
    runner.event.is_stopped = lambda: runner.count >= 2
    output = [chain async for chain in run_agent(runner)]
    assert output == [], "用户停止后不得再发送刚产生的中间正文"
    assert runner.count == 2 and runner.aborted


@pytest.mark.asyncio
async def test_model_managed_agent_cancel_closes_stop_watcher(monkeypatch):
    import astrbot.core.astr_agent_run_util as util

    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def watcher(*_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    class WaitingRunner(_MultiStepRunner):
        async def step(self):
            await entered.wait()
            await asyncio.Event().wait()
            yield

    monkeypatch.setattr(util, "_watch_agent_stop_signal", watcher)
    runner = WaitingRunner(autonomous=True)
    stream = run_agent(runner)
    advancing = asyncio.create_task(anext(stream))
    await entered.wait()
    advancing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await advancing
    assert runner.aborted and cleaned.is_set()


@pytest.mark.asyncio
async def test_run_agent_forwards_streaming_provider_error():
    error_text = (
        "LLM 响应错误: Not found the model k2.7-code-highspeed or Permission denied"
    )
    runner = _StreamingErrorRunner(error_text)

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == error_text


@pytest.mark.asyncio
async def test_run_agent_replaces_malformed_streaming_provider_error():
    runner = _MalformedStreamingErrorRunner("unused")

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == "Error occurred during AI execution."


@pytest.mark.asyncio
async def test_simulated_stream_tts_leaves_audio_for_deferred_cleanup(tmp_path):
    audio_path = tmp_path / "speech.wav"
    audio_path.write_bytes(b"audio")

    class _TTSProvider:
        async def get_audio(self, text: str) -> str:
            assert text == "hello"
            return str(audio_path)

    text_queue: asyncio.Queue[str | None] = asyncio.Queue()
    audio_queue: asyncio.Queue[bytes | tuple[str, bytes] | None] = asyncio.Queue()
    await text_queue.put("hello")
    await text_queue.put(None)

    await _simulated_stream_tts(_TTSProvider(), text_queue, audio_queue)

    assert await audio_queue.get() == ("hello", b"audio")
    assert await audio_queue.get() is None
    assert audio_path.exists()
