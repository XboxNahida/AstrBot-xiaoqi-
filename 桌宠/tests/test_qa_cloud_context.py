from types import SimpleNamespace

import pytest

from cloud_plugin.astrbot_plugin_desktop_link.provider_api import ProviderAPI


def context():
    calls = []
    class Provider:
        provider_config = {'type': 'openai', 'api_base': 'http://127.0.0.1/v1', 'context_length': 8192}  # noqa: RUF012
        def get_model(self):
            return 'vision-a'
        def meta(self):
            return SimpleNamespace(id='p')
        async def text_chat(self, contexts=None, system_prompt=None, **_ignored_kwargs):
            # 模拟真实AstrBot丢弃max_tokens kwargs的适配器。
            return await self._query({'messages': contexts, 'system': system_prompt, 'model': self.get_model()}, None)
        async def _query(self, payload, _tools, **_kwargs):
            self.queried.append(payload)
            return await ctx.llm_generate(chat_provider_id='p', contexts=payload['messages'],
                system_prompt=payload['system'], max_tokens=payload.get('max_tokens'))
    provider = Provider()
    provider.queried = []
    async def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(completion_text='答', usage={'input_tokens': 123, 'output_tokens': 2})
    async def lookup(_id):
        raise AssertionError('不能重读并覆盖会话人设快照')
    ctx = SimpleNamespace(get_using_provider=lambda: provider, get_all_providers=lambda: [provider], llm_generate=generate,
                          persona_manager=SimpleNamespace(get_persona=lookup))
    return ctx, calls, provider


@pytest.mark.asyncio
async def test_cloud_metadata_and_fixed_persona_budgets_before_generate():
    ctx, calls, provider = context()
    api = ProviderAPI(ctx, {})
    info = await api.rpc('pc', 'qa_info', {})
    assert info['profile']['model'] == 'vision-a'
    result = await api.rpc('pc', 'qa', {'messages': [{'role': 'user', 'content': '旧' * 10000},
         {'role': 'assistant', 'content': '旧答'}, {'role': 'user', 'content': '当前问题'}],
         'system': '冻结人格', 'persona': 'cloud:one', 'persona_snapshot': True,
         'expected_model': info['signature'], 'context_options': {'output_tokens': 2048}})
    assert result['text'] == '答' and result['telemetry']['dropped_rounds'] == 1
    assert calls[0]['system_prompt'] == '冻结人格' and len(calls[0]['contexts']) == 1
    assert calls[0]['max_tokens'] == 2048
    assert provider.queried and provider.queried[0]['max_tokens'] == 2048


@pytest.mark.asyncio
async def test_model_changed_or_oversized_latest_never_calls_llm():
    ctx, calls, provider = context()
    api = ProviderAPI(ctx, {})
    info = await api.rpc('pc', 'qa_info', {})
    provider.get_model = lambda: 'vision-b'
    with pytest.raises(ValueError, match='新对话'):
        await api.rpc('pc', 'qa', {'messages': [{'role': 'user', 'content': '问题'}], 'system': '人设', 'expected_model': info['signature']})
    with pytest.raises(ValueError, match='当前问题'):
        await api.rpc('pc', 'qa', {'messages': [{'role': 'user', 'content': '字' * 50000}], 'system': '人设'})
    assert not calls


@pytest.mark.asyncio
async def test_astrbot_cache_usage_distinguishes_unreported_and_explicit_zero():
    ctx, _, _ = context()
    response = SimpleNamespace(completion_text='答', usage=SimpleNamespace(input_other=100, input_cached=25), raw_completion=None)
    async def generate(**_kwargs):
        return response
    ctx.llm_generate = generate
    api = ProviderAPI(ctx, {})
    payload = {'messages': [{'role': 'user', 'content': '题目'}], 'system': '人设'}
    stats = (await api.rpc('pc', 'qa', payload))['telemetry']
    assert stats['cached_tokens'] == 25 and stats['prompt_tokens'] == 125
    response.usage.input_cached = 0
    assert (await api.rpc('pc', 'qa', payload))['telemetry']['cached_tokens'] is None
    response.raw_completion = {'usage': {'prompt_tokens_details': {'cached_tokens': 0}},
                               'choices': [{'finish_reason': 'length'}]}
    stats = (await api.rpc('pc', 'qa', payload))['telemetry']
    assert stats['cached_tokens'] == 0 and stats['truncated']
