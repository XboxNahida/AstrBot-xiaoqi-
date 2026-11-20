"""稳定Shell图标复核，不点击真实桌面。"""

from copy import deepcopy

import pytest
from PIL import Image

from desktop_control.windows import encode_capture
from test_desktop_control_tools import setup_tools


def prepare_icon():
    tools, backend, settings = setup_tools()
    image = [Image.new("RGB", (200, 100), "white")]
    backend.capture = lambda **kwargs: encode_capture(image[0], -100, 0, 1, True)
    icon = {
        "scope": "desktop",
        "name": "测试程序",
        "runtime_id": [42, 1],
        "hwnd": 42,
        "left": -80,
        "top": 0,
        "right": -20,
        "bottom": 50,
    }
    icons = [icon]
    backend.desktop_icons = lambda point=None: deepcopy(
        [
            item
            for item in icons
            if point is None
            or (
                item["left"] <= point[0] < item["right"]
                and item["top"] <= point[1] < item["bottom"]
            )
        ]
    )
    backend.controls = lambda: {"available": True, "items": backend.desktop_icons()}
    return tools, backend, settings, image, icons


@pytest.mark.parametrize("observation", ["screenshot", "controls"])
def test_stable_desktop_icon_accepts_animated_background(observation):
    tools, backend, _, image, _ = prepare_icon()
    shot = tools.call(observation)
    assert "desktop_icons" not in shot, "截图返回不应夹带私有UIA快照"
    image[0] = Image.new("RGB", (200, 100), "black")
    result = tools.call(
        "click",
        {
            "x": -50,
            "y": 10,
            "count": 2,
            "observation_id": shot["observation_id"],
            "request_id": "icon",
        },
    )
    assert result["status"] == "sent" and len(backend.actions) == 1


@pytest.mark.parametrize(
    "change",
    ["removed", "moved", "renamed", "replaced", "foreground", "monitors", "permission"],
)
def test_same_pixels_do_not_allow_changed_or_covered_icon(change):
    tools, backend, settings, _, icons = prepare_icon()
    shot = tools.call("screenshot")
    if change == "removed":
        icons.clear()
    elif change == "moved":
        icons[0]["left"] += 1
    elif change == "renamed":
        icons[0]["name"] = "另一个程序"
    elif change == "replaced":
        icons[0]["runtime_id"] = [42, 2]
    elif change == "foreground":
        backend.window = {"hwnd": 999}
    elif change == "monitors":
        backend.monitors = lambda: [
            {"id": 2, "left": -100, "top": 0, "width": 200, "height": 100}
        ]
    else:
        settings["cloud_enabled"] = False
    with pytest.raises(
        ValueError, match="observation_changed|foreground_changed|cloud_disabled"
    ):
        tools.call(
            "click",
            {
                "x": -50,
                "y": 10,
                "observation_id": shot["observation_id"],
                "request_id": change,
            },
        )
    assert not backend.actions


def test_non_icon_target_still_requires_unchanged_pixels():
    tools, backend, _, image, _ = prepare_icon()
    shot = tools.call("screenshot")
    image[0] = Image.new("RGB", (200, 100), "black")
    with pytest.raises(ValueError, match="observation_changed"):
        tools.call(
            "click",
            {
                "x": 70,
                "y": 70,
                "observation_id": shot["observation_id"],
                "request_id": "background",
            },
        )
    assert not backend.actions


def test_drag_to_background_does_not_ignore_changed_source_identity():
    tools, backend, _, _, icons = prepare_icon()
    shot = tools.call("screenshot")
    icons[0]["runtime_id"] = [42, 2]
    with pytest.raises(ValueError, match="observation_changed"):
        tools.call(
            "drag",
            {
                "x": -50,
                "y": 10,
                "to_x": 70,
                "to_y": 70,
                "observation_id": shot["observation_id"],
                "request_id": "drag",
            },
        )
    assert not backend.actions


@pytest.mark.parametrize(
    "covered,foreground", [(False, 1), (True, 1), (False, 999), (False, "pet")]
)
def test_windows_shell_reader_requires_visible_icon_and_desktop_foreground(
    monkeypatch, covered, foreground
):
    import os
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    from desktop_control.windows import WindowsDesktop

    control = SimpleNamespace(
        ControlTypeName="ListItemControl",
        Name="测试程序",
        BoundingRectangle=SimpleNamespace(left=-80, top=0, right=-20, bottom=50),
        GetRuntimeId=lambda: [42, 1],
        IsOffscreen=False,
    )
    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        SimpleNamespace(
            UIAutomationInitializerInThread=nullcontext,
            ControlFromHandle=lambda handle: SimpleNamespace(
                GetChildren=lambda: [control]
            ),
        ),
    )
    desktop = WindowsDesktop.__new__(WindowsDesktop)
    desktop._dpi = lambda: None
    desktop._user = SimpleNamespace(IsChild=lambda shell, child: False)
    desktop._desktop_listviews = lambda: [(1, 42)]
    desktop._desktop_icon_visible = lambda listing, point: not covered
    desktop.foreground = lambda: {
        "hwnd": foreground if foreground != "pet" else 1000,
        "pid": os.getpid() if foreground == "pet" else 1,
    }
    items = desktop.desktop_icons()
    if covered or foreground not in (1, "pet"):
        assert not items
    else:
        assert (
            len(items) == 1
            and items[0]["left"] == -80
            and items[0]["runtime_id"] == [42, 1]
        )
        assert desktop.desktop_icons((-50, 10)) == items
        assert not desktop.desktop_icons((70, 70))
