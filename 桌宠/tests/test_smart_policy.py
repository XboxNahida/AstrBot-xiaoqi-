"""本地状态机可重放时间轨迹，不触发网络或截图。"""

from smart_config import SmartSettingsStore


def test_stability_dedup_and_changed_scene(tmp_path):
    from smart_policy import SmartPolicy
    cfg = SmartSettingsStore.defaults()
    cfg.update(enabled=True, cooldown_seconds=10)
    policy = SmartPolicy(cfg, tmp_path / "state.json")
    info = {"process": "editor.exe", "title": "项目", "scene": "work"}
    assert not policy.observe(info, 1000)[0]
    assert not policy.observe(info, 1044)[0]
    assert policy.observe(info, 1045)[0]
    policy.reserve(info, 1045)
    assert not policy.observe(info, 1100)[0]
    assert not policy.observe(info, 2300)[0]  # 原地不动不定时重问。
    other = {**info, "title": "另一个项目"}
    assert not policy.observe(other, 2301)[0]
    assert policy.observe(other, 2346)[0]
    assert not policy.observe(info, 2350)[0]
    assert policy.observe(info, 2395)[0]


def test_budget_persists_failed_calls_and_usage(tmp_path):
    from smart_policy import SmartPolicy
    cfg = SmartSettingsStore.defaults()
    cfg.update(enabled=True, hourly_limit=1)
    path = tmp_path / "state.json"
    policy = SmartPolicy(cfg, path)
    info = {"process": "a", "title": "b", "scene": "general"}
    policy.reserve(info, 1000)
    policy.complete({"total_tokens": 320, "usage_known": True}, False, 1001)
    fresh = SmartPolicy(cfg, path)
    fresh.observe({**info, "title": "c"}, 2000)
    assert not fresh.observe({**info, "title": "c"}, 2045)[0]
    assert fresh.state["tokens"] == 320
    assert fresh.state["calls"][0] == 1000


def test_dnd_across_midnight_and_title_noise():
    from smart_policy import dnd_active, scene_key
    cfg = SmartSettingsStore.defaults()
    cfg.update(dnd_schedule_enabled=True)
    assert dnd_active(cfg, "00:30")
    assert not dnd_active(cfg, "12:00")
    assert scene_key({"process": "a", "title": "项目 (2) 12:30"}, True) == scene_key({"process": "a", "title": "项目 (3) 12:31"}, True)


def test_config_roundtrip_does_not_touch_original(tmp_path):
    store = SmartSettingsStore(tmp_path / "smart.json")
    cfg = store.load()
    cfg["enabled"] = True
    cfg["voice_sources"]["manual"]["probability"] = 75
    store.save(cfg)
    assert store.load() == cfg


def test_manual_and_user_are_metered_without_consuming_scene_or_cooldown(tmp_path):
    import pytest

    from smart_policy import SmartPolicy
    cfg = SmartSettingsStore.defaults()
    cfg.update(hourly_limit=2)
    policy = SmartPolicy(cfg, tmp_path / "state.json")
    policy.reserve({}, 1000, kind="manual", check_budget=True)
    policy.reserve({}, 1001, kind="user", check_budget=True)
    assert policy.last_call == 0 and not policy.consumed and not policy.state["scenes"]
    with pytest.raises(ValueError, match="上限"):
        policy.reserve({}, 1002, kind="summary", check_budget=True)
    assert len(policy.state["calls"]) == 2
    assert policy.state["requests_by_kind"] == {"manual": 1, "user": 1}


def test_usage_breakdown_and_only_numeric_request_metadata_are_persisted(tmp_path):
    from smart_policy import SmartPolicy
    cfg = SmartSettingsStore.defaults()
    policy = SmartPolicy(cfg, tmp_path / "state.json")
    policy.complete({"usage_known": True, "input_tokens": 3000, "output_tokens": 20, "total_tokens": 3020,
                     "request_info": {"kind": "manual", "system_chars": 888, "context_chars": 80,
                                      "has_image": True, "secret": "不应保存"}}, True, 1000)
    assert policy.state["input_tokens"] == 3000 and policy.state["output_tokens"] == 20
    assert policy.state["last_request"]["system_chars"] == 888
    assert "不应保存" not in policy.path.read_text(encoding="utf-8")


def test_parallel_usage_and_asr_saves_remain_consistent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from smart_policy import SmartPolicy
    cfg = SmartSettingsStore.defaults()
    policy = SmartPolicy(cfg, tmp_path / "state.json")
    def write(index):
        policy.complete({"usage_known": True, "input_tokens": 2, "output_tokens": 1, "total_tokens": 3}, True, 1000)
        policy.record_asr(0.1, 1000)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(write, range(20)))
    fresh = SmartPolicy(cfg, policy.path)
    assert fresh.state["tokens"] == 60 and fresh.state["asr_calls"] == 20
