import pytest

from qa_session import QASession
from tests.test_qa_session import controller


def test_same_profile_id_changed_model_requires_new_conversation(tmp_path):
    original, _ = controller()
    original.bridge.config['profiles'] = [{'id': 'one', 'name': '模型', 'provider': 'openai', 'base_url': 'http://127.0.0.1:1234/v1', 'model': 'vision-a'}]
    state = QASession(original, tmp_path)
    state.change('one', state.personas()[0][0])
    state.append(state.current, 'user', '问题')
    first = state.prepare()
    original.bridge.config['profiles'][0]['key'] = 'rotated-secret'
    assert state.prepare()['system'] == first['system']
    original.bridge.config['profiles'][0]['model'] = 'vision-b'
    with pytest.raises(ValueError, match='新对话'):
        state.prepare()
    old = state.current
    state.new(state.store.settings(old))
    state.append(state.current, 'user', '新问题')
    assert state.prepare()['profile']['model'] == 'vision-b'
    state.store.close()
