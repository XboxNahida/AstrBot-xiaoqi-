import json

import pytest

from companion_config import SettingsStore, validate_endpoint


def test_profile_keys_encrypted_and_switch_persists(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    cfg = store.load()
    cfg["profiles"][0]["key"] = "fake-secret-for-local-test"
    cfg["profiles"].append(dict(cfg["profiles"][0], id="second", name="备用模型"))
    cfg["active_profile"] = "second"
    cfg["cloud"]["token"] = "fake-pair-token"
    store.save(cfg)
    raw = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "fake-secret-for-local-test" not in raw
    assert "fake-pair-token" not in raw
    restored = store.load()
    assert restored["active_profile"] == "second"
    assert restored["profiles"][1]["key"] == "fake-secret-for-local-test"
    assert restored["cloud"]["token"] == "fake-pair-token"


def test_invalid_active_profile_does_not_replace_valid_config(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    cfg = store.load()
    store.save(cfg)
    before = store.path.read_bytes()
    cfg["active_profile"] = "missing"
    with pytest.raises(ValueError):
        store.save(cfg)
    assert store.path.read_bytes() == before


def test_corrupted_config_preserved(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        SettingsStore(path).load()
    assert path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:secret@example.com",
        "https://example.com?key=bad",
    ],
)
def test_endpoint_rejects_insecure_or_embedded_secrets(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_local_http_supported_for_local_models():
    assert (
        validate_endpoint("http://127.0.0.1:18211/v1/") == "http://127.0.0.1:18211/v1"
    )


def test_defaults_disable_automatic_capture():
    cfg = SettingsStore.defaults()
    assert cfg["report_foreground"] is False
    assert cfg["route"] == "direct"
    assert cfg["tts_enabled"] is False
    json.dumps(cfg, ensure_ascii=False)


def test_old_config_loads_perception_defaults_without_losing_profiles(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    config = store.defaults()
    for key in ("auto_mode", "critique_screenshot", "critique_cooldown"):
        config.pop(key, None)
    config["profiles"][0]["model"] = "existing-model"
    store.path.write_text(json.dumps(config), encoding="utf-8")
    loaded = store.load()
    assert loaded["auto_mode"] is False
    assert loaded["critique_screenshot"] is False
    assert loaded["critique_cooldown"] == 45
    assert loaded["profiles"][0]["model"] == "existing-model"


@pytest.mark.parametrize("value", [0, 14, 3601, True, "45"])
def test_invalid_critique_cooldown_does_not_replace_config(tmp_path, value):
    store = SettingsStore(tmp_path / "settings.json")
    config = store.defaults()
    store.save(config)
    original = store.path.read_bytes()
    config["critique_cooldown"] = value
    with pytest.raises(ValueError, match="冷却"):
        store.save(config)
    assert store.path.read_bytes() == original
