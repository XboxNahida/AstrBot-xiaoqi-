"""只管理导入副本；原角色资源永不覆盖或删除。"""
import re
import uuid
from pathlib import Path

from PyQt6.QtGui import QImageReader, QPixmap


class ResourceLibrary:
    MAX_BYTES = 20 * 1024 * 1024
    MAX_PIXELS = 16_000_000

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, resource_id):
        if not isinstance(resource_id, str) or not re.fullmatch(r"[0-9a-f]{32}", resource_id):
            raise ValueError("资源编号无效")
        target = self.root / (resource_id + ".png")
        if target.is_symlink() or target.resolve().parent != self.root:
            raise ValueError("禁止访问资源库外文件")
        return target

    def import_image(self, path):
        source = Path(path)
        if not source.is_file() or source.stat().st_size > self.MAX_BYTES:
            raise ValueError("图像不存在或超过20MB")
        reader = QImageReader(str(source))
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > self.MAX_PIXELS or reader.supportsAnimation():
            raise ValueError("仅支持不超过1600万像素的静态图像")
        image = reader.read()
        if image.isNull():
            raise ValueError("图像无法解码")
        image.setText("resource_name", source.stem)
        resource_id = uuid.uuid4().hex
        target = self.path(resource_id)
        if not image.save(str(target), "PNG"):
            target.unlink(missing_ok=True)
            raise ValueError("图像保存失败")
        return {"id": resource_id, "name": source.stem, "type": "static", "path": str(target)}

    def list(self):
        return [{"id": p.stem, "name": QImageReader(str(p)).text("resource_name") or p.stem, "type": "static", "path": str(p)}
                for p in sorted(self.root.glob("*.png"))
                if re.fullmatch(r"[0-9a-f]{32}", p.stem) and not p.is_symlink()]

    def remove(self, resource_id):
        self.path(resource_id).unlink(missing_ok=True)

    def pixmap(self, resource_id):
        return QPixmap(str(self.path(resource_id)))
