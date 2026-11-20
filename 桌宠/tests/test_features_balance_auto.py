"""自动余额接口使用合成HTTP，禁止真实凭据和网络。"""
import pytest

from pet_features.accounting import BalanceClient


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        status, data = self.responses.pop(0)
        class Response:
            status_code = status

            def json(self):
                return data
        return Response()


def query(session, base='https://relay.example/v1'):
    return BalanceClient('auto', {'base_url': base, 'api_key': 'synthetic'}, session).fetch()


def test_usage_endpoint_from_selected_base_and_zero_is_valid():
    session = Session([(200, {'remaining': 0, 'balance': 99, 'unit': 'USD', 'isValid': True})])
    data = query(session)
    assert data['available'] and data['total'] == 0 and data['currency'] == 'USD'
    assert data['scope'] == 'reported'
    assert session.calls[0][0] == 'https://relay.example/v1/usage'
    assert session.calls[0][1]['allow_redirects'] is False


def test_newapi_uses_slash_route_and_explicit_quota_conversion():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 1250000, 'unlimited_quota': False}}),
        (200, {'success': True, 'data': {'quota_per_unit': 500000, 'quota_display_type': 'USD'}})])
    data = query(session)
    assert data['available'] and data['total'] == 2.5 and data['currency'] == 'USD'
    assert data['scope'] == 'token'
    assert session.calls[1][0] == 'https://relay.example/api/usage/token/'
    assert 'Authorization' not in session.calls[2][1].get('headers', {})


def test_unlimited_token_is_not_zero_account_balance():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 0, 'unlimited_quota': True}})])
    data = query(session)
    assert data['available'] and data['unlimited'] and data['total'] is None
    assert data['scope'] == 'token'


def test_newapi_user_quota_is_explicitly_account_scope():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 1, 'unlimited_quota': True,
        'user_quota': {'total_available': 2500000}}}),
        (200, {'success': True, 'data': {'quota_per_unit': 500000, 'quota_display_type': 'USD'}})])
    data = query(session)
    assert data['scope'] == 'account' and data['total'] == 5 and not data['unlimited']


def test_unknown_conversion_keeps_raw_quota():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 2500000}}), (503, {})])
    data = query(session)
    assert data['total'] == 2500000 and data['currency'] == 'QUOTA'


@pytest.mark.parametrize('status', [401, 403, 429, 500, 302])
def test_auth_busy_and_redirect_do_not_probe_or_follow(status):
    session = Session([(status, {})])
    data = query(session)
    assert not data['available'] and len(session.calls) == 1
    assert 'synthetic' not in str(data)


def test_invalid_origin_never_sends_key():
    for url in ['http://relay.example/v1', 'https://u:p@relay.example', 'https://relay.example/?key=x']:
        session = Session([])
        assert query(session, url)['error'] and not session.calls


def test_official_host_uses_official_adapter():
    session = Session([(200, {'is_available': True, 'balance_infos': [
        {'currency': 'CNY', 'total_balance': '8.5'}]})])
    data = query(session, 'https://api.deepseek.com/v1')
    assert data['total'] == 8.5 and session.calls[0][0] == 'https://api.deepseek.com/user/balance'


@pytest.mark.parametrize('base,body,target', [
    ('https://api.deepseek.com/v1/chat/completions', {'balance_infos': [{'currency': 'CNY', 'total_balance': 2}]},
     'https://api.deepseek.com/user/balance'),
    ('https://openrouter.ai/api/v1/chat/completions', {'data': {'total_credits': 3, 'total_usage': 1}},
     'https://openrouter.ai/api/v1/credits'),
])
def test_official_full_request_url(base, body, target):
    session = Session([(200, body)])
    assert query(session, base)['total'] == 2
    assert session.calls[0][0] == target


@pytest.mark.parametrize('failure', [{'success': False}, {'code': False}, {'error': {'message': 'SECRET'}}])
def test_explicit_failure_never_becomes_balance(failure):
    session = Session([(200, {'remaining': 99, 'unit': 'USD', **failure})])
    data = query(session)
    assert not data['available'] and data['total'] is None
    assert len(session.calls) == 1 and 'SECRET' not in str(data)


def test_invalid_cny_rate_keeps_original_positive_quota():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 2500000}}),
        (200, {'success': True, 'data': {'quota_per_unit': 500000, 'quota_display_type': 'CNY',
                                      'usd_exchange_rate': -1}})])
    data = query(session)
    assert data['total'] == 2500000 and data['currency'] == 'QUOTA'


def test_failed_status_payload_cannot_supply_money_conversion():
    session = Session([(404, {}), (200, {'code': True, 'data': {
        'object': 'token_usage', 'total_available': 2500000}}),
        (200, {'success': False, 'data': {'quota_per_unit': 500000, 'quota_display_type': 'USD'}})])
    data = query(session)
    assert data['total'] == 2500000 and data['currency'] == 'QUOTA'


def test_token_error_payload_never_enters_money_ledger():
    session = Session([(404, {}), (200, {'error': {'message': 'SECRET'}, 'data': {
        'object': 'token_usage', 'total_available': 2500000}}),
        (200, {'data': {'quota_per_unit': 500000, 'quota_display_type': 'USD'}})])
    data = query(session)
    assert not data['available'] and data['total'] is None
    assert len(session.calls) == 2 and 'SECRET' not in str(data)
