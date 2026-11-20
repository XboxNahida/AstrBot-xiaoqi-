import json

import pytest


def test_planner_sends_only_current_image_and_bounded_history():
    from desktop_control.planner import LocalPlanner
    calls = []
    class Model:
        def chat(self, text, **kwargs):
            calls.append((json.loads(text), kwargs))
            return '{"name":"click","x":-150,"y":60}'
    planner = LocalPlanner({}, model=Model())
    observation = {'image_b64': 'current', 'mime': 'image/jpeg', 'left': -200, 'top': 0,
                   'width': 100, 'height': 100, 'controls': [{'name': '按钮'}]}
    result = planner.next('点击按钮', observation, [{'result': str(n)} for n in range(15)])
    assert result == {'name': 'click', 'x': -150, 'y': 60}
    sent, options = calls[0]
    assert len(sent['recent_steps']) == 4
    assert 'image_b64' not in sent['observation']
    assert options['image_b64'] == 'current'
    assert '物理像素' in options['system_prompt']


@pytest.mark.parametrize('text', ['{"name":"shell","code":"calc"}',
    '{"name":"click","x":NaN,"y":3}', '{"name":"finish"}',
    '{"name":"click","name":"finish","text":"结束"}', 'print(123)'])
def test_planner_rejects_invalid_or_ambiguous_actions(text):
    from desktop_control.planner import parse_action
    with pytest.raises(ValueError):
        parse_action(text)


def test_planner_accepts_one_fenced_action_and_finish_requires_evidence():
    from desktop_control.planner import parse_action
    assert parse_action('```json\n{"name":"ask","text":"选哪个窗口？"}\n```')['name'] == 'ask'
    with pytest.raises(ValueError):
        parse_action('{"name":"finish","text":"完成"}')
    assert parse_action('{"name":"finish","text":"完成","evidence":"最新截图出现已保存"}')['name'] == 'finish'


def test_streaming_planner_shows_public_message_before_action_is_complete():
    from desktop_control.planner import LocalPlanner
    shown = []
    class Model:
        def chat(self, _text, **kwargs):
            assert 'message' in kwargs['system_prompt']
            first = '{"message":"正在检查'
            kwargs['on_chunk'](first)
            assert shown == ['正在检查']
            last = '窗口","name":"click","x":3,"y":4}'
            kwargs['on_chunk'](last)
            return first + last
    planner = LocalPlanner({}, model=Model())
    result = planner.next_stream('点击', {'image_b64': 'shot'}, [], shown.append, None)
    assert result['name'] == 'click'
    assert result['message'] == shown[-1] == '正在检查窗口'


def test_partial_message_handles_escaped_text_and_never_leaks_json():
    from desktop_control.planner import public_message
    assert public_message('{"message":"查\\u770b\\n按\\"钮') == '查看\n按"钮'
    assert public_message('{"message":"查\\u77') == '查'
    assert public_message('{"name":"click","x":12}') == ''
    assert public_message('{"message":"完成","name":"click"}') == '完成'


def test_public_description_is_removed_before_tools_and_invalid_message_rejected():
    from desktop_control.planner import parse_action
    with pytest.raises(ValueError):
        parse_action('{"name":"click","x":1,"y":2,"message":{"secret":1}}')
