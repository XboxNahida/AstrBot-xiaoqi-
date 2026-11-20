"""会话成本软预算：达到门槛批量收缩，模型硬保护始终独立执行。"""
import math

from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for

MODES = {'normal': '普通 · 省Token', 'retain': '保留上下文'}
TRIGGER, TARGET, MIN_ROUNDS = 12000, 8000, 4


def mode_for(settings):
    mode = settings.get('context_mode', 'normal')
    return mode if mode in MODES else 'normal'


def output_policy(profile, policy, entry, mode):
    overrides = entry.get('overrides', {})
    if overrides.get('output_tokens'):
        return policy
    desired = min(4096 if mode == 'normal' else 8192, max(64, policy['context_tokens'] // 4))
    selected = limits_for(profile, entry.get('metadata'), {**overrides, 'output_tokens': desired})
    return dict(policy, output_tokens=selected['output_tokens'])


def _ratio(settings):
    previous = settings.get('telemetry') or {}
    identity = settings.get('resolved_identity')
    actual, estimate = previous.get('prompt_tokens'), previous.get('input_tokens_estimate')
    if (identity and previous.get('resolved_identity') == identity
            and type(actual) is int and type(estimate) is int and actual > 0 and estimate > 0):
        return min(1., max(.5, actual / estimate) * 1.15)
    return 1.


def prepare_budget(planner, messages, system, policy, settings, *, image_reader=None):
    starts = [i for i, row in enumerate(messages) if row.get('role') == 'user']
    if not starts or starts[0] != 0:
        raise ValueError('历史缺少对应用户问题')
    mode = mode_for(settings)
    cursor = settings.get('budget_start', 0) if mode == 'normal' else 0
    if type(cursor) is not int or not 0 <= cursor < len(starts):
        cursor = 0
    limits = dict(policy)
    if mode == 'retain' and not limits.get('server_message_limit'):
        limits['max_messages'] = max(len(messages), limits['max_messages'])
    result = planner.prepare(messages[starts[cursor]:], system, limits, image_reader=image_reader)
    ratio = _ratio(settings)
    cost = lambda value: math.ceil(value['stats']['input_tokens_estimate'] * ratio)
    compacted = False
    if mode == 'normal' and cost(result) >= TRIGGER:
        # 收缩目标是软限制；尽可能保护当前轮和最近三个完整旧轮次。
        while cost(result) > TARGET and result['stats']['kept_rounds'] > MIN_ROUNDS:
            rows = result['messages']
            next_round = next(i for i, row in enumerate(rows[1:], 1) if row['role'] == 'user')
            result = planner.prepare(rows[next_round:], system, limits)
            compacted = True
    dropped = len(starts) - result['stats']['kept_rounds']
    result['stats'].update(
        context_mode=mode, budget_start=dropped, dropped_rounds=dropped,
        budget_compacted=compacted, cost_input_estimate=cost(result),
        budget_trigger=TRIGGER if mode == 'normal' else None,
        budget_target=TARGET if mode == 'normal' else None,
        budget_protected=mode == 'normal' and cost(result) > TARGET
                         and result['stats']['kept_rounds'] <= MIN_ROUNDS,
    )
    if limits.get('server_message_limit'):
        result['stats']['server_message_limit'] = limits['server_message_limit']
    return result
