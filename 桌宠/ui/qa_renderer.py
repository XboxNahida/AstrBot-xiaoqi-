"""本地Markdown/KaTeX渲染；不允许模型HTML、脚本或远程资源。"""
import base64
import json
import tempfile
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView

from app_paths import ASSETS_DIR

from .qa_markup import format_markdown


class LocalPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, kind, is_main):
        return kind != QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in ("file", "about")


class ReplyRenderer(QWebEngineView):
    def __init__(self, cache_dir):
        super().__init__()
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self.cache = tempfile.TemporaryDirectory(prefix="答疑渲染-", dir=cache_dir)
        self.revision = 0
        self.streaming = ''
        self.ready = False
        self.loadFinished.connect(self._loaded)
        self.setPage(LocalPage(self))
        settings = self.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)

    def render(self, messages, image_reader, streaming=""):
        cards = []
        for message in messages:
            body = format_markdown(message["text"])
            images = "".join('<img class="shot" src="data:image/jpeg;base64,' +
                             base64.b64encode(image_reader(identity)).decode("ascii") + '">' 
                             for identity in message.get("images", []))
            label = "我" if message["role"] == "user" else "答疑"
            cards.append(f'<article><b>{label}</b>{images}<div>{body}</div></article>')
        cards.append('<article id="stream-card" style="display:none"><b>答疑 · 正在输出</b><div id="stream-text"></div></article>')
        assets = QUrl.fromLocalFile(str(Path(ASSETS_DIR) / "chat_renderer") + "/").toString()
        document = r'''<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' file: 'unsafe-inline';
style-src 'self' file: 'unsafe-inline'; font-src file:; img-src data:; connect-src 'none'">
<link rel="stylesheet" href="katex.min.css"><style>
body{font:14px 'Microsoft YaHei',sans-serif;color:#332d44;background:#fff8fc;margin:10px;overflow-wrap:anywhere}
article{background:white;border:1px solid #e6dbe6;border-radius:10px;padding:10px;margin-bottom:10px}
b{color:#ed4881}pre{white-space:pre-wrap;background:#f5f0f8;padding:8px} .shot{display:block;max-width:100%;max-height:150px;margin:8px 0}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:4px}.katex-display{overflow-x:auto;overflow-y:hidden}
</style><script src="katex.min.js"></script><script src="auto-render.min.js"></script></head><body>'''
        document += "".join(cards) + r'''<script>
const mathOptions={delimiters:[{left:'$$',right:'$$',display:true},
{left:'\\[',right:'\\]',display:true},{left:'$',right:'$',display:false},{left:'\\(',right:'\\)',display:false}],
throwOnError:false,trust:false}; renderMathInElement(document.body,mathOptions);
function updateStream(html){const card=document.getElementById('stream-card');card.style.display=html?'block':'none';
const target=document.getElementById('stream-text');target.innerHTML=html;renderMathInElement(target,mathOptions);
window.scrollTo(0,document.body.scrollHeight);}
window.scrollTo(0,document.body.scrollHeight);
</script></body></html>'''
        document = document.replace('<link rel="stylesheet"', '<base href="' + assets + '"><link rel="stylesheet"', 1)
        page = Path(self.cache.name) / "view.html"
        page.write_text(document, encoding="utf-8")
        self.revision += 1
        self.streaming, self.ready = streaming, False
        url = QUrl.fromLocalFile(str(page))
        url.setQuery(str(self.revision))
        self.load(url)

    def _loaded(self, success):
        self.ready = success
        if success:
            self.update_stream(self.streaming)

    def update_stream(self, text):
        self.streaming = text
        if self.ready:
            self.page().runJavaScript('updateStream(' + json.dumps(format_markdown(text)) + ')')
