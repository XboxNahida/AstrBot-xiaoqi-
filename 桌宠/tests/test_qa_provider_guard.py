import copy
from types import SimpleNamespace

import pytest

from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
from cloud_plugin.astrbot_plugin_desktop_link.provider_guard import (
    QAGuardError,
    bounded_generate,
)


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['query', 'stream'])
@pytest.mark.parametrize(('kind', 'model', 'field'), [('openai_chat_completion', 'gpt-5', 'max_completion_tokens'),
    ('google_genai_chat_completion', 'gemini-2.5-flash', 'max_tokens'), ('anthropic_chat_completion', 'claude', 'max_tokens')])
async def test_cap_reaches_real_query_and_shared_provider_unchanged(kind, model, field, route):
    calls = []
    class Provider:
        def __init__(self):
            self.provider_config = {'type': kind, 'custom_extra_body': {'max_tokens': 100000, 'model': 'wrong', 'temperature': .7}}
        def get_model(self):
            return model
        async def text_chat(self, contexts=None, system_prompt=None, **_kwargs):
            if route == 'stream':
                async for chunk in self._query_stream({'messages': contexts, 'system': system_prompt}, None):
                    return chunk
            return await self._query({'messages': contexts, 'system': system_prompt}, None)
        async def _query(self, body, _tools, **kwargs):
            calls.append((body, copy.deepcopy(self.provider_config)))
            return SimpleNamespace(completion_text='答')
        async def _query_stream(self, body, _tools, **kwargs):
            calls.append((body, copy.deepcopy(self.provider_config)))
            yield SimpleNamespace(completion_text='答')
        async def _prepare_query_config(self, *_args, **_kwargs):
            return None
    provider = Provider()
    before = copy.deepcopy(provider.provider_config)
    await bounded_generate(provider, [{'role': 'user', 'content': '问题'}], '冻结人设', limits_for({'model': model}))
    assert calls[0][0][field] == 2048 and calls[0][0]['model'] == model
    assert 'max_tokens' not in calls[0][1]['custom_extra_body']
    assert provider.provider_config == before and '_query' not in provider.__dict__


@pytest.mark.asyncio
async def test_unknown_provider_rejected_before_generation_and_no_automatic_second_query():
    calls = []
    class Provider:
        provider_config = {'type': 'unknown'}  # noqa: RUF012
        def get_model(self):
            return 'vision'
        async def _query(self, *_args):
            calls.append(1)
        async def text_chat(self, **_kwargs):
            await self._query({}, None)
            return await self._query({}, None)
    provider = Provider()
    with pytest.raises(QAGuardError, match='尚无可靠'):
        await bounded_generate(provider, [], '', limits_for({'model': 'vision'}))
    assert not calls
    provider.provider_config = {'type': 'openai'}
    with pytest.raises(QAGuardError, match='未自动重复'):
        await bounded_generate(provider, [], '', limits_for({'model': 'vision'}))
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('drop_persona', [False, True])
async def test_gemini_internal_retry_cannot_repeat_or_remove_frozen_persona(drop_persona):
    sdk_calls = []
    class Provider:
        provider_config = {'type': 'google_genai_chat_completion'}  # noqa: RUF012
        def get_model(self):
            return 'gemini-2.5-flash'
        async def text_chat(self, **_kwargs):
            return await self._query({}, None)
        async def _query(self, payload, tools):
            for index in range(2):
                await self._prepare_query_config(payload, tools, 'auto', None if index and drop_persona else '冻结人设')
                sdk_calls.append(1)  # 模拟真实Gemini RECITATION/APIError内部重试。
        async def _prepare_query_config(self, *_args, **_kwargs):
            return None
    with pytest.raises(QAGuardError, match='重复|人格'):
        await bounded_generate(Provider(), [], '冻结人设', limits_for({'model': 'gemini-2.5-flash'}))
    assert len(sdk_calls) == 1
