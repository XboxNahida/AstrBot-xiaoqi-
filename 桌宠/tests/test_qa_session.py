from types import SimpleNamespace

from qa_session import QASession


def controller():
    class Config:
        config = {'api_provider': 'gemini'}  # noqa: RUF012 -- 独立测试只读配置。
        def get(self, key, default=None):
            return self.config.get(key, default)
    reports = []
    link = SimpleNamespace(config={'token': 'test'}, report=lambda facts, **kwargs: reports.append((facts, kwargs)))
    bridge = SimpleNamespace(link=link, config={'profiles': [], 'route': 'original', 'cloud': {}})
    return SimpleNamespace(bridge=bridge, config_mgr=Config()), reports


def test_web_history_reopen_change_delete_and_text_only_sync(tmp_path):
    original, reports = controller()
    state = QASession(original, tmp_path)
    first = state.current
    state.append(first, 'user', '题目', [b'jpeg'])
    state.append(first, 'assistant', '完整解答')
    state.new()
    assert len(state.store.conversations()) == 2
    state.select(first)
    state.store.close()
    state = QASession(original, tmp_path)
    assert state.current == first
    assert state.store.messages(first)[1]['text'] == '完整解答'
    assert all('images' not in r[0] for r in reports)
    assert reports[0][0]['text'] == '题目'
    state.change('cloud', state.personas()[0][0])
    assert state.current != first
    state.delete(first)
    assert not state.store.messages(first)
    assert reports[-1][0]['_record_type'] == 'qa_delete'
    state.store.close()
