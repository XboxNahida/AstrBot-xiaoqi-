"""扩展设置与原配置隔离、部分更新和损坏保护。"""
import json

import pytest

from pet_features.config import FeaturesStore


def test_defaults_disabled_and_partial_update_preserves_unknown(tmp_path):
    store = FeaturesStore(tmp_path / "features.json")
    cfg = store.load()
    assert cfg["feeding"]["enabled"] is True
    assert all(not cfg[key]["enabled"] for key in ("weather", "media", "system", "apps", "reminders", "agent", "usage", "balance", "behavior", "sound", "appearance"))
    cfg["future"] = {"untouched": "保留"}
    store.save(cfg)
    store.update_section("weather", {"enabled": True})
    result = store.load()
    assert result["future"] == {"untouched": "保留"}
    assert result["weather"]["cache_seconds"] == 1800
    assert result["weather"]["enabled"] is True
    assert not (tmp_path / "config.json").exists()


def test_bad_config_not_rewritten_and_invalid_values_rejected(tmp_path):
    path = tmp_path / "features.json"
    path.write_text("坏文件", encoding="utf-8")
    with pytest.raises(ValueError, match="保留"):
        FeaturesStore(path).load()
    assert path.read_text(encoding="utf-8") == "坏文件"
    path.write_text(json.dumps({"weather": {"enabled": "yes"}}), encoding="utf-8")
    with pytest.raises(ValueError):
        FeaturesStore(path).load()
    with pytest.raises(ValueError):
        FeaturesStore(tmp_path / "other.json").update_section("feeding", {"voice_probability": 101})


def test_fractional_lyric_offset_roundtrips_and_legacy_int_loads(tmp_path):
    store = FeaturesStore(tmp_path / "features.json")
    cfg = store.update_section("media", {"offset_seconds": 1.25})
    assert cfg["media"]["offset_seconds"] == 1.25
    assert store.load()["media"]["offset_seconds"] == 1.25
    store.update_section("media", {"offset_seconds": 0})
    assert store.load()["media"]["offset_seconds"] == 0


def test_legacy_config_adds_manual_interaction_without_enabling_roaming(tmp_path):
    path = tmp_path / "features.json"
    path.write_text(json.dumps({"future": {"unknown": "保留"},
                                "interaction": {"future_option": 7}}), encoding="utf-8")
    store = FeaturesStore(path)
    cfg = store.load()
    interaction = cfg["interaction"]
    for key in ("enabled", "fling_enabled", "bounce_enabled", "rope_enabled",
                "sling_enabled", "click_enabled", "trail_enabled"):
        assert interaction[key] is True
    assert interaction["gravity_enabled"] is False
    assert interaction["auto_sleep"] is False
    assert interaction["sleep_minutes"] == 10
    assert interaction["gravity"] == 1000
    assert interaction["restitution"] == 0.65
    assert interaction["friction"] == 0.985
    assert interaction["sling_power"] == 8.0
    assert "voice_enabled" not in interaction and "voice_probability" not in interaction
    assert cfg["behavior"]["enabled"] is False
    assert cfg["behavior"]["mode"] == "stationary"
    store.save(cfg)
    assert store.load()["interaction"]["future_option"] == 7
    assert store.load()["future"] == {"unknown": "保留"}


@pytest.mark.parametrize("key,low,high", [
    ("gravity", 0, 3000), ("restitution", 0.0, 1.0),
    ("friction", 0.8, 1.0), ("sling_power", 1.0, 30.0), ("sleep_minutes", 1, 120),
])
def test_interaction_numeric_limits_and_types(tmp_path, key, low, high):
    store = FeaturesStore(tmp_path / "features.json")
    for valid in (low, high):
        assert store.update_section("interaction", {key: valid})["interaction"][key] == valid
    for invalid in (low - 1, high + 1, True, "1", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            store.update_section("interaction", {key: invalid})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_existing_float_fields_reject_nonfinite_numbers(tmp_path, value):
    store = FeaturesStore(tmp_path / "features.json")
    with pytest.raises(ValueError):
        store.update_section("media", {"offset_seconds": value})
    with pytest.raises(ValueError):
        store.update_section("balance", {"manual_balance": value})


def test_interaction_schema_keeps_categories_and_explains_input_contract():
    from pet_features.ui_schema import ACTIONS, CATEGORIES, HINTS, LABELS

    assert len(CATEGORIES) == 11
    assert dict(CATEGORIES)["行为与窗口"] == ["behavior", "interaction"]
    for key in ("fling_enabled", "bounce_enabled", "rope_enabled", "gravity_enabled",
                "sling_enabled", "click_enabled", "trail_enabled", "auto_sleep",
                "sleep_minutes", "gravity", "restitution", "friction", "sling_power"):
        assert key in LABELS
    hint = HINTS["interaction"]
    for phrase in ("不钳制", "中键拖动", "中键单击", "甩抛", "不调用模型", "重力默认关闭"):
        assert phrase in hint
    assert ("睡觉", "tools:sleep") in ACTIONS["interaction"]
    assert ("唤醒", "tools:wake") in ACTIONS["interaction"]
