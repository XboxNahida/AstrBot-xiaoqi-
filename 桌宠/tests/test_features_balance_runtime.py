"""余额跟随模型、配置隔离与中文结果展示。"""
from companion_config import SettingsStore
from pet_features.backend import FeaturesBackend
from pet_features.config import defaults
from tests.test_original_extension import original_config as original_config  # noqa: PLC0414


def setup_profiles(tmp_path):
    store = SettingsStore(tmp_path / 'companion_settings.json')
    cfg = store.load()
    cfg['profiles'] = [dict(cfg['profiles'][0], id=k, name=k, base_url=f'https://{k}.example/v1', key='synthetic')
                       for k in ('first', 'second')]
    cfg['active_profile'] = 'first'
    store.save(cfg)
    return store, cfg


def test_follow_current_profile_and_separate_ledger_by_account(tmp_path, monkeypatch):
    store, models = setup_profiles(tmp_path)
    calls = []
    def fetch(self):
        calls.append(self.profile['base_url'])
        return {'available': True, 'total': 8, 'currency': 'USD', 'error': '', 'scope': 'account'}
    monkeypatch.setattr('pet_features.backend_extended.BalanceClient.fetch', fetch)
    cfg = defaults()
    assert cfg['balance']['provider'] == 'auto'
    backend = FeaturesBackend(tmp_path)
    try:
        first = backend.perform('balance:refresh', cfg)['text']
        assert 'first.example' in first and '账户余额' in first and '8' in first
        models['active_profile'] = 'second'
        store.save(models)
        second = backend.perform('balance:refresh', cfg)['text']
        assert 'second.example' in second and calls == ['https://first.example/v1', 'https://second.example/v1']
        assert [r['kind'] for r in backend._ensure_account().ledger()] == ['baseline', 'baseline']
        cfg['balance']['profile_id'] = 'first'
        assert 'first.example' in backend.perform('balance:refresh', cfg)['text']
    finally:
        backend.close()


def test_unlimited_and_raw_quota_do_not_enter_money_ledger(tmp_path, monkeypatch):
    setup_profiles(tmp_path)
    response = {'available': True, 'total': None, 'currency': None, 'error': '', 'scope': 'token', 'unlimited': True}
    monkeypatch.setattr('pet_features.backend_extended.BalanceClient.fetch', lambda self: dict(response))
    cfg = defaults()
    cfg['balance']['provider'] = 'auto'
    backend = FeaturesBackend(tmp_path)
    try:
        text = backend.perform('balance:refresh', cfg)['text']
        assert '无限额度' in text and '账户余额' in text and '未返回' in text
        response.update(total=500000, currency='QUOTA', unlimited=False)
        assert '原始额度' in backend.perform('balance:refresh', cfg)['text']
        assert backend._ensure_account().ledger() == []
    finally:
        backend.close()


def test_original_route_uses_original_request_address(original_config, tmp_path, monkeypatch):
    store, models = setup_profiles(tmp_path)
    models['route'] = 'original'
    store.save(models)
    original_config.config.update(api_provider='openai_compatible', custom_api_url='https://original.example/v1', api_key='synthetic')
    calls = []
    monkeypatch.setattr('pet_features.backend_extended.BalanceClient.fetch',
                        lambda self: calls.append(self.profile['base_url']) or {'available': False, 'error': '测试失败'})
    backend = FeaturesBackend(tmp_path)
    try:
        cfg = defaults()
        cfg['balance']['provider'] = 'auto'
        assert '测试失败' in backend.perform('balance:refresh', cfg)['text']
        assert calls == ['https://original.example/v1']
        models['route'] = 'cloud'
        store.save(models)
        assert '云端' in backend.perform('balance:refresh', cfg)['text']
        assert len(calls) == 1
    finally:
        backend.close()
