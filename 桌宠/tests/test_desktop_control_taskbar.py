"""任务栏控件使用物理矩形，模型选择编号由本地定位。"""
import sys
from types import SimpleNamespace

import pytest


def test_controls_use_physical_dpi_and_include_deep_taskbar_buttons(monkeypatch):
    from desktop_control.windows import WindowsDesktop

    events = []
    class Node:
        Name = '任务栏应用'
        ControlTypeName = 'ButtonControl'
        ClassName = 'Taskbar.TaskListButtonAutomationPeer'
        BoundingRectangle = SimpleNamespace(left=150, top=1528, right=215, bottom=1600)
        def __init__(self, depth):
            self.depth = depth
            if depth:
                self.ControlTypeName = 'PaneControl'
                self.ClassName = '容器'
        def GetChildren(self):
            return [Node(self.depth - 1)] if self.depth else []

    class Context:
        def __enter__(self):
            events.append('uia')
        def __exit__(self, *_args):
            pass
    monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(
        UIAutomationInitializerInThread=Context,
        ControlFromHandle=lambda hwnd: Node(5 if hwnd == 20 else 0)))
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    desktop._dpi = lambda: events.append('physical') or 123
    desktop._user = SimpleNamespace(SetThreadDpiAwarenessContext=lambda old: events.append(('restore', old)))
    desktop.foreground = lambda: {'hwnd': 10}
    desktop._taskbar_handles = lambda: [20]
    result = desktop.controls()
    assert events[0] == 'physical' and events[-1] == ('restore', 123)
    task = next(row for row in result['items'] if row.get('scope') == 'taskbar')
    assert task['top'] == 1528 and task['left'] == 150
    assert result['items'][0] == task


@pytest.mark.parametrize('coordinates', ['', ',"x":1,"y":2'])
def test_taskbar_control_id_uses_physical_center_without_model_scaling(coordinates):
    from desktop_control.planner import LocalPlanner

    class Model:
        def chat(self, text, **_kwargs):
            assert 'c0' in text
            return '{"message":"点击任务栏应用","name":"click","control_id":"c0"' + coordinates + '}'
    observation = {'width': 1440, 'height': 900, 'source_width': 2560, 'source_height': 1600,
                   'left': 0, 'top': 0, 'controls': [{'name': '应用', 'left': 149, 'top': 1528, 'right': 215, 'bottom': 1600}]}
    action = LocalPlanner({}, model=Model()).next('打开应用', observation, [])
    assert action['x'] == 182 and action['y'] == 1564
    assert 'control_id' not in action


def test_unknown_control_id_cannot_fall_back_to_invented_coordinates():
    from desktop_control.planner import LocalPlanner

    class Model:
        def chat(self, _text, **_kwargs):
            return '{"name":"click","control_id":"c99","x":100,"y":100}'
    with pytest.raises(ValueError, match='控件编号'):
        LocalPlanner({}, model=Model()).next('打开', {'controls': []}, [])
