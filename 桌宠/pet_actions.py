"""本地动作目录与按需精灵图缓存，不触发网络或音频。"""

import json
import logging
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtGui import QPixmap

LEGACY_ACTIONS = {
    "idle": {"file": "待机呼吸休闲", "loop": True, "title": "待机呼吸"},
    "thinking": {"file": "工作状态-思考冒泡", "loop": True, "title": "思考冒泡"},
    "happy": {"file": "点击回应-开心跃动", "loop": False, "title": "开心跃动"},
    "dangling": {"file": "被鼠标拖拽悬空反馈", "loop": True, "title": "悬空挣扎"},
    "coding": {"file": "写代码", "loop": True, "title": "写代码"},
    "shocked": {"file": "被吓一跳", "loop": False, "title": "被吓一跳"},
}


def _entry(path, meta, *, identity, title, category, loop=False):
    fields = {key: meta[source] for key, source in (
        ("frame_w", "FrameWidth"), ("frame_h", "FrameHeight"), ("columns", "Columns"),
        ("rows", "Rows"), ("count", "FrameCount"))}
    if any(type(value) is not int or value <= 0 for value in fields.values()):
        raise ValueError("动作帧参数无效")
    if fields["count"] > fields["columns"] * fields["rows"] or not path.is_file():
        raise ValueError("动作帧数据不完整")
    return dict(fields, id=identity, title=title, category=category, path=str(path),
                meta=meta, base_fps=max(1, int(meta.get("SampleFps", 12))), loop=loop)


def load_action_catalog(assets_dir):
    root = Path(assets_dir).resolve()
    catalog = {}
    for key, info in LEGACY_ACTIONS.items():
        directory = root / "dynamic_actions"
        metadata = directory / (info["file"] + ".json")
        image = directory / (info["file"] + ".webp")
        if metadata.is_file() and image.is_file():
            meta = json.loads(metadata.read_text(encoding="utf-8"))
            catalog[key] = _entry(image, meta, identity=info["file"], title=info["title"],
                                  category="原有动作", loop=info["loop"])
    pack = root / "gemini_actions"
    index = pack / "actions.json"
    if not index.is_file():
        return catalog
    try:
        rows = json.loads(index.read_text(encoding="utf-8"))["animations"]
        for row in rows:
            identity = row["id"]
            old_key = next((key for key, info in LEGACY_ACTIONS.items() if info["file"] == identity), None)
            if old_key in catalog:
                catalog[old_key]["category"] = row.get("category", "其他")
                continue
            path = (pack / row["sources"]["webp"]).resolve()
            if not path.is_relative_to(pack) or path.suffix != ".webp":
                raise ValueError("动作资源路径无效")
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            key = old_key or "gemini:" + identity
            catalog[key] = _entry(path, meta, identity=identity, title=row.get("name", identity),
                                  category=row.get("category", "其他"),
                                  loop=LEGACY_ACTIONS[old_key]["loop"] if old_key else False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logging.getLogger(__name__).warning("Gemini动作包读取未完成，保留可用动作：%s", type(exc).__name__)
    return catalog


class ActionSheetCache:
    """最多保留两张已解码大图，目录本身不持有像素。"""

    def __init__(self, max_entries=2):
        self.max_entries = max(1, int(max_entries))
        self._sheets = OrderedDict()

    def __len__(self):
        return len(self._sheets)

    def get(self, path):
        path = str(path)
        if path in self._sheets:
            self._sheets.move_to_end(path)
            return self._sheets[path]
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise ValueError("无法读取动画图像")
        self._sheets[path] = pixmap
        while len(self._sheets) > self.max_entries:
            self._sheets.popitem(last=False)
        return pixmap
