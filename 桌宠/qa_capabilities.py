"""每模型能力缓存与人工覆盖，所有文件均不写密钥。"""
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote

import requests

from cloud_plugin.astrbot_plugin_desktop_link.model_limits import (
    limits_for,
    model_identity,
)


class CapabilityStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.lock = threading.RLock()

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    def entry(self, profile):
        with self.lock:
            return self._load().get(model_identity(profile), {})

    def resolve(self, profile):
        entry = self.entry(profile)
        metadata = entry.get('metadata', {}) if self.clock() - entry.get('at', 0) <= 86400 else {}
        return limits_for(profile, metadata, entry.get('overrides', {}))

    def remember(self, profile, metadata, diagnostic=''):
        # 只存支持的数值能力，不落盘服务商可能返回的额外配置或凭据。
        safe = {k: metadata[k] for k in ('inputTokenLimit', 'outputTokenLimit', 'context_length',
                'context_window', 'max_model_len', 'max_output_tokens') if type(metadata.get(k)) is int}
        if type((metadata.get('top_provider') or {}).get('max_completion_tokens')) is int:
            safe['max_output_tokens'] = metadata['top_provider']['max_completion_tokens']
        with self.lock:
            data = self._load()
            entry = data.setdefault(model_identity(profile), {})
            entry.update(at=self.clock(), metadata=safe, diagnostic=diagnostic, discovery_version=2)
            self._save(data)
        return self.resolve(profile)

    def override(self, profile, values):
        limits_for(profile, self.entry(profile).get('metadata'), values)
        with self.lock:
            data = self._load()
            data.setdefault(model_identity(profile), {})['overrides'] = values
            self._save(data)

    def discover(self, profile, force=False):
        from companion_models import DirectModels
        entry = self.entry(profile)
        if not force and entry.get('discovery_version') == 2 and self.clock() - entry.get('at', -86401) < 86400:
            return self.resolve(profile)
        model = DirectModels(profile)
        if profile.get('access_token'):
            model.headers['Authorization'] = 'Bearer ' + profile['access_token']
        name = quote(str(profile.get('model', '')).removeprefix('models/'), safe='')
        paths = ['/models'] if 'openrouter.ai' in model.base else ['/models/' + name, '/models']
        metadata = {}
        notes = []
        for path in paths:
            label = '模型列表' if path == '/models' else '模型详情'
            try:
                with model.session.get(model.base + path, headers=model.headers, timeout=(5, 8), allow_redirects=False) as response:
                    if response.status_code != 200:
                        notes.append(f'{label}返回HTTP{response.status_code}')
                        if response.status_code in (401, 403):
                            break
                        continue
                    data = response.json()
                    if isinstance(data, dict) and (data.get('error') or data.get('success') is False):
                        notes.append(label + '正文返回错误')
                        continue
                    found = self._model_metadata(data, profile.get('model', ''))
                    metadata.update(found)
                    if any(type(metadata.get(k)) is int and metadata[k] > 0 for k in
                           ('inputTokenLimit', 'context_length', 'context_window', 'max_model_len')):
                        notes.append(label + '已提供上下文上限')
                        break
                    notes.append(label + '未提供上下文上限')
            except (requests.RequestException, ValueError):
                notes.append(label + '读取失败或响应格式不兼容')
        return self.remember(profile, metadata, '；'.join(notes))

    @staticmethod
    def _model_metadata(data, name):
        if isinstance(data, dict):
            data = data.get('data', data.get('models', data))
        if isinstance(data, list):
            data = next((r for r in data if isinstance(r, dict) and
                         str(r.get('id', r.get('name', ''))).removeprefix('models/') == name.removeprefix('models/')), {})
        if not isinstance(data, dict) or data.get('error'):
            return {}
        identity = data.get('id', data.get('name'))
        if identity and str(identity).removeprefix('models/') != name.removeprefix('models/'):
            return {}
        # 不保存错误正文、描述或未知嵌套配置，避免将服务商私有响应落盘。
        result = {k: data[k] for k in ('inputTokenLimit', 'outputTokenLimit', 'context_length', 'context_window',
                  'max_model_len', 'max_output_tokens') if type(data.get(k)) is int}
        top = data.get('top_provider')
        if isinstance(top, dict) and type(top.get('max_completion_tokens')) is int:
            result['max_output_tokens'] = top['max_completion_tokens']
        return result
