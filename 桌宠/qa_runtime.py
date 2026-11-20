"""答疑窗口的主线程协调；网络独立线程，迟到结果按会话归档。"""
import copy
import threading
import time

from PyQt6.QtCore import QBuffer, QIODevice, QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from qa_budget import mode_for
from qa_requests import QARequests, describe_usage
from qa_session import QASession


class ScreenshotQA(QObject):
    completed = pyqtSignal(object)
    chunk = pyqtSignal(str, str)
    personas_ready = pyqtSignal(object, str)
    binding_ready = pyqtSignal(str, str)
    context_ready = pyqtSignal(object, object, str)

    def __init__(self, controller, root):
        super().__init__(controller)
        self.controller = controller
        self.state = QASession(controller, root)
        self.requests = QARequests(controller, self.state.root)
        self.window, self.selector = None, None
        self.context_dialog = None
        self.pending, self.stream = [], ''
        self.failures = {}
        self.busy, self.closed = False, False
        self.active_id = None
        self.next_render = 0
        self.completed.connect(self._complete)
        self.chunk.connect(self._chunk)
        self.personas_ready.connect(self._personas)
        self.binding_ready.connect(self._bind)
        self.context_ready.connect(self._context_ready)
        controller.bridge.link.status_changed.connect(self._connected)
        if getattr(controller.bridge.link, 'connected', False):
            QTimer.singleShot(0, self.state.replay_today)

    def _connected(self, status):
        if status == '已连接云端' and not self.closed:
            self.state.replay_today()

    def open(self, capture_if_empty=True):
        if self.closed:
            return
        if self.window is None:
            from ui.qa_window import QAWindow
            self.window = QAWindow(self.state.root / '渲染缓存')
            self.window.send_requested.connect(self.send)
            self.window.capture_requested.connect(self.capture)
            self.window.new_requested.connect(self.new)
            self.window.delete_requested.connect(self.delete)
            self.window.selected_conversation.connect(self.select)
            self.window.choice_changed.connect(self.change)
            self.window.personas_requested.connect(self.fetch_personas)
            self.window.retry_requested.connect(lambda: self.send('', retry=True))
            self.window.context_requested.connect(self.configure_context)
            self.window.mode_changed.connect(self.set_mode)
        self.refresh()
        # 消费Windows隐藏启动参数，再重显；仅用户打开时激活。
        self.window.show()
        self.window.hide()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        if capture_if_empty and not self.state.store.messages(self.state.current) and not self.pending:
            self.capture(False)

    def refresh(self):
        if self.window is None or self.closed:
            return
        win, store = self.window, self.state.store
        win.sessions.clear()
        for row in store.conversations():
            win.sessions.addItem(row['title'], row['id'])
        win.sessions.setCurrentIndex(win.sessions.findData(self.state.current))
        settings = store.settings(self.state.current)
        win.mode.setCurrentIndex(win.mode.findData(mode_for(settings)))
        for combo, rows, key in ((win.models, self.state.models(), 'model'),
                                 (win.personas, self.state.personas(), 'persona')):
            combo.clear()
            for row in rows:
                combo.addItem(row[1], row[0])
            index = combo.findData(settings.get(key))
            if index < 0:
                combo.addItem('未加载 · ' + str(settings.get(key)), settings.get(key))
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        win.set_busy(self.busy)
        win.status.setToolTip(win.status.text())
        self.render()

    def render(self):
        if self.window:
            rows = self.state.store.messages(self.state.current)
            if self.pending:
                rows.append({'role': 'user', 'text': '待发送截图（尚未请求模型）',
                             'images': ['pending:' + str(i) for i in range(len(self.pending))]})
            failure = self.failures.get(self.state.current)
            if failure:
                text = '本次请求未完成：' + failure['error']
                if failure['partial']:
                    text += '\n\n以下为已收到的未完成片段，不参与后续上下文：\n\n' + failure['partial']
                rows.append({'role': 'assistant', 'text': text, 'images': []})
            def image(identity):
                return self.pending[int(identity[8:])] if identity.startswith('pending:') else self.state.store.image(identity)
            self.window.renderer.render(rows, image,
                                        self.stream if self.active_id == self.state.current else '')

    def new(self):
        self.pending = []
        self.state.new(self.state.store.settings(self.state.current))
        self.refresh()
        if self.window:
            self.window.status.setText('新对话；旧历史完整保留。')

    def select(self, identity):
        self.pending = []
        self.state.select(identity)
        self.refresh()
        if self.window:
            self.window.status.setText(self.failures.get(identity, {}).get('error') or describe_usage(self.state.store.settings(identity).get('telemetry')) or '可继续追问或补充截图。')

    def change(self):
        self.pending = []
        self.state.change(self.window.models.currentData(), self.window.personas.currentData())
        self.refresh()

    def set_mode(self):
        if self.closed or self.window is None:
            return
        if self.busy:
            self.refresh()
            self.window.status.setText('请等待本次完整回复保存后再切换模式。')
            return
        try:
            mode = self.window.mode.currentData()
            self.state.set_mode(mode)
            self.refresh()
            self.window.status.setText('已切到保留上下文：下次从完整本地历史重新纳入，服从模型硬限制。'
                if mode == 'retain' else '已切到普通模式：下次按12K触发、8K目标重新选择历史，最近4轮优先保留。')
            self.window.status.setToolTip(self.window.status.text())
        except (ValueError, OSError) as exc:
            self.window.status.setText(str(exc))
            self.refresh()

    def delete(self):
        if QMessageBox.question(self.window, '删除对话', '删除整段对话和本地截图？云端当日记录也会删除。') != QMessageBox.StandardButton.Yes:
            return
        self.pending = []
        self.state.delete(self.state.current)
        self.refresh()

    def capture(self, new=False):
        if self.selector is not None:
            return
        from ui.region_selector import RegionSelector
        self.window.hide()
        def begin():
            if self.closed:
                return
            self.selector = RegionSelector()
            self.selector.selected.connect(lambda image: self._captured(image, new))
            self.selector.cancelled.connect(self._capture_cancelled)
            self.selector.show()
            self.selector.raise_()
            self.selector.activateWindow()
        QTimer.singleShot(160, begin)

    def _capture_cancelled(self):
        self.selector = None
        self.open(False)

    def _captured(self, image, new):
        self.selector = None
        if self.closed:
            return
        if new:
            self.new()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'JPEG', 85)
        self.pending.append(bytes(buffer.data()))
        self.open(False)
        self.window.status.setText(f'已附加{len(self.pending)}张截图，输入问题后发送。')

    def send(self, text, retry=False):
        if self.busy or self.closed:
            return
        desktop = getattr(self.controller, 'desktop_control', None)
        if desktop is not None and desktop.busy:
            if self.window:
                self.window.status.setText('桌面任务进行中，请先停止或完成任务再答疑。')
            return
        try:
            if not retry:
                if not text.strip():
                    return
                self.state.append(self.state.current, 'user', text, self.pending)
                self.pending = []
                self.window.input.clear()
            history = self.state.store.messages(self.state.current)
            if not history or history[-1]['role'] != 'user':
                raise ValueError('没有失败待重试的问题，请输入追问或开启新对话')
            job = self.state.prepare(defer_images=True)
            self.failures.pop(job['id'], None)
            self.busy, self.active_id, self.stream = True, job['id'], ''
            self.window.status.setText('正在生成；可切换历史，关闭窗口不清空对话。')
            self.refresh()
            threading.Thread(target=self._work, args=(job,), daemon=True, name='独立截图答疑').start()
        except (ValueError, OSError) as exc:
            self.window.status.setText(str(exc))
            self.refresh()

    def _work(self, job):
        telemetry = {}
        try:
            text, telemetry = self.requests.run(job,
                lambda chunk: self.chunk.emit(job['id'], chunk) if not self.closed else None,
                lambda binding: self.binding_ready.emit(job['id'], binding) if not self.closed else None)
            error = ''
        except Exception as exc:  # noqa: BLE001 -- 后台边界必须释放生成状态。
            text, error = '', str(exc) if isinstance(exc, ValueError) else '答疑未完成，请检查模型配置或网络；可点击重试。'
        if not self.closed:
            self.completed.emit((job['id'], text, error, telemetry))

    def _bind(self, identity, binding):
        if self.closed or not any(r['id'] == identity for r in self.state.store.conversations()):
            return
        settings = self.state.store.settings(identity)
        settings['resolved_identity'] = binding
        self.state.store.update_settings(identity, settings)

    def _context_job(self):
        settings = self.state.store.settings(self.state.current)
        bridge = copy.deepcopy(self.controller.bridge.config)
        return {'id': self.state.current, 'model': settings['model'], 'context_mode': mode_for(settings),
                'profile': next((p for p in bridge['profiles'] if p['id'] == settings['model']), None),
                'cloud': bridge['cloud'], 'original': copy.deepcopy(self.controller.config_mgr.config)}

    def configure_context(self):
        if self.closed or self.window is None:
            return
        if self.context_dialog and self.context_dialog.isVisible():
            self.context_dialog.raise_()
            return
        job = self._context_job()
        self.window.context_button.setEnabled(False)
        self.window.status.setText('正在读取当前模型的上下文配置…')
        def work():
            profile, error = None, ''
            try:
                profile, info = self.requests.profile(job)
                if not info:
                    self.requests.limits.discover(profile)
            except Exception as exc:  # noqa: BLE001 -- 后台配置读取不阻塞桌宠。
                error = str(exc) if isinstance(exc, ValueError) else '读取失败，请检查模型和云端连接。'
            if not self.closed:
                self.context_ready.emit(job, profile, error)
        threading.Thread(target=work, daemon=True, name='答疑配置读取').start()

    def _context_ready(self, job, profile, error):
        if self.closed or self.window is None:
            return
        self.window.context_button.setEnabled(True)
        if not self.window.isVisible():
            return
        if job != self._context_job():
            self.window.status.setText('当前会话或模型配置已变化，请重新打开上下文配置。')
            return
        if error:
            self.window.status.setText(error)
            return
        from ui.qa_context_settings import ContextSettings
        self.context_dialog = ContextSettings(self.requests.limits, profile, self.window, mode=job['context_mode'])
        self.context_dialog.show()

    def _chunk(self, identity, text):
        if self.closed or identity != self.active_id:
            return
        self.stream += text
        if self.window and self.window.isVisible() and time.monotonic() >= self.next_render:
            self.next_render = time.monotonic() + 0.25
            if identity == self.state.current:
                self.window.renderer.update_stream(self.stream)

    def _complete(self, result):
        if self.closed:
            return
        identity, text, error = result[:3]
        telemetry = result[3] if len(result) > 3 else {}
        exists = any(r['id'] == identity for r in self.state.store.conversations())
        if exists and error:
            self.failures[identity] = {'error': error, 'partial': self.stream if identity == self.active_id else ''}
            from log_manager import get_log_manager
            get_log_manager().error('截图答疑失败：' + error)
        elif exists:
            self.failures.pop(identity, None)
        self.busy, self.stream = False, ''
        if exists and text and not error:
            settings = self.state.store.settings(identity)
            # 先落盘完整正文；软预算绝不截断回复，也不在失败时推进发送范围。
            self.state.append(identity, 'assistant', text)
            settings.update(telemetry=telemetry)
            if telemetry.get('resolved_identity'):
                settings['resolved_identity'] = telemetry['resolved_identity']
            cursor = telemetry.get('budget_start')
            if (mode_for(settings) == 'normal' and telemetry.get('context_mode') == 'normal'
                    and type(cursor) is int and cursor >= 0):
                settings['budget_start'] = cursor
            self.state.store.update_settings(identity, settings)
        if self.window:
            if identity == self.state.current:
                self.window.status.setText(error or describe_usage(telemetry) or '回答已保存；可继续追问、补充截图或开启新对话。')
            self.refresh()

    def fetch_personas(self):
        scope = self.controller.bridge.link.journal.scope
        self.window.status.setText('正在读取云端人格…')
        def work():
            try:
                rows = self.controller.bridge.link.rpc('personas', {}, expected_scope=scope).get('personas', [])
                self.personas_ready.emit((scope, rows), '')
            except Exception:  # noqa: BLE001 -- 未配对或旧插件不影响本地答疑。
                self.personas_ready.emit((scope, []), '云端人格读取失败，请检查配对与新版插件。')
        threading.Thread(target=work, daemon=True).start()

    def _personas(self, payload, error):
        scope, rows = payload
        if self.closed or scope != self.controller.bridge.link.journal.scope:
            return
        if not error:
            self.state.cloud_personas = rows
        self.window.status.setText(error or f'已加载{len(rows)}个云端人格。')
        self.refresh()

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.selector:
            selector, self.selector = self.selector, None
            selector.close()
        if self.window:
            self.window.close()
        if self.context_dialog:
            self.context_dialog.close()
        self.state.store.close()
