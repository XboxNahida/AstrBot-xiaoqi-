from types import SimpleNamespace

import pytest

from tests.test_desktop_control_runtime import make_control
from tests.test_smart_models import setup_client


@pytest.mark.parametrize('kind', ['manual', 'passive', 'icebreaker'])
def test_critique_always_follows_main_even_with_legacy_independent_choice(monkeypatch, kind):
    client, calls, _, _ = setup_client(monkeypatch, separate_model=True, profile_id='two')
    result = client.generate({}, 'synthetic-image', kind)
    assert calls[0][0].endswith('/chat/completions')
    assert calls[0][1]['json']['model'] == 'gpt-4o'
    assert result['text']
    client.smart_cfg['profile_id'] = 'deleted-old-independent-choice'
    client.validate_configuration()


def test_operating_window_model_choice_is_global_persistent_and_does_not_change_active_task(tmp_path):
    control, _, controller = make_control(tmp_path, SimpleNamespace())
    profiles = [{'id': 'one', 'name': '模型一', 'model': 'first', 'base_url': 'http://127.0.0.1:1234/v1'},
                {'id': 'two', 'name': '模型二', 'model': 'second', 'base_url': 'http://127.0.0.1:1234/v1'}]
    controller.bridge.config['profiles'] = profiles
    control.panel.profiles_provider = lambda: profiles
    control.panel.refresh_profiles()
    try:
        control.open()
        assert control.window.model_combo.findData('two') >= 0
        index = control.window.model_combo.findData('one')
        control.window.model_combo.setCurrentIndex(index)
        control.window.model_combo.activated.emit(index)
        assert control.panel.config['profile_id'] == control.panel.store.load()['profile_id'] == 'one'
        assert control.panel.profile.currentData() == 'one'
        control._task_profile_id = 'one'
        assert control._planner().model.profile['model'] == 'first'
        control.state = 'planning'
        control.select_model('two')
        assert control.panel.config['profile_id'] == control.panel.store.load()['profile_id'] == 'two'
        assert control.state == 'planning'
        assert control._planner().model.profile['model'] == 'first'
        from ui.desktop_control import ControlPanel
        reopened = ControlPanel(control.panel.store, lambda: profiles)
        assert reopened.config['profile_id'] == 'two' and reopened.profile.currentData() == 'two'
        reopened.hide()
        reopened.deleteLater()
    finally:
        control.close()
        controller.pet.close()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()


@pytest.mark.parametrize('kind', ['user', 'summary'])
def test_independent_chat_and_summary_validate_their_own_model(monkeypatch, kind):
    from smart_runtime import SmartCompanion
    client, calls, _, _ = setup_client(monkeypatch, separate_model=True, profile_id='one')
    client.bridge_config['route'] = 'cloud'
    original = client.original_controller
    original.bridge = SimpleNamespace(config=client.bridge_config)
    smart = SimpleNamespace(generation=1, closed=False, cfg=client.smart_cfg, original=original,
        policy=SimpleNamespace(reserve=lambda *a, **kw: None, complete=lambda *a: None),
        usage_changed=SimpleNamespace(emit=lambda: None))
    result = SmartCompanion.generate_metered(smart, {}, 'synthetic-image' if kind == 'user' else None,
                                             kind, user_text='原问题')
    assert result['text'] and calls


def test_old_planner_cannot_overwrite_new_task_model_snapshot(tmp_path, monkeypatch):
    import threading

    from smart_models import SmartModelClient
    control, _, controller = make_control(tmp_path, SimpleNamespace())
    old = {'model': 'old-A', 'base_url': 'http://127.0.0.1:1234/v1'}
    new = {'id': 'two', 'model': 'new-B', 'base_url': 'http://127.0.0.1:1234/v1'}
    controller.bridge.config['profiles'] = [new]
    entered, release = threading.Event(), threading.Event()
    def resolve(_client):
        entered.set()
        assert release.wait(3)
        return old
    monkeypatch.setattr(SmartModelClient, '_original_profile', resolve)
    control._task_profile_id = 'original'
    worker = threading.Thread(target=control._planner)
    worker.start()
    try:
        assert entered.wait(2)
        control.stop()
        control._task_profile, control._task_profile_id = None, 'two'
        assert control._planner().model.profile['model'] == 'new-B'
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        assert control._planner().model.profile['model'] == 'new-B'
    finally:
        release.set()
        worker.join(2)
        control.close()
        controller.pet.close()
