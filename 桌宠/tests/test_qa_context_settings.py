from PyQt6.QtWidgets import QApplication

from qa_capabilities import CapabilityStore

_APP = QApplication.instance() or QApplication(['qa_context_settings'])


def test_context_dialog_preserves_model_specific_overrides_and_resets(tmp_path):
    from ui.qa_context_settings import ContextSettings
    store = CapabilityStore(tmp_path / 'limits.json')
    profile = {'provider': 'openai', 'base_url': 'http://localhost:1234/v1', 'model': 'custom'}
    win = ContextSettings(store, profile)
    assert win.context.value() == 0
    win.context.setValue(32000)
    win.output.setValue(4096)
    win.save()
    assert store.resolve(profile)['context_tokens'] == 32000
    assert store.resolve({**profile, 'model': 'other'})['context_tokens'] == 8192
    win = ContextSettings(store, profile)
    assert win.context.value() == 32000
    win.reset()
    assert store.resolve(profile)['context_tokens'] == 8192
    win.close()


def test_official_source_is_visible_and_refresh_does_not_overwrite_unsaved_inputs(tmp_path):
    from ui.qa_context_settings import ContextSettings
    store = CapabilityStore(tmp_path / 'limits.json')
    profile = {'provider': 'openai', 'base_url': 'http://localhost:1234/v1', 'model': 'gemini-4-hydrogen'}
    store.remember(profile, {}, '模型详情正文返回错误；模型列表未提供上下文上限')
    win = ContextSettings(store, profile)
    assert '1000000' in win.info.text() and '官方' in win.info.text()
    assert 'deepmind.google' in win.origin.text()
    assert '未提供上下文' in win.status.text()
    win.output.setValue(4096)
    store.remember(profile, {'context_length': 128000})
    win.ready('已更新')
    assert win.output.value() == 4096
    assert '128000' in win.info.text() and win.origin.isHidden()
    win.close()


def test_dialog_shows_current_mode_output_and_keeps_explicit_override(tmp_path):
    from ui.qa_context_settings import ContextSettings
    store = CapabilityStore(tmp_path / 'limits.json')
    profile = {'provider': 'openai', 'model': 'gpt-4o', 'base_url': 'http://localhost/v1'}
    normal = ContextSettings(store, profile, mode='normal')
    retained = ContextSettings(store, profile, mode='retain')
    assert normal.output.value() == 4096 and retained.output.value() == 8192
    store.override(profile, {'output_tokens': 1500})
    retained.populate()
    assert retained.output.value() == 1500
    assert '保留上下文' in retained.info.text()
    normal.close()
    retained.close()
