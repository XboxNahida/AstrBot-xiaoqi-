"""按角色实际绘制轮廓计算物理边界，统一使用Qt逻辑坐标。"""
from collections import OrderedDict

from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QRegion


def frame_bounds(screen, body):
    left, top = screen.left() - body.left(), screen.top() - body.top()
    return (left, top, max(left, screen.right() + 1 - body.right()),
            max(top, screen.bottom() + 1 - body.bottom()))


class BodyBounds:
    def __init__(self):
        self.cache = OrderedDict()

    def _alpha(self, pixmap, source):
        key = (pixmap.cacheKey(), source.x(), source.y(), source.width(), source.height())
        if key not in self.cache:
            frame = pixmap.copy(source)
            rect = QRegion(frame.mask()).boundingRect() if frame.hasAlphaChannel() else frame.rect()
            self.cache[key] = QRectF(rect.x() / source.width(), rect.y() / source.height(),
                                     rect.width() / source.width(), rect.height() / source.height())
            if len(self.cache) > 32:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def rect(self, pet):
        catalog = getattr(pet, 'actions', {})
        act = catalog.get(getattr(pet, 'current_action_key', None)) if isinstance(catalog, dict) else None
        custom = getattr(pet, "feature_resource_pixmap", None)
        if act and hasattr(pet, "_current_pixmap"):
            pixmap = custom if custom is not None and not custom.isNull() else pet._current_pixmap
            if pixmap.isNull():
                return QRectF(pet.rect())
            frame = pet.current_frame % act["count"]
            source = pixmap.rect() if pixmap is custom else QRect(
                frame % act["columns"] * act["frame_w"], frame // act["columns"] * act["frame_h"],
                act["frame_w"], act["frame_h"])
            width, height = int(act["frame_w"] * pet.scale_factor), int(act["frame_h"] * pet.scale_factor)
            transform = pet._body_transform()
            transform.translate(15, 15)
            transform.scale(width / source.width(), height / source.height())
            matrix = tuple(getattr(transform, name)() for name in ("m11", "m12", "m21", "m22", "dx", "dy"))
            key = (pixmap.cacheKey(), source.x(), source.y(), source.width(), source.height(), matrix)
            if key not in self.cache:
                frame = pixmap.copy(source)
                shape = QRegion(frame.mask()) if frame.hasAlphaChannel() else QRegion(frame.rect())
                # 必须先变换真实遮罩再取外接框，旋转外接矩形会放大留白。
                self.cache[key] = QRectF(transform.map(shape).boundingRect().intersected(pet.rect()))
                if len(self.cache) > 32:
                    self.cache.popitem(last=False)
            self.cache.move_to_end(key)
            local = self.cache[key]
            return local if not local.isEmpty() else QRectF(pet.rect())
        label = getattr(pet, "avatar_label", None)
        if label is not None:
            pixmap = custom if custom is not None and not custom.isNull() else label.pixmap()
            if pixmap is not None and not pixmap.isNull():
                alpha = self._alpha(pixmap, pixmap.rect())
                if bool(getattr(pet, 'feature_flipped', False)) ^ bool(getattr(pet, 'manual_mirrored', False)):
                    alpha = QRectF(1 - alpha.right(), alpha.y(), alpha.width(), alpha.height())
                ratio = min(label.width() / pixmap.width(), label.height() / pixmap.height())
                width, height = pixmap.width() * ratio, pixmap.height() * ratio
                return QRectF(label.x() + (label.width() - width) / 2 + alpha.x() * width,
                              label.y() + (label.height() - height) / 2 + alpha.y() * height,
                              alpha.width() * width, alpha.height() * height)
        return QRectF(pet.rect())
