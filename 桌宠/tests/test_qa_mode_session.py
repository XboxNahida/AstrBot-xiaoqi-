"""既有会话原地切模式，历史不删，新会话不继承旧游标。"""
import pytest
from test_qa_session import controller

from qa_session import QASession


def test_existing_conversation_switch_keeps_all_data_and_persists_on_reopen(tmp_path):
    original, reports = controller()
    state = QASession(original, tmp_path)
    identity = state.current
    state.append(identity, 'user', '题目', [b'local-picture'])
    state.append(identity, 'assistant', '完整解答')
    old = state.store.settings(identity)
    old.pop('context_mode', None)  # 模拟升级前已有数据。
    old['budget_start'] = 3
    state.store.update_settings(identity, old)
    messages = state.store.messages(identity)
    before_reports = len(reports)
    state.set_mode('retain')
    assert state.current == identity and len(state.store.conversations()) == 1
    assert state.store.messages(identity) == messages
    assert state.store.image(messages[0]['images'][0]) == b'local-picture'
    assert len(reports) == before_reports
    state.store.close()
    reopened = QASession(original, tmp_path)
    assert reopened.store.settings(identity)['context_mode'] == 'retain'
    reopened.set_mode('normal')
    assert reopened.store.settings(identity).get('budget_start', 0) == 0
    assert reopened.store.messages(identity) == messages
    reopened.store.close()


def test_new_and_model_change_inherit_mode_not_old_cursor(tmp_path):
    original, _ = controller()
    state = QASession(original, tmp_path)
    state.set_mode('retain')
    settings = state.store.settings(state.current)
    settings['budget_start'] = 10
    state.new(settings)
    assert state.store.settings(state.current) == {
        'model': 'original', 'persona': settings['persona'], 'context_mode': 'retain'}
    state.append(state.current, 'user', '原题')
    before = state.current
    state.change('cloud', settings['persona'])
    assert state.current != before
    assert state.store.settings(state.current)['context_mode'] == 'retain'
    with pytest.raises(ValueError, match='模式'):
        state.set_mode('invalid')
    state.store.close()
