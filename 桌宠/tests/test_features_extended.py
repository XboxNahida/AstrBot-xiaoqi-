"""日志、用量、余额分发使用明确数据来源。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from pet_features.backend import FeaturesBackend
from pet_features.config import defaults
from smart_config import SmartSettingsStore
from smart_policy import SmartPolicy


def _completion(**extra):
    return dict(usage_known=True, input_tokens=100, output_tokens=20, total_tokens=120, **extra)


def test_completed_requests_are_not_sampled_or_repeated(tmp_path):
    policy = SmartPolicy(SmartSettingsStore.defaults(), tmp_path / "smart_state.json")
    backend = FeaturesBackend(tmp_path)
    try:
        policy.complete(_completion(), True, 1000)
        policy.complete(_completion(), True, 1001)
        backend._import_pet_usage()
        assert backend._ensure_usage().report()["requests"] == 2
        policy.reserve({}, 1002, kind="manual")
        backend._import_pet_usage()
        assert backend._ensure_usage().report()["requests"] == 2
        backend.close()
        backend = FeaturesBackend(tmp_path)
        backend._import_pet_usage()
        assert backend._ensure_usage().report()["totals"]["input_tokens"] == 200
    finally:
        backend.close()


def test_concurrent_completions_keep_every_numeric_record(tmp_path):
    policy = SmartPolicy(SmartSettingsStore.defaults(), tmp_path / "smart_state.json")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: policy.complete(_completion(), True, 1000 + i), range(20)))
    with sqlite3.connect(tmp_path / "features_model_usage.sqlite") as db:
        rows = db.execute("SELECT request_id,data FROM completions").fetchall()
    assert len({row[0] for row in rows}) == 20
    assert policy.state["tokens"] == 2400
    assert all("prompt" not in json.loads(data) for _, data in rows)


def test_costs_require_identity_and_keep_source_currency(tmp_path):
    policy = SmartPolicy(SmartSettingsStore.defaults(), tmp_path / "smart_state.json")
    policy.complete(_completion(model="m", provider="p"), True, 1000)
    policy.complete(_completion(model="n", provider="p"), True, 1001)
    policy.complete(_completion(), True, 1002)
    backend = FeaturesBackend(tmp_path)
    cfg = defaults()
    cfg["balance"]["prices"] = [{"model": model, "provider": "p", "currency": currency,
                                     "input": 1, "cached_input": 1, "output": 2}
                                  for model, currency in (("m", "CNY"), ("n", "USD"))]
    try:
        backend._ensure_usage().record({"input_tokens": 10}, "agent-1")
        backend._import_pet_usage()
        costs = backend._usage_costs(cfg)
        assert costs["pet"]["by_currency"] == {"CNY": .00014, "USD": .00014}
        assert costs["pet"]["unknown_requests"] == 1
        assert costs["agent"]["unknown_requests"] == 1
    finally:
        backend.close()


def test_optional_completion_ledger_failure_preserves_budget(tmp_path):
    (tmp_path / "features_model_usage.sqlite").mkdir()
    policy = SmartPolicy(SmartSettingsStore.defaults(), tmp_path / "smart_state.json")
    policy.complete(_completion(), True, 1000)
    assert policy.state["tokens"] == 120


def test_usage_and_manual_balance_routes(tmp_path):
    backend = FeaturesBackend(tmp_path)
    cfg = defaults()
    try:
        data = backend.perform("usage:report", cfg)
        assert "Token" in data["text"]
        cfg["balance"].update(provider="manual", manual_balance=123)
        adjusted = backend.perform("balance:adjust", cfg)
        assert "123" in adjusted["text"]
        assert "手动校准" in backend.perform("balance:ledger", cfg)["text"]
    finally:
        backend.close()


def test_usage_result_is_chinese_summary_without_internal_json(tmp_path):
    backend = FeaturesBackend(tmp_path)
    try:
        backend._ensure_usage().record({'input_tokens': 1234, 'cached_input_tokens': 200,
                                       'output_tokens': 60, 'reasoning_output_tokens': 10}, 'known', source='pet')
        response = backend.perform('usage:report', defaults())
        text = response['text']
        for label in ('累计', '今日', '最近一次', '输入', '输出', '缓存命中', '思考', '桌宠', '费用'):
            assert label in text
        assert '1,234' in text
        for raw in ('input_tokens', 'estimated_costs', 'by_source', 'legacy_note', '{', '}'):
            assert raw not in text
        assert response['status']['usage']['totals']['input_tokens'] == 1234
    finally:
        backend.close()


def test_usage_empty_is_explicit_not_a_configuration_dump(tmp_path):
    backend = FeaturesBackend(tmp_path)
    try:
        text = backend.perform('usage:report', defaults())['text']
        assert '暂无' in text and 'Token' in text and 'totals' not in text
    finally:
        backend.close()


def test_ledger_and_adjustment_hide_internal_fields(tmp_path):
    backend = FeaturesBackend(tmp_path)
    try:
        cfg = defaults()
        cfg['balance'].update(provider='manual', manual_balance=123)
        adjusted = backend.perform('balance:adjust', cfg)['text']
        assert '123' in adjusted and '手动' in adjusted and '{' not in adjusted
        ledger = backend.perform('balance:ledger', cfg)['text']
        assert '手动校准' in ledger and '123' in ledger
        assert 'provider' not in ledger and 'currency' not in ledger and '{' not in ledger
    finally:
        backend.close()


def test_chinese_ledger_keeps_same_host_accounts_and_scope_distinct():
    from pet_features.report_text import ledger_text

    row = {'at': '2026-11-17T08:00:00+00:00', 'balance': 10, 'amount': 0,
           'currency': 'USD', 'kind': 'baseline'}
    account = ledger_text([{**row, 'provider': 'same.example/profile-a/0123456789ab/account'}])
    token = ledger_text([{**row, 'provider': 'same.example/profile-b/fedcba987654/token'}])
    assert account != token
    assert '账户余额' in account and '令牌额度' in token
    assert 'profile-a' in account and 'profile-b' in token
