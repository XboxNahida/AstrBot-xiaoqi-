"""全部Gemini动作的真实目录与有限精灵图缓存。"""

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication

from app_paths import ASSETS_DIR

_APP = QApplication.instance() or QApplication([])


def test_catalog_imports_all_100_without_duplicate_legacy_actions():
    from pet_actions import LEGACY_ACTIONS, load_action_catalog

    catalog = load_action_catalog(ASSETS_DIR)
    upstream = json.loads((Path(ASSETS_DIR) / "gemini_actions/actions.json").read_text(encoding="utf-8"))
    assert len(catalog) == len(upstream["animations"]) == 100
    assert {row["id"] for row in catalog.values()} == {row["id"] for row in upstream["animations"]}
    for key, original in LEGACY_ACTIONS.items():
        row = catalog[key]
        assert row["id"] == original["file"] and row["loop"] == original["loop"]
        assert Path(row["path"]).parent.name == "dynamic_actions"
    for row in catalog.values():
        assert Path(row["path"]).is_file() and row["category"]
        assert row["frame_w"] == 640 and row["frame_h"] == 360
        assert 0 < row["count"] <= row["columns"] * row["rows"]
        assert row["base_fps"] == 12


def test_copied_resources_match_recorded_hashes_and_attribution():
    root = Path(ASSETS_DIR) / "gemini_actions"
    manifest = json.loads((root / "资源校验清单.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "ee29ca4abb489e9257f41ab889a1db115323a267"
    for row in manifest["files"]:
        data = (root / row["path"]).read_bytes()
        assert len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"]
    assert "CC BY-NC-SA 4.0" in (root / "来源与许可.md").read_text(encoding="utf-8")


def test_pixmap_cache_decodes_on_demand_and_evicts_old_sheets(tmp_path):
    from pet_actions import ActionSheetCache

    paths = []
    for index in range(3):
        path = tmp_path / f"frame-{index}.png"
        pixmap = QPixmap(16, 12)
        pixmap.fill(QColor(index * 30, 50, 80))
        assert pixmap.save(str(path))
        paths.append(path)
    cache = ActionSheetCache(max_entries=2)
    assert len(cache) == 0
    first = cache.get(paths[0])
    assert cache.get(paths[0]) is first and len(cache) == 1
    for path in paths[1:]:
        assert not cache.get(path).isNull() and len(cache) <= 2
    assert cache.get(paths[0]) is not first
    with pytest.raises(ValueError, match="动画"):
        cache.get(tmp_path / "absent.png")
    assert len(cache) == 2


