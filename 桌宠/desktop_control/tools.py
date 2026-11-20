"""有授权和观察约束的原子工具，不编排模型。"""
import base64
import binascii
import hashlib
import io
import json
import math
import threading
import time
import uuid

from .windows import validate_action


class Tools:
    INPUTS = frozenset({'move', 'click', 'drag', 'scroll', 'type', 'hotkey'})
    LOCAL_OBSERVATION_SECONDS = 15
    CLOUD_OBSERVATION_SECONDS = 120

    def __init__(self, settings_provider, backend):
        self.settings_provider = settings_provider
        self.backend = backend
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._active = False
        self._source = None
        self._session = None
        self._cancel = threading.Event()
        self._observations = {}
        self._results = {}

    def _authorize(self):
        settings = self.settings_provider()
        if settings.get('enabled', False) is not True:
            self.stop()
            raise ValueError('disabled')
        if self._source == 'cloud' and settings.get('cloud_enabled', False) is not True:
            self.stop()
            raise ValueError('cloud_disabled')
        return settings

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf-8')).digest()

    def _pointer_fingerprint(self, snapshot, payload, name):
        """检查目标周围96物理像素；缩放JPEG按源区域尺寸映射。"""
        from PIL import Image
        metadata = {k: v for k, v in snapshot.items() if k != 'image_b64'}
        digest = hashlib.sha256(self._fingerprint(metadata))
        try:
            with Image.open(io.BytesIO(base64.b64decode(snapshot['image_b64'], validate=True))) as image:
                width = snapshot.get('source_width', snapshot['width'])
                height = snapshot.get('source_height', snapshot['height'])
                points = [('x', 'y')] + ([('to_x', 'to_y')] if name == 'drag' else [])
                for xkey, ykey in points:
                    x, y = payload[xkey] - snapshot['left'], payload[ykey] - snapshot['top']
                    box = (max(0, math.floor((x - 48) * image.width / width)),
                           max(0, math.floor((y - 48) * image.height / height)),
                           min(image.width, math.ceil((x + 48) * image.width / width)),
                           min(image.height, math.ceil((y + 48) * image.height / height)))
                    if box[2] <= box[0] or box[3] <= box[1]:
                        return self._fingerprint(snapshot)
                    digest.update(image.crop(box).convert('RGB').tobytes())
            return digest.digest()
        except (OSError, ValueError, binascii.Error):
            # 非图片fake/损坏快照保守降为全帧比较。
            return self._fingerprint(snapshot)

    def _desktop_target_matches(self, observation, payload, name):
        """仅稳定、可命中的Shell图标可使用控件身份代替背景像素。"""
        lookup = getattr(self.backend, 'desktop_icons', None)
        if not callable(lookup) or not observation[8]:
            return None
        points = [('x', 'y')] + ([('to_x', 'to_y')] if name == 'drag' else [])
        matches = []
        for xkey, ykey in points:
            point = payload[xkey], payload[ykey]
            targets = [item for item in observation[8]
                       if item.get('scope') == 'desktop' and item.get('runtime_id')
                       and item['left'] <= point[0] < item['right']
                       and item['top'] <= point[1] < item['bottom']]
            if len(targets) == 1:
                matches.append((point, targets[0]))
        if not all(target in lookup(point) for point, target in matches):
            return False
        return True if len(matches) == len(points) else None

    def begin(self, source):
        if source not in ('local', 'cloud'):
            raise ValueError('invalid_source')
        with self._lock:
            if self._active or not self._operation.acquire(blocking=False):
                raise ValueError('busy')
            try:
                self._source = source
                self._authorize()
                self._session = uuid.uuid4().hex
                self._cancel = threading.Event()
                self._active = True
                self._observations.clear()
                self._results.clear()
                return self._session
            finally:
                self._operation.release()

    def stop(self):
        with self._lock:
            self._cancel.set()
            self._active = False
            self._observations.clear()
            self.backend.release_inputs()
        return {'ok': True, 'status': 'stopped'}

    def call(self, name, payload=None):
        payload = dict(payload or {})
        if name == 'stop':
            return self.stop()
        if name == 'status':
            settings = self.settings_provider()
            if settings.get('enabled') is not True or (self._source == 'cloud' and settings.get('cloud_enabled') is not True):
                self.stop()
            return {'active': self._active, 'source': self._source,
                        'session_id': self._session, 'cancelled': self._cancel.is_set()}
        if name not in self.INPUTS | {'monitors', 'screenshot', 'controls'}:
            raise ValueError('unknown_tool')
        if not self._operation.acquire(blocking=False):
            raise ValueError('busy')
        try:
            settings = self._authorize()
            if not self._active or self._cancel.is_set():
                raise ValueError('inactive')
            if name == 'monitors':
                return {'monitors': self.backend.monitors()}
            if name in ('screenshot', 'controls'):
                if name == 'screenshot' and (set(payload) - {'monitor_id', 'region', 'original'} or not isinstance(payload.get('original', False), bool)):
                    raise ValueError('invalid_capture_arguments')
                if name == 'screenshot' and payload.get('original', False) and settings.get('allow_original', False) is not True:
                    raise ValueError('original_not_allowed')
                before = self.backend.foreground()
                lookup = getattr(self.backend, 'desktop_icons', None)
                icons = lookup() if callable(lookup) else []
                capture_args = {k: payload[k] for k in ('monitor_id', 'region', 'original') if k in payload}
                result = (self.backend.capture(**capture_args)
                          if name == 'screenshot' else {'controls': self.backend.controls()})
                settings = self._authorize()
                if name == 'screenshot' and payload.get('original', False) and settings.get('allow_original', False) is not True:
                    raise ValueError('original_not_allowed')
                if before != self.backend.foreground():
                    raise ValueError('foreground_changed')
                if self._cancel.is_set():
                    raise ValueError('cancelled')
                observation_id = uuid.uuid4().hex
                extent = ((result['left'], result['top'], result.get('source_width', result['width']), result.get('source_height', result['height']))
                          if name == 'screenshot' else None)
                if before != self.backend.foreground():
                    raise ValueError('foreground_changed')
                self._observations = {observation_id: (time.monotonic(), before, name, capture_args, self._fingerprint(result), extent, result, self._fingerprint(self.backend.monitors()), icons)}
                settings = self._authorize()
                if name == 'screenshot' and payload.get('original', False) and settings.get('allow_original') is not True:
                    self._observations.clear()
                    raise ValueError('original_not_allowed')
                if self._cancel.is_set():
                    self._observations.clear()
                    raise ValueError('cancelled')
                return {**result, 'observation_id': observation_id}
            request_id = payload.get('request_id')
            if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
                raise ValueError('invalid_request_id')
            if request_id in self._results:
                return dict(self._results[request_id])
            validate_action({**payload, 'name': name})
            observation = self._observations.get(payload.get('observation_id'))
            # 云端模型往返通常超过15秒；延长引用保留，输入前仍实时复核画面和焦点。
            lifetime = self.CLOUD_OBSERVATION_SECONDS if self._source == 'cloud' else self.LOCAL_OBSERVATION_SECONDS
            if not observation or time.monotonic() - observation[0] > lifetime:
                raise ValueError('stale_observation')
            if observation[1] != self.backend.foreground():
                raise ValueError('foreground_changed')
            if observation[3].get('original', False) and settings.get('allow_original', False) is not True:
                raise ValueError('original_not_allowed')
            current = (self.backend.capture(**observation[3]) if observation[2] == 'screenshot'
                       else {'controls': self.backend.controls()})
            self._authorize()
            if observation[2] == 'controls' and not current['controls'].get('available', False):
                raise ValueError('controls_unavailable')
            changed = observation[4] != self._fingerprint(current)
            if observation[2] == 'screenshot' and name in {'move', 'click', 'drag', 'scroll'}:
                changed = self._pointer_fingerprint(observation[6], payload, name) != self._pointer_fingerprint(current, payload, name)
            if name in {'move', 'click', 'drag', 'scroll'}:
                stable_icon = self._desktop_target_matches(observation, payload, name)
                if stable_icon is not None:
                    changed = not stable_icon
            if changed or observation[7] != self._fingerprint(self.backend.monitors()):
                self._observations.clear()
                raise ValueError('observation_changed')
            if observation[1] != self.backend.foreground():
                raise ValueError('foreground_changed')
            if name in {'move', 'click', 'drag', 'scroll'}:
                for xkey, ykey in [('x', 'y')] + ([('to_x', 'to_y')] if name == 'drag' else []):
                    x, y = payload.get(xkey), payload.get(ykey)
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (x, y)):
                        raise ValueError('invalid_coordinates')
                    if not any(m['left'] <= x < m['left'] + m['width'] and m['top'] <= y < m['top'] + m['height'] for m in self.backend.monitors()):
                        raise ValueError('coordinates_outside_desktop')
                    if observation[5]:
                        left, top, width, height = observation[5]
                        if not (left <= x < left + width and top <= y < top + height):
                            raise ValueError('outside_observation')
            self._observations.clear()
            result = {'ok': True, 'status': 'sent', 'request_id': request_id, 'session_id': self._session}
            # 执行开始即占用请求号；异常不得导致未知输入被重播。
            self._results[request_id] = dict(result, ok=False, status='unknown')
            try:
                settings = self._authorize()
                if observation[3].get('original', False) and settings.get('allow_original') is not True:
                    raise ValueError('original_not_allowed')
                if self._cancel.is_set():
                    raise ValueError('cancelled')
                self.backend.perform({**payload, 'name': name}, self._cancel)
                if self._cancel.is_set():
                    result = dict(result, ok=False, status='cancelled')
                self._results[request_id] = result
                return dict(result)
            finally:
                self.backend.release_inputs()
        finally:
            self._operation.release()
