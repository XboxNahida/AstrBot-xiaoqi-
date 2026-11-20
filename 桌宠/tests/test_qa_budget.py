"""日常预算为软门槛；模型硬保护与完整历史互相独立。"""
import copy

from cloud_plugin.astrbot_plugin_desktop_link.context_policy import ContextPlanner
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
from qa_budget import prepare_budget


def history(count, size=1000):
    rows = []
    for i in range(count - 1):
        rows += [{'role': 'user', 'content': str(i) + 'x' * size},
                 {'role': 'assistant', 'content': 'a' * size}]
    return rows + [{'role': 'user', 'content': '当前问题'}]


def prepare(rows, settings=None, **limits):
    policy = dict(limits_for({'model': 'gpt-4o'}), **limits)
    return prepare_budget(ContextPlanner(), rows, '完整人格', policy, settings or {})


def test_threshold_compacts_once_then_keeps_stable_prefix_and_full_history():
    rows = history(9)
    original = copy.deepcopy(rows)
    first = prepare(rows)
    assert first['stats']['budget_compacted']
    assert first['stats']['budget_start'] > 0
    assert first['stats']['cost_input_estimate'] <= 8000
    settings = {'budget_start': first['stats']['budget_start']}
    following = rows + [{'role': 'assistant', 'content': '完整的新回复'},
                        {'role': 'user', 'content': '继续'}]
    second = prepare(following, settings)
    assert not second['stats']['budget_compacted']
    assert second['stats']['budget_start'] == settings['budget_start']
    assert second['messages'][:-2] == first['messages']
    assert rows == original


def test_recent_four_rounds_are_protected_above_soft_budget_but_not_hard_limit():
    rows = history(4, 2500)
    result = prepare(rows)
    assert result['messages'] == rows
    assert result['stats']['cost_input_estimate'] > 12000
    assert result['stats']['budget_protected']
    hard = prepare(rows, context_tokens=10000)
    assert hard['stats']['dropped_rounds'] > 0
    assert hard['messages'][-1] == rows[-1]
    assert hard['stats']['input_tokens_estimate'] + hard['stats']['output_tokens'] + hard['stats']['safety_tokens'] <= 10000


def test_retain_restores_old_rounds_and_does_not_apply_soft_budget():
    rows = history(9)
    result = prepare(rows, {'context_mode': 'retain', 'budget_start': 5})
    assert result['messages'] == rows
    assert result['stats']['context_mode'] == 'retain'
    assert result['stats']['dropped_rounds'] == 0


def test_calibration_changes_soft_estimate_only_and_does_not_cut_current_question():
    rows = history(7)
    raw = prepare(rows)
    result = prepare(rows, {'resolved_identity': 'same', 'telemetry': {
        'resolved_identity': 'same', 'input_tokens_estimate': 16000, 'prompt_tokens': 9600}})
    assert raw['stats']['budget_compacted'] and not result['stats']['budget_compacted']
    assert result['stats']['cost_input_estimate'] < result['stats']['input_tokens_estimate']
    huge = [{'role': 'user', 'content': '完整问题' * 1000}]
    assert prepare(huge)['messages'] == huge


def test_response_crossing_budget_is_kept_whole_until_next_request():
    before = history(6, 900)
    assert not prepare(before)['stats']['budget_compacted']
    reply = '完整代码和解答' * 400
    after = before + [{'role': 'assistant', 'content': reply}, {'role': 'user', 'content': '追问'}]
    result = prepare(after)
    assert result['stats']['budget_compacted']
    assert result['messages'][-2]['content'] == reply
    assert after[-2]['content'] == reply
