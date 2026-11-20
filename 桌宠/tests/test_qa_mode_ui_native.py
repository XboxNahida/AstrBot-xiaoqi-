"""独立Qt进程检查实际答疑窗口，避免WebEngine初始化顺序影响其他测试。"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_startup_uses_software_web_rendering_and_keeps_math_images_stream(tmp_path):
    script = textwrap.dedent('''
        import io
        import os
        import sys
        from pathlib import Path
        import original_main
        from PyQt6.QtCore import QCoreApplication, Qt
        from PyQt6.QtGui import QFontDatabase
        from PyQt6.QtQuick import QQuickWindow, QSGRendererInterface
        from PyQt6.QtQuickWidgets import QQuickWidget
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtTest import QTest
        from PIL import Image
        class StartupChecked(Exception):
            pass
        class InspectApplication:
            setAttribute = QCoreApplication.setAttribute
            def __init__(self, args):
                assert QQuickWindow.sceneGraphBackend() == 'software'
                flags = os.environ['QTWEBENGINE_CHROMIUM_FLAGS'].split()
                assert '--disable-gpu' in flags and '--disable-gpu-compositing' in flags
                assert '--disable-extensions' in flags
                raise StartupChecked
        original_main.QApplication = InspectApplication
        sys.argv = ['original_main.py', '--state-dir', sys.argv[1]]
        try:
            original_main.main()
        except StartupChecked:
            pass
        else:
            raise AssertionError('未检查应用创建前的渲染设置')
        from ui.qa_window import QAWindow
        app = QApplication(['qa-software-rendering'])
        assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc') >= 0
        root = Path(sys.argv[-1])
        win = QAWindow(root / 'render')
        image = io.BytesIO()
        Image.new('RGB', (8, 8), 'pink').save(image, format='JPEG')
        win.renderer.render([{'role': 'assistant', 'text': r'公式 $$a^2+b^2=c^2$$', 'images': ['demo']}],
                            lambda _: image.getvalue())
        win.show()
        for _ in range(150):
            QTest.qWait(20)
            if win.renderer.ready:
                break
        assert win.renderer.ready
        def js_result(expression):
            result = []
            win.renderer.page().runJavaScript(expression, result.append)
            for _ in range(100):
                if result:
                    return result[0]
                QTest.qWait(20)
            raise AssertionError('页面脚本未响应')
        assert js_result("document.querySelectorAll('.katex').length > 0 && document.querySelector('.shot').naturalWidth === 8")
        win.renderer.update_stream('增量追问也能显示')
        assert js_result("document.getElementById('stream-text').innerText.includes('增量追问')")
        quick_widgets = win.renderer.findChildren(QQuickWidget)
        assert quick_widgets
        assert all(widget.quickWindow().rendererInterface().graphicsApi() == QSGRendererInterface.GraphicsApi.Software
                   for widget in quick_widgets)
        QTest.qWait(100)
        win.grab().save(str(root / '软件渲染窗口.png'))
        win.close()
        print('启动前软件后端/原参数保留/实际公式图片流式渲染通过')
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR='2',
               QTWEBENGINE_DISABLE_SANDBOX='1', QTWEBENGINE_CHROMIUM_FLAGS='--disable-extensions')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=40, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '实际公式图片流式渲染通过' in result.stdout


def test_mode_control_style_wheel_busy_and_small_window(tmp_path):
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        from ui.qa_window import QAWindow
        from PyQt6.QtGui import QFontDatabase, QMouseEvent, QWheelEvent
        from PyQt6.QtTest import QSignalSpy, QTest
        from PyQt6.QtWidgets import QApplication
        app = QApplication(['qa-mode-native'])
        assert QFontDatabase.addApplicationFont('C:/Windows/Fonts/msyh.ttc') >= 0
        root = Path(sys.argv[1])
        win = QAWindow(root / 'render')
        assert win.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert win.mode.currentData() == 'normal'
        assert win.mode.itemText(1) == '保留上下文'
        spy = QSignalSpy(win.mode_changed)
        win.mode.setCurrentIndex(1)
        win.mode.activated.emit(1)
        assert len(spy) == 1
        wheel = QWheelEvent(QPointF(3, 3), QPointF(3, 3), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(win.mode, wheel)
        assert win.mode.currentData() == 'retain'
        win.set_busy(True)
        assert not win.mode.isEnabled()
        win.set_busy(False)
        assert win.mode.isEnabled()
        win.resize(360, 360)
        win.show()
        QTest.qWait(200)
        start = win.pos()
        for kind, x in ((QEvent.Type.MouseButtonPress, 100),
                        (QEvent.Type.MouseMove, 145), (QEvent.Type.MouseButtonRelease, 145)):
            buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
            QApplication.sendEvent(win.title, QMouseEvent(kind, QPointF(5, 5), QPointF(x, 100),
                                   Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier))
        assert win.pos() == start + QPoint(45, 0)
        assert win.size_grip.isVisible()
        for control in (win.mode, win.models, win.personas, win.context_button, win.send):
            origin = control.mapTo(win, QPoint())
            assert origin.x() >= 0 and origin.x() + control.width() <= win.width()
        win.grab().save(str(root / '双模式窗口.png'))
        win.close()
        sys.path.insert(0, str(Path.cwd() / 'tests'))
        from qa_runtime import ScreenshotQA
        from test_qa_lifecycle import Original
        qa = ScreenshotQA(Original(), root / 'runtime')
        qa.open(False)
        identity = qa.state.current
        qa.state.append(identity, 'user', '已有问题')
        qa.state.append(identity, 'assistant', '已有完整答案')
        qa.window.mode.setCurrentIndex(1)
        qa.window.mode.activated.emit(1)
        assert qa.state.current == identity
        assert qa.state.store.settings(identity)['context_mode'] == 'retain'
        qa.window.mode.setCurrentIndex(0)
        qa.window.mode.activated.emit(0)
        assert qa.state.store.settings(identity)['context_mode'] == 'normal'
        qa.busy = True
        qa.new()
        other = qa.state.current
        assert not qa.window.mode.isEnabled() and not qa.window.send.isEnabled()
        qa._complete((identity, '跨预算仍完整保存' * 1200, '',
                      {'context_mode': 'normal', 'budget_start': 0}))
        assert qa.state.current == other
        assert qa.window.mode.isEnabled() and qa.window.send.isEnabled()
        assert qa.state.store.messages(identity)[-1]['text'] == '跨预算仍完整保存' * 1200
        qa.close()
        print('实际窗口模式/信号/禁滚轮/生成中锁定/360宽布局通过')
    ''')
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR='2', QTWEBENGINE_DISABLE_SANDBOX='1')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=40, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert '实际窗口模式' in result.stdout
