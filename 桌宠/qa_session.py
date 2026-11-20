"""网页版式答疑会话状态：完整历史、独立路由和当日文字同步。"""
import copy
import json
import time
from pathlib import Path

from brain.prompts import PERSONA_GUIDES
from cloud_plugin.astrbot_plugin_desktop_link.model_limits import model_identity
from qa_budget import MODES, mode_for
from qa_models import build_messages
from qa_store import ConversationStore


class QASession:
    def __init__(self, controller, root):
        self.controller, self.root = controller, Path(root) / '答疑历史'
        self.store = ConversationStore(self.root)
        self.cloud_personas = []
        state = self.root / '当前会话.json'
        try:
            selected = json.loads(state.read_text(encoding='utf-8')).get('id')
        except (OSError, ValueError):
            selected = None
        rows = self.store.conversations()
        self.current = selected if any(r['id'] == selected for r in rows) else (rows[0]['id'] if rows else None)
        if self.current is None:
            self.new()

    def models(self):
        return [('original', '沿用原版模型')] + [(p['id'], p['name'] + ' · ' + p.get('model', ''))
             for p in self.controller.bridge.config['profiles']] + [('cloud', '云端小七模型')]

    def personas(self):
        rows = [('local:' + name, '本地 · ' + name, guide['desc']) for name, guide in PERSONA_GUIDES.items()]
        custom = self.controller.config_mgr.get('custom_system_prompt', '')
        if custom:
            rows.append(('custom', '本地 · 当前自定义人设', custom))
        rows += [('cloud:' + p['id'], '云端 · ' + p['name'], p['prompt']) for p in self.cloud_personas]
        return rows

    def defaults(self):
        config = self.controller.bridge.config
        route = config.get('route', 'original')
        model = config.get('active_profile') if route == 'direct' else route
        return {'model': model, 'persona': self.personas()[0][0], 'context_mode': 'normal'}

    def select(self, identity):
        self.store.settings(identity)
        self.current = identity
        self.save_current()

    def save_current(self):
        target = self.root / '当前会话.json'
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps({'id': self.current}), encoding='utf-8')
        temp.replace(target)

    def new(self, settings=None):
        selected = settings or self.defaults()
        values = {k: selected[k] for k in ('model', 'persona')}
        values['context_mode'] = mode_for(selected)
        self.current = self.store.create(values)
        self.save_current()
        return self.current

    def change(self, model, persona):
        old = self.store.settings(self.current)
        settings = {'model': model, 'persona': persona, 'context_mode': mode_for(old)}
        if old.get('model') == model and old.get('persona') == persona:
            return
        if self.store.messages(self.current):
            self.new(settings)
        else:
            self.store.update_settings(self.current, settings)

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError('答疑模式无效')
        settings = self.store.settings(self.current)
        if mode_for(settings) == mode:
            return
        settings['context_mode'] = mode
        # 从完整历史重新计算发送范围；原消息和图片始终留在数据库。
        settings.pop('budget_start', None)
        self.store.update_settings(self.current, settings)

    def append(self, identity, role, text, images=()):
        row = self.store.append(identity, role, text, images)
        self.sync(identity, row)
        return row

    def sync(self, identity, row):
        link = self.controller.bridge.link
        if not link.config.get('token'):
            return
        settings = self.store.settings(identity)
        metadata = {k: settings.get(k, '') for k in ('model', 'persona', 'model_name', 'persona_name')}
        metadata['model_name'] = metadata['model_name'] or dict(self.models()).get(metadata['model'], metadata['model'])
        metadata['persona_name'] = metadata['persona_name'] or next((p[1] for p in self.personas() if p[0] == metadata['persona']), metadata['persona'])
        link.report({'_record_type': 'qa_message', 'observed_at': row['created'], 'conversation': identity,
                     'message': str(row['id']), 'role': row['role'], 'text': row['text'], 'settings': metadata},
                    identity='qa-' + identity + '-' + str(row['id']))

    def replay_today(self):
        day = ((int(time.time()) + 28800) // 86400) * 86400 - 28800
        for conversation in self.store.conversations():
            for row in self.store.messages(conversation['id']):
                if row['created'] >= day:
                    self.sync(conversation['id'], row)

    def delete(self, identity):
        link = self.controller.bridge.link
        if link.config.get('token'):
            link.report({'_record_type': 'qa_delete', 'conversation': identity}, identity='qa-delete-' + identity)
        self.store.delete(identity)
        rows = self.store.conversations()
        if self.current == identity:
            self.select(rows[0]['id']) if rows else self.new()

    def prepare(self, *, defer_images=False):
        identity = self.current
        settings = self.store.settings(identity)
        persona = next((p for p in self.personas() if p[0] == settings['persona']), None)
        if persona is None and not settings.get('system'):
            raise ValueError('该人格未加载，请刷新云端人格或选择本地人格')
        system = settings.get('system') or ('你是桌宠的独立图文答疑助手，认真解答问题，支持Markdown和LaTeX。不要套用锐评字数和情绪标签。\n' + persona[2])
        bridge = copy.deepcopy(self.controller.bridge.config)
        model = settings['model']
        if model not in ('original', 'cloud'):
            profile = next((p for p in bridge['profiles'] if p['id'] == model), None)
            if not profile:
                raise ValueError('该模型配置已不存在，请重新选择')
        else:
            profile = None
        original = copy.deepcopy(self.controller.config_mgr.config)
        if model == 'original':
            identity_data = {'provider': original.get('api_provider'), 'model': original.get('model'),
                             'base_url': original.get('custom_api_url', ''),
                             'scope': json.dumps([original.get(k, '') for k in ('vertex_project_id', 'vertex_location')])}
        elif model == 'cloud':
            identity_data = {'provider': 'cloud', 'base_url': bridge['cloud'].get('url', ''),
                             'scope': bridge['cloud'].get('device_id', '')}
        else:
            identity_data = profile
        identity_hash = model_identity(identity_data)
        if settings.get('model_identity') and settings['model_identity'] != identity_hash:
            raise ValueError('本会话的实际模型或接口配置已变更，请开启新对话；旧历史保留')
        settings['model_identity'] = identity_hash
        settings.update(model_name=dict(self.models()).get(model, model),
                        persona_name=persona[1] if persona else settings.get('persona_name', settings['persona']), system=system)
        self.store.update_settings(identity, settings)
        history = self.store.messages(identity)
        messages = ([{'role': r['role'], 'content': [{'type': 'text', 'text': r['text']}] +
                      [{'type': 'local_image', 'id': image} for image in r['images']]} for r in history]
                    if defer_images else build_messages(history, self.store.image, enforce_limit=False))
        return {'id': identity, 'settings': settings, 'system': system, 'model': model, 'profile': profile,
                'cloud': bridge['cloud'], 'original': original,
                'image_database': str(self.root / 'conversations.sqlite') if defer_images else None, 'messages': messages}
