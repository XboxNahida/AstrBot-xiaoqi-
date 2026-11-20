"""确定性图片上传副本与有界LRU；从不改动历史原图。"""
import hashlib
import io
import json
import threading
from collections import OrderedDict

from PIL import Image, ImageOps, UnidentifiedImageError


class ImageCopies:
    def __init__(self, max_cache_bytes=16 * 1024 * 1024):
        self.max_cache_bytes, self.bytes_used = max_cache_bytes, 0
        self.cache, self.lock = OrderedDict(), threading.RLock()

    def prepare(self, raw, policy):
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError('图片原件超过64MiB，请缩小框选区域')
        parameters = [1, policy['image_edge'], policy['jpeg_quality'], policy['max_image_bytes']]
        key = hashlib.sha256(raw + json.dumps(parameters).encode()).hexdigest()
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return {**self.cache[key], 'cache_hit': True}
        try:
            with Image.open(io.BytesIO(raw)) as opened:
                if opened.width * opened.height > 40000000:
                    raise ValueError('图片超过4000万像素，请缩小框选区域')
                if (opened.format == 'JPEG' and opened.mode == 'RGB' and not opened.getexif() and
                        max(opened.size) <= policy['image_edge'] and len(raw) <= policy['max_image_bytes']):
                    result = {'key': key, 'data': raw, 'width': opened.width, 'height': opened.height,
                              'changed': False, 'cache_hit': False}
                    opened.verify()
                    return self._remember(key, result)
                image = ImageOps.exif_transpose(opened).convert('RGB')
                original_size = image.size
                edge, quality = policy['image_edge'], policy['jpeg_quality']
                image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                while True:
                    output = io.BytesIO()
                    image.save(output, 'JPEG', quality=quality, subsampling=0, optimize=False)
                    data = output.getvalue()
                    if len(data) <= policy['max_image_bytes']:
                        break
                    if quality > 60:
                        quality = max(60, quality - 10)
                    elif max(image.size) > 512:
                        edge = max(512, int(max(image.size) * .75))
                        image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                    else:
                        raise ValueError('图片上传副本仍超限，请减少本轮截图或缩小区域')
                result = {'key': key, 'data': data, 'width': image.width, 'height': image.height,
                          'changed': len(raw) != len(data) or original_size != image.size, 'cache_hit': False}
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError('图片无法解码，请重新框选截图') from exc
        return self._remember(key, result)

    def _remember(self, key, result):
        data = result['data']
        with self.lock:
            if key in self.cache:
                return {**self.cache[key], 'cache_hit': True}
            if len(data) <= self.max_cache_bytes:
                self.cache[key] = result
                self.bytes_used += len(data)
                while self.bytes_used > self.max_cache_bytes:
                    _, old = self.cache.popitem(last=False)
                    self.bytes_used -= len(old['data'])
        return result
