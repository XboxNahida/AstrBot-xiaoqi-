import io
import random

import pytest
from PIL import Image

from cloud_plugin.astrbot_plugin_desktop_link.image_policy import ImageCopies


def test_large_image_copy_is_bounded_deterministic_and_original_untouched():
    raw = io.BytesIO()
    image = Image.frombytes('RGB', (2100, 1800), random.Random(1).randbytes(2100 * 1800 * 3))
    image.save(raw, 'BMP')  # 原图超过10MiB，不上传BMP，只编码JPEG副本。
    source = raw.getvalue()
    assert len(source) > 10 * 1024 * 1024
    cache = ImageCopies(max_cache_bytes=2 * 1024 * 1024)
    policy = {'image_edge': 1536, 'jpeg_quality': 85, 'max_image_bytes': 200000}
    first, second = cache.prepare(source, policy), cache.prepare(source, policy)
    assert first['data'] == second['data'] and source == raw.getvalue()
    assert len(first['data']) <= 200000 and max(first['width'], first['height']) <= 1536
    assert first['data'].startswith(b'\xff\xd8') and second['cache_hit']
    assert cache.bytes_used <= 2 * 1024 * 1024
    assert cache.prepare(source, dict(policy, image_edge=1024))['key'] != first['key']


def test_bad_image_rejected_never_sent_as_broken_base64():
    with pytest.raises(ValueError, match='图片'):
        ImageCopies().prepare(b'not an image', {'image_edge': 1024, 'jpeg_quality': 85, 'max_image_bytes': 1000000})
