"""用合成显示器验证截图区域，不读取真实屏幕。"""

from types import SimpleNamespace
from typing import ClassVar

import pytest

from perception import screen_capture


@pytest.mark.parametrize("rect,expected", [
    ({"left": 2000, "top": 100, "width": 900, "height": 600}, {"left": 2000, "top": 100, "width": 900, "height": 600}),
    ({"left": 5000, "top": 100, "width": 900, "height": 600}, None),
    ({"left": -500, "top": 100, "width": 900, "height": 600}, {"left": 0, "top": 100, "width": 400, "height": 600}),
])
def test_strict_target_capture_never_falls_back_to_primary(monkeypatch, rect, expected):
    boxes = []

    class Screens:
        monitors: ClassVar = [
            {"left": 0, "top": 0, "width": 3840, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 1920, "top": 0, "width": 1920, "height": 1080},
        ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def grab(self, box):
            boxes.append(box)
            return SimpleNamespace(size=(1, 1), bgra=b"\x00\x00\x00\xff")

    monkeypatch.setattr(screen_capture.mss, "mss", Screens)
    # 通过当前桌宠入口调用，测试实际使用的严格截图模式。
    from companion_perception import CompanionPerception

    if expected is None:
        with pytest.raises(ValueError, match="截图失败"):
            CompanionPerception.capture(rect)
        assert boxes == []
    else:
        assert CompanionPerception.capture(rect)
        assert boxes == [expected]
