"""模式切换及完成回调不删除历史，不截断跨软预算的完整回复。"""
from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication, QComboBox, QLabel
from test_qa_lifecycle import Original

from qa_budget import MODES
from qa_runtime import ScreenshotQA

_APP = QApplication.instance() or QApplication(['qa_mode_runtime'])


def mode_window(qa):
    mode = QComboBox()
    for key, label in MODES.items():
        mode.addItem(label, key)
    qa.window = SimpleNamespace(mode=mode, status=QLabel(), close=lambda: None)
    qa.refresh = lambda: mode.setCurrentIndex(mode.findData(qa.state.store.settings(qa.state.current).get('context_mode', 'normal')))
    return mode


def test_switch_existing_mode_does_not_create_session_and_busy_switch_is_rejected(tmp_path):
    qa = ScreenshotQA(Original(), tmp_path)
    identity = qa.state.current
    qa.state.append(identity, 'user', '问题')
    control = mode_window(qa)
    control.setCurrentIndex(control.findData('retain'))
    qa.set_mode()
    assert qa.state.current == identity
    assert qa.state.store.settings(identity)['context_mode'] == 'retain'
    qa.busy = True
    control.setCurrentIndex(control.findData('normal'))
    qa.set_mode()
    assert control.currentData() == 'retain'
    assert qa.state.store.settings(identity)['context_mode'] == 'retain'
    qa.close()


def test_success_commits_cursor_and_full_reply_failure_does_not(tmp_path):
    qa = ScreenshotQA(Original(), tmp_path)
    identity = qa.state.current
    qa.state.append(identity, 'user', '问题')
    reply = '完整的代码与解释\n' * 1500
    qa._complete((identity, reply, '', {'context_mode': 'normal', 'budget_start': 5}))
    assert qa.state.store.messages(identity)[-1]['text'] == reply
    assert qa.state.store.settings(identity)['budget_start'] == 5
    qa._complete((identity, '', '连接中断', {'context_mode': 'normal', 'budget_start': 8}))
    assert qa.state.store.settings(identity)['budget_start'] == 5
    qa.state.set_mode('retain')
    qa._complete((identity, '保留模式回答', '', {'context_mode': 'retain', 'budget_start': 3}))
    assert qa.state.store.settings(identity).get('budget_start', 0) == 0
    assert qa.state.store.messages(identity)[1]['text'] == reply
    qa.close()


def test_changing_visible_conversation_does_not_commit_cursor_to_wrong_one(tmp_path):
    qa = ScreenshotQA(Original(), tmp_path)
    first = qa.state.current
    qa.state.append(first, 'user', '旧问题')
    qa.state.new()
    second = qa.state.current
    qa._complete((first, '完整回复', '', {'context_mode': 'normal', 'budget_start': 2}))
    assert qa.state.store.settings(first)['budget_start'] == 2
    assert 'budget_start' not in qa.state.store.settings(second)
    qa.state.delete(first)
    qa._complete((first, '迟到结果', '', {'context_mode': 'normal', 'budget_start': 3}))
    assert not qa.state.store.messages(first)
    qa.close()


def test_completion_unlocks_visible_other_conversation(tmp_path):
    qa = ScreenshotQA(Original(), tmp_path)
    first = qa.state.current
    qa.state.append(first, 'user', '正在回答的问题')
    mode = mode_window(qa)
    refresh = qa.refresh
    def refresh_busy():
        refresh()
        mode.setEnabled(not qa.busy)
    qa.refresh = refresh_busy
    qa.busy = True
    qa.new()
    assert not mode.isEnabled()
    second = qa.state.current
    qa._complete((first, '完整回复', '', {'context_mode': 'normal', 'budget_start': 0}))
    assert qa.state.current == second and not qa.busy
    assert mode.isEnabled()
    qa.close()
