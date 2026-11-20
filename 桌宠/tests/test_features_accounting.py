"""余额协议只用合成响应，不发送真实请求。"""
from datetime import UTC, datetime

import pytest

from pet_features.accounting import BalanceClient


class Session:
    def __init__(self, data=None, status=200):
        self.data, self.status_code, self.calls = data, status, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self

    def json(self):
        return self.data


def test_deepseek_normalizes_selected_official_profile():
    session = Session({'is_available': True, 'balance_infos': [
        {'currency': 'CNY', 'total_balance': '8.5', 'granted_balance': '2', 'topped_up_balance': '6.5'}]})
    data = BalanceClient('deepseek', {'api_key': 'synthetic', 'base_url': 'https://api.deepseek.com/v1'}, session).fetch()
    assert data['available'] is True and data['total'] == 8.5
    assert data['paid'] == 6.5 and data['granted'] == 2
    assert session.calls[0][0] == 'https://api.deepseek.com/user/balance'
    assert session.calls[0][1]['allow_redirects'] is False


def test_openrouter_and_explicit_custom_balance_path():
    session = Session({'data': {'total_credits': 20, 'total_usage': 3}})
    data = BalanceClient('openrouter', {'api_key': 'synthetic', 'base_url': 'https://openrouter.ai/api/v1'}, session).fetch()
    assert data['total'] == 17 and data['currency'] == 'USD'
    assert session.calls[0][0] == 'https://openrouter.ai/api/v1/credits'
    custom = Session({'data': {'total_credits': 1, 'total_usage': 0}})
    assert BalanceClient('openrouter', {'api_key': 'x', 'base_url': 'https://custom.example/api/v1/credits'}, custom).fetch()['total'] == 1


def test_unverified_hosts_and_http_fail_closed_without_key_leak():
    for base in ['https://api.deepseek.com.evil/v1', 'http://api.deepseek.com', 'https://custom.example/v1', 'https://u:p@api.deepseek.com']:
        session = Session()
        data = BalanceClient('deepseek', {'api_key': 'SECRET', 'base_url': base}, session).fetch()
        assert data['error'] and 'SECRET' not in str(data) and not session.calls
    session = Session(status=401)
    assert BalanceClient('deepseek', {'api_key': 'SECRET'}, session).fetch()['error'] == 'HTTP 401'


def test_malformed_and_exception_never_become_zero_or_leak():
    session = Session({'balance_infos': [{'total_balance': 'nan', 'currency': 'CNY'}]})
    assert BalanceClient('deepseek', {'api_key': 'SECRET'}, session).fetch()['total'] is None
    class Broken(Session):
        def get(self, *args, **kwargs):
            raise ValueError('SECRET')
    assert 'SECRET' not in str(BalanceClient('deepseek', {'api_key': 'SECRET'}, Broken()).fetch())


def test_ledger_persists_separates_currency_and_never_calls_drop_spending(tmp_path):
    from pet_features.accounting import AccountBook
    book = AccountBook(tmp_path / 'book.sqlite')
    book.snapshot('deepseek', {'total': 10, 'currency': 'CNY', 'paid': 8, 'granted': 2})
    book.snapshot('deepseek', {'total': 15, 'currency': 'CNY', 'paid': 13, 'granted': 2})
    book.snapshot('deepseek', {'total': 13, 'currency': 'CNY', 'paid': 13, 'granted': 0})
    book.snapshot('deepseek', {'total': 12, 'currency': 'CNY', 'paid': 12, 'granted': 0})
    book.snapshot('deepseek', {'total': 9, 'currency': 'USD'})
    rows = AccountBook(tmp_path / 'book.sqlite').ledger()
    assert [r['kind'] for r in rows] == ['baseline', 'unknown_decrease', 'grant_decrease', 'recharge', 'baseline']
    assert all(r.get('pet_cost', 0) == 0 for r in rows)
    book.adjust('manual', 23, 'CNY')
    assert book.ledger(1)[0]['kind'] == 'manual'
    assert book.ledger(1)[0]['balance'] == 23


def test_errors_and_invalid_amounts_do_not_pollute_ledger(tmp_path):
    from pet_features.accounting import AccountBook
    book = AccountBook(tmp_path / 'book.sqlite')
    assert book.snapshot('x', {'error': 'HTTP 401', 'total': None}) is None
    assert book.ledger() == []
    with pytest.raises(ValueError):
        book.adjust('x', float('nan'), 'CNY')


def test_estimate_cached_tokens_are_subset_and_provider_is_exact(tmp_path):
    from pet_features.accounting import AccountBook
    book = AccountBook(tmp_path / 'book.sqlite')
    usage = {'input_tokens': 1_000_000, 'cached_input_tokens': 200_000, 'output_tokens': 100_000}
    price = {'provider': 'deepseek', 'model': 'model', 'currency': 'CNY', 'input': 2, 'cached_input': 1, 'output': 4}
    estimate = book.estimate(usage, [price], 'model', 'deepseek')
    assert estimate == {'cost': 2.2, 'currency': 'CNY', 'unknown': False}
    assert book.estimate(usage, [price], 'model', 'codex')['unknown']
    assert book.estimate(usage, [], 'model')['cost'] is None
    assert book.estimate({'input_tokens': 1, 'cached_input_tokens': 2}, [price], 'model', 'deepseek')['unknown']


def test_timezone_overnight_and_unknown_price(tmp_path):
    from pet_features.accounting import AccountBook
    book = AccountBook(tmp_path / 'book.sqlite')
    price = {'provider': 'p', 'model': 'm', 'currency': 'USD', 'input': 1, 'cached_input': 0, 'output': 2,
                 'timezone': 'Asia/Shanghai', 'start': '22:00', 'end': '06:00', 'multiplier': 0.5}
    usage = {'input_tokens': 1_000_000, 'output_tokens': 0}
    assert book.estimate(usage, [price], 'm', 'p', datetime(2026, 1, 1, 15, tzinfo=UTC))['cost'] == 0.5
    assert book.estimate(usage, [price], 'm', 'p', datetime(2026, 1, 1, 5, tzinfo=UTC))['unknown']
    price['timezone'] = 'Invalid/Zone'
    assert book.estimate(usage, [price], 'm', 'p')['unknown']


def test_expiry_requires_explicit_evidence_and_price_conflicts_unknown(tmp_path):
    from pet_features.accounting import AccountBook
    book = AccountBook(tmp_path / 'book.sqlite')
    book.snapshot('p', {'total': 10, 'paid': 8, 'granted': 2, 'currency': 'CNY', 'expires_at': '2026-01-02T00:00:00Z'},
                  now='2026-01-01T00:00:00Z')
    book.snapshot('p', {'total': 8, 'paid': 8, 'granted': 0, 'currency': 'CNY'}, now='2026-01-03T00:00:00Z')
    assert book.ledger(1)[0]['kind'] == 'grant_expiry'
    price = {'provider': 'p', 'model': 'm', 'currency': 'USD', 'input': 1, 'cached_input': 0, 'output': 2}
    assert book.estimate({'input_tokens': 1}, [price, dict(price, currency='CNY')], 'm', 'p')['unknown']


def test_official_wrong_currency_and_redirect_are_not_followed():
    session = Session({'balance_infos': [{'currency': 'USD', 'total_balance': 3}]})
    assert BalanceClient('deepseek', {'api_key': 'synthetic'}, session).fetch()['error']
    session = Session(status=302)
    assert BalanceClient('openrouter', {'api_key': 'synthetic'}, session).fetch()['error'] == 'HTTP 302'
