import base64
import io
from types import SimpleNamespace

from PIL import Image
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from qa_runtime import ScreenshotQA

_APP = QApplication.instance() or QApplication(['qa_lifecycle'])


class Link(QObject):
    status_changed = pyqtSignal(str)
    config = {}  # noqa: RUF012 -- 只读未配对测试配置。


class Original(QObject):
    def __init__(self):
        super().__init__()
        cfg = {'api_provider': 'gemini'}
        self.config_mgr = SimpleNamespace(config=cfg, get=lambda key, default=None: cfg.get(key, default))
        self.bridge = SimpleNamespace(link=Link(), config={'route': 'original', 'cloud': {}, 'profiles': []})


def test_completion_uses_request_conversation_and_does_not_revive_deleted(tmp_path):
    controller = Original()
    qa = ScreenshotQA(controller, tmp_path)
    first = qa.state.current
    qa.state.append(first, 'user', '第一题')
    qa.active_id, qa.busy = first, True
    qa.new()
    second = qa.state.current
    qa._complete((first, '第一题完整解答', ''))
    assert qa.state.current == second and not qa.state.store.messages(second)
    assert qa.state.store.messages(first)[-1]['text'] == '第一题完整解答'
    qa.state.delete(first)
    qa._complete((first, '迟到回复', ''))
    assert not qa.state.store.messages(first)
    qa.close()
    qa._complete((second, '退出后回复', ''))  # 不触碰已关闭SQLite。


def test_worker_snapshot_keeps_all_screenshots_and_closed_window_stays_closed(tmp_path, monkeypatch):
    import qa_requests
    controller = Original()
    qa = ScreenshotQA(controller, tmp_path)
    identity = qa.state.current
    buffer = io.BytesIO()
    Image.new('RGB', (24, 24), 'white').save(buffer, 'JPEG')
    picture = buffer.getvalue()
    qa.state.append(identity, 'user', '原题', [picture])
    qa.state.append(identity, 'assistant', '第一答')
    qa.state.append(identity, 'user', '补图追问', [picture])
    qa.state.change('original', qa.state.personas()[0][0])
    job = qa.state.prepare()
    calls = []
    class Client:
        def __init__(self, *_args):
            self.telemetry = {'cached_tokens': 42}
        def chat(self, messages, on_chunk, **kwargs):
            calls.append(messages)
            on_chunk('完整续答')
            return '完整续答'
    monkeypatch.setattr(qa_requests, 'QAClient', Client)
    from cloud_plugin.astrbot_plugin_desktop_link.model_limits import limits_for
    monkeypatch.setattr(qa.requests.limits, 'discover', lambda _p: limits_for({}))
    from smart_models import SmartModelClient
    monkeypatch.setattr(SmartModelClient, '_original_profile', lambda self: {})
    controller.engine = SimpleNamespace()
    qa._work(job)
    _APP.processEvents()
    assert len(calls[0]) == 3
    assert base64.b64decode(calls[0][0]['content'][1]['image_url']['url'].split(',', 1)[1]) == picture
    assert qa.window is None
    assert qa.state.store.messages(identity)[-1]['text'] == '完整续答'
    settings = qa.state.store.settings(identity)
    assert settings['resolved_identity'] and settings['telemetry']['cached_tokens'] == 42
    qa.close()
