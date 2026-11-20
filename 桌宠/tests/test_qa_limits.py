from cloud_plugin.astrbot_plugin_desktop_link.model_limits import (
    limits_for,
    model_identity,
)
from qa_capabilities import CapabilityStore


def test_metadata_override_and_output_headroom():
    profile = {'provider': 'openai', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'unknown', 'key': 'secret'}
    assert limits_for(profile)['context_tokens'] == 8192
    meta = {'context_length': 32768, 'top_provider': {'max_completion_tokens': 8192}}
    auto = limits_for(profile, meta)
    assert auto['context_tokens'] == 32768 and auto['source'] == '服务商元数据'
    override = limits_for(profile, meta, {'context_tokens': 16384, 'output_tokens': 4096})
    assert override['context_tokens'] == 16384 and override['output_tokens'] == 4096
    assert limits_for({'provider': 'gemini', 'model': 'gemini-2.5-flash'},
                      {'inputTokenLimit': 1048576, 'outputTokenLimit': 65536})['input_limit'] == 1048576
    assert model_identity(profile) == model_identity(dict(profile, key='rotated'))
    assert model_identity(profile) != model_identity(dict(profile, model='other'))


def test_limits_cache_is_model_bound_expires_and_never_stores_keys(tmp_path):
    profile = {'provider': 'openai', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'vision', 'key': 'secret'}
    now = [1000]
    store = CapabilityStore(tmp_path / 'limits.json', clock=lambda: now[0])
    store.remember(profile, {'context_length': 64000})
    store.override(profile, {'context_tokens': 32000, 'output_tokens': 3000})
    assert store.resolve(profile)['context_tokens'] == 32000
    assert store.resolve(dict(profile, model='other'))['context_tokens'] == 8192
    store.override(profile, {})
    assert store.resolve(profile)['context_tokens'] == 64000
    now[0] += 86401
    assert store.resolve(profile)['context_tokens'] == 8192
    assert 'secret' not in store.path.read_text(encoding='utf-8')


def test_hydrogen_official_unified_window_and_provider_limits_take_precedence():
    profile = {'provider': 'openai', 'model': 'gemini-4-hydrogen', 'base_url': 'http://localhost:1234/v1'}
    policy = limits_for(profile)
    assert policy['context_tokens'] == 1000000 and policy['input_limit'] == 0
    assert '官方' in policy['source'] and 'deepmind.google' in policy['source_url']
    assert limits_for(profile, overrides={'output_tokens': 100000})['output_tokens'] == 64000
    assert limits_for(profile, {'context_length': 64000})['context_tokens'] == 64000
    assert limits_for(profile, overrides={'context_tokens': 32000})['context_tokens'] == 32000
    # 不把未确认的类似别名按Hydrogen窗口扩大。
    assert limits_for({**profile, 'model': 'gemini-4-hydrogen-custom'})['context_tokens'] == 8192
