"""桌面操作独立配置。"""
import json
import os
import tempfile
from pathlib import Path

DEFAULTS = {'enabled': False, 'cloud_enabled': False, 'allow_original': False,
                'move_pet': True, 'pause_on_input': False, 'prefer_controls': True,
                'profile_id': 'original', 'max_steps': 20, 'max_seconds': 180}


def validate_settings(data):
    if not isinstance(data, dict):
        raise ValueError('invalid_settings')  # noqa: TRY004 -- 统一配置错误
    result = {**DEFAULTS, **{k: v for k, v in data.items() if k in DEFAULTS}}
    for key, maximum in (('max_steps', 100), ('max_seconds', 3600)):
        if isinstance(result[key], bool) or not isinstance(result[key], int) or not 1 <= result[key] <= maximum:
            raise ValueError('invalid_' + key)
    for key in ('enabled', 'cloud_enabled', 'allow_original', 'move_pet', 'pause_on_input', 'prefer_controls'):
        if not isinstance(result[key], bool):
            raise ValueError('invalid_' + key)  # noqa: TRY004 -- 统一配置错误
    if not isinstance(result['profile_id'], str) or not result['profile_id'] or len(result['profile_id']) > 200:
        raise ValueError('invalid_profile_id')
    return result


class ControlStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return dict(DEFAULTS)
        data = json.loads(self.path.read_text(encoding='utf-8'))
        return validate_settings(data)

    def save(self, data):
        result = validate_settings(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
