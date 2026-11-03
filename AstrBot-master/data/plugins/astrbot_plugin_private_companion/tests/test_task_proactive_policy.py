from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from astrbot_plugin_private_companion.task_proactive_policy import (
    ProactivePolicyStore,
    RuleConfig,
)
from astrbot_plugin_private_companion.task_service import TaskScope


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 3, 15, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.now


@pytest.fixture
def policy(tmp_path):
    clock = Clock()
    store = ProactivePolicyStore(tmp_path / "rules.db", clock=clock, lease_seconds=30)
    return store, TaskScope("bot", "group", "sender"), clock


def reserve(store, scope, fingerprint="news-v1", **kwargs):
    return store.reserve_delivery(scope, "cron-existing", fingerprint, **kwargs)


def test_restart_owner_isolation_and_dynamic_rules(policy):
    store, scope, clock = policy
    store.save(scope, "cron-existing", RuleConfig(max_deliveries=1))
    lease = reserve(store, scope)
    assert lease is not None
    assert store.get(scope, "cron-existing")["sent_count"] == 0
    assert store.ack(lease)
    store = ProactivePolicyStore(store.path, clock=clock)
    assert reserve(store, scope, "news-v2") is None
    store.save(scope, "cron-existing", RuleConfig(max_deliveries=None))
    assert store.get(scope, "cron-existing")["sent_count"] == 1
    assert reserve(store, scope, "news-v1") is None
    for field in ("bot_id", "session_key", "sender_id"):
        other = replace(scope, **{field: "other"})
        assert store.get(other, "cron-existing") is None
        assert reserve(store, other) is None
        assert not store.ack(replace(lease, scope=other))
        store.save(other, "cron-existing", RuleConfig())
        assert reserve(store, other) is not None
    assert reserve(store, scope, "news-v2") is not None


def test_empty_fingerprint_and_no_update_never_send(policy):
    store, scope, _ = policy
    store.save(scope, "cron-existing", RuleConfig())
    for fingerprint in (None, "", "   "):
        assert reserve(store, scope, fingerprint) is None
    assert reserve(store, scope, has_update=False) is None
    lease = reserve(store, scope)
    assert store.ack(lease)
    assert reserve(store, scope) is None


def test_concurrent_reservations_across_connections_have_single_winner(policy):
    store, scope, clock = policy
    store.save(scope, "cron-existing", RuleConfig())

    def claim(_):
        second = ProactivePolicyStore(store.path, clock=clock)
        return reserve(second, scope)

    with ThreadPoolExecutor(max_workers=6) as pool:
        leases = list(pool.map(claim, range(12)))
    assert sum(lease is not None for lease in leases) == 1
    assert reserve(store, scope, "different-news") is None


def test_expired_lease_and_unknown_failure_need_explicit_reconciliation(policy):
    store, scope, clock = policy
    store.save(scope, "cron-existing", RuleConfig())
    lease = reserve(store, scope)
    clock.now += 31
    assert store.get(scope, "cron-existing")["state"] == "unknown_result"
    assert not store.ack(lease)
    assert reserve(store, scope, "news-v2") is None
    assert store.get(scope, "cron-existing")["state"] == "unknown_result"
    assert store.get(scope, "cron-existing")["sent_count"] == 0
    store.save(scope, "cron-existing", RuleConfig(max_deliveries=2))
    assert reserve(store, scope, "news-v2") is None
    assert store.reconcile_delivery(lease, delivered=True)
    assert not store.reconcile_delivery(lease, delivered=True)
    assert store.get(scope, "cron-existing")["sent_count"] == 1
    assert reserve(store, scope) is None
    second = reserve(store, scope, "news-v2")
    assert store.fail(second)
    assert reserve(store, scope, "news-v2") is None
    assert store.reconcile_delivery(second, delivered=False)
    third = reserve(store, scope, "news-v2")
    assert store.fail(third, outcome_unknown=False)
    assert store.get(scope, "cron-existing")["sent_count"] == 1
    assert reserve(store, scope, "news-v2") is not None


def test_shanghai_cross_midnight_window_and_dynamic_interval_stop_expiry(policy):
    store, scope, clock = policy
    config = RuleConfig(windows=(("22:00", "02:00"),), min_interval_seconds=3600)
    store.save(scope, "cron-existing", config)
    assert store.ack(reserve(store, scope))  # 23:00 Shanghai.
    clock.now += 3599
    assert reserve(store, scope, "news-v2") is None
    clock.now += 1
    lease = reserve(store, scope, "news-v2")  # 00:00 Shanghai.
    assert lease is not None
    assert store.ack(lease)
    clock.now += 2 * 3600
    assert reserve(store, scope, "news-v3") is None  # End is exclusive.
    store.save(scope, "cron-existing", replace(config, windows=(), stopped=True))
    assert reserve(store, scope, "news-v3") is None
    store.save(scope, "cron-existing", replace(config, windows=(), enabled=False))
    assert reserve(store, scope, "news-v3") is None
    store.save(scope, "cron-existing", RuleConfig(expires_at=clock.now + 1))
    clock.now += 1
    assert reserve(store, scope, "news-v3") is None
    store.save(scope, "cron-existing", RuleConfig(min_interval_seconds=0))
    assert reserve(store, scope, "news-v3") is not None


def test_utc_windows_and_explicit_repeated_delivery_configuration(policy):
    store, scope, _ = policy
    config = RuleConfig(timezone="UTC", windows=(("15:00", "16:00"),))
    store.save(scope, "cron-existing", config)
    assert store.ack(reserve(store, scope))
    assert reserve(store, scope) is None
    store.save(scope, "cron-existing", replace(config, only_updates=False))
    assert reserve(store, scope, has_update=False) is None
    assert reserve(store, scope) is not None


@pytest.mark.parametrize(
    "changes",
    [
        {"timezone": "invalid/timezone"},
        {"windows": (("25:00", "01:00"),)},
        {"windows": (("12:00", "12:00"),)},
        {"max_deliveries": -1},
        {"max_deliveries": True},
        {"min_interval_seconds": float("nan")},
        {"min_interval_seconds": -1},
        {"expires_at": float("inf")},
        {"enabled": "yes"},
    ],
)
def test_invalid_configuration_fails_before_persisting(policy, changes):
    store, scope, _ = policy
    with pytest.raises((ValueError, TypeError)):
        store.save(scope, "cron-existing", RuleConfig(**changes))
    assert store.get(scope, "cron-existing") is None


def test_missing_scope_and_cron_reference_rejected(policy):
    store, scope, _ = policy
    with pytest.raises(ValueError):
        store.save(replace(scope, sender_id=""), "cron-existing", RuleConfig())
    with pytest.raises(ValueError):
        store.save(scope, "", RuleConfig())


def test_max_zero_and_dynamic_change_while_reserved_preserve_receipt(policy):
    store, scope, _ = policy
    store.save(scope, "cron-existing", RuleConfig(max_deliveries=0))
    assert reserve(store, scope) is None
    store.save(scope, "cron-existing", RuleConfig())
    lease = reserve(store, scope)
    store.save(scope, "cron-existing", RuleConfig(stopped=True))
    assert store.ack(lease)
    assert store.get(scope, "cron-existing")["sent_count"] == 1
    assert reserve(store, scope, "news-v2") is None


def test_failure_outcome_must_be_explicit_boolean(policy):
    store, scope, _ = policy
    store.save(scope, "cron-existing", RuleConfig())
    lease = reserve(store, scope)
    with pytest.raises(ValueError):
        store.fail(lease, outcome_unknown="")
    assert reserve(store, scope, "news-v2") is None
