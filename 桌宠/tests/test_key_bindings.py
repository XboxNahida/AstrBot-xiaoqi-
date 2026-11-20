import pytest

from hotkey_config import DEFAULT_KEYS, ConfiguredHotkeys, HotkeyStore, parse_keys


def test_combinations_roundtrip_disabled_and_conflicts(tmp_path):
    store = HotkeyStore(tmp_path / "keys.json")
    assert store.load() == DEFAULT_KEYS
    cfg = dict(DEFAULT_KEYS, critique="Ctrl+Shift+F8", mute="")
    store.save(cfg)
    assert store.load() == cfg
    assert parse_keys(cfg["critique"]) == "<ctrl>+<shift>+<f8>"
    with pytest.raises(ValueError):
        store.validate(dict(cfg, qa="Ctrl+Shift+F8"))
    with pytest.raises(ValueError):
        store.validate(cfg, ["<ctrl>+<shift>+<f8>"])
    with pytest.raises(ValueError):
        store.validate(dict(DEFAULT_KEYS, qa='Ctrl+Alt+F9'))


def test_failed_rebind_keeps_old_listener():
    class Listener:
        stopped = False

        def __init__(self, _mapping):
            pass

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    manager = ConfiguredHotkeys({"qa": lambda: None}, listener_factory=Listener)
    manager.apply(DEFAULT_KEYS)
    previous = manager.listener
    with pytest.raises(ValueError):
        manager.apply(dict(DEFAULT_KEYS, qa="Ctrl+F9"))
    assert manager.listener is previous and not previous.stopped
    manager.stop()
