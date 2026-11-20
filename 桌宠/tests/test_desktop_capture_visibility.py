"""只采集自身合成窗口，不操作用户鼠标、键盘或外部程序。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows捕获排除集成测试')
@pytest.mark.parametrize('persistent', [False, True])
def test_native_capture_excludes_overlay_without_hiding_or_stealing_focus(persistent):
    script = 'PERSISTENT = ' + repr(persistent) + '\n' + '''
import ctypes
import time
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QApplication, QWidget
from desktop_control.capture_visibility import exclude_capture, restore_capture
from desktop_control.windows import WindowsDesktop
import base64, io
from PIL import Image
app = QApplication([])
flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
class ColorWindow(QWidget):
    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(self.color))
under = ColorWindow(None, flags)
over = ColorWindow(None, flags)
for window, color in ((under, '#00ff00'), (over, '#ff0000')):
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    window.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    window.color = color
    window.setGeometry(60, 60, 100, 100)
    window.show()
    app.processEvents()
    time.sleep(.12)
backend = WindowsDesktop()
user = ctypes.WinDLL('user32')
user.GetForegroundWindow.restype = ctypes.c_void_p
focus = user.GetForegroundWindow()
stamp = backend.input_stamp()
def pixel():
    previous = backend._dpi()
    try:
        rect = __import__('ctypes.wintypes', fromlist=['RECT']).RECT()
        user.GetWindowRect(ctypes.c_void_p(int(over.winId())), ctypes.byref(rect))
        result = backend.capture(region={'left':rect.left+20, 'top':rect.top+20, 'width':20,'height':20}, original=True)
        return Image.open(io.BytesIO(base64.b64decode(result['image_b64']))).getpixel((10, 10))
    finally:
        if previous: user.SetThreadDpiAwarenessContext(previous)
def wait_color(channel):
    until = time.monotonic() + 2
    while time.monotonic() < until:
        app.processEvents()
        color = pixel()
        if color[channel] > 200 and color[1-channel] < 50:
            return
        time.sleep(.02)
    raise AssertionError('自身合成窗未显示预期颜色：' + str(color))
over.raise_()
wait_color(0)
permanent = exclude_capture(over, passthrough=False) if PERSISTENT else None
if PERSISTENT:
    assert permanent is not None
    wait_color(1)
token = exclude_capture(over)
assert token is not None, 'Windows未支持捕获排除/穿透'
app.processEvents()
time.sleep(.15)
assert over.isVisible()
wait_color(1)
assert user.GetForegroundWindow() not in (int(over.winId()), int(under.winId()))
if stamp == backend.input_stamp():
    assert focus == user.GetForegroundWindow()
restore_capture(token)
app.processEvents()
time.sleep(.15)
if PERSISTENT:
    from desktop_control.capture_visibility import _api
    assert not _api().GetWindowLongPtrW(int(over.winId()), -20) & 0x20
    wait_color(1)
    for step in range(1, 5):
        over.move(60 + step * 4, 60 + step * 2)
        over.color = '#0000ff'
        over.update()
        app.processEvents()
        transient = exclude_capture(over)
        wait_color(1)
        restore_capture(transient)
        wait_color(1)
        assert over.isVisible()
    restore_capture(permanent)
    over.color = '#ff0000'
    over.update()
wait_color(0)
over.close()
under.close()
print('自身窗口排除/恢复/无隐藏/不抢焦点通过')
'''
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                            cwd=Path(__file__).resolve().parents[1],
                            env={**os.environ, 'QT_QPA_PLATFORM': 'windows'},
                            capture_output=True, text=True, encoding='utf-8', timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
