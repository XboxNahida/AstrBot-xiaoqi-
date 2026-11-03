import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion import page_api_tasks as api_module
from astrbot_plugin_private_companion.page_api_task_tools import SharedTaskToolsPageMixin


@pytest.fixture
def page(tmp_path, monkeypatch):
    class Page(SharedTaskToolsPageMixin):
        def _ok(self, data):
            return {"success": True, "data": data}

        def _error(self, text, *, status_code=400):
            return {"success": False, "status_code": status_code, "error": text}

    request = SimpleNamespace(username="admin", get_json=AsyncMock())
    monkeypatch.setattr(api_module, "request", request)
    page = Page()
    page.plugin = SimpleNamespace(_persistent_task_runtime=SimpleNamespace(workspace=tmp_path))
    return page, request, tmp_path


@pytest.mark.asyncio
async def test_shared_upload_preserves_bytes_and_never_executes(page):
    api, request, root = page
    data = "print('中文工具')".encode()
    request.get_json.return_value = {"name": "辅助脚本.py", "content_base64": base64.b64encode(data).decode()}
    result = await api.upload_shared_task_tool()
    assert result["success"]
    assert (root / "tools/辅助脚本.py").read_bytes() == data
    listing = await api.get_shared_task_tools()
    assert listing["data"]["files"][0]["name"] == "辅助脚本.py"
    assert listing["data"]["shared"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../bad.py", "C:/bad.py", "CON", "a:b", "a\\b.py", "a.", "a/../b"])
async def test_shared_upload_rejects_unsafe_filenames(page, name):
    api, request, _ = page
    request.get_json.return_value = {"name": name, "content_base64": "eA=="}
    assert (await api.upload_shared_task_tool())["status_code"] == 400


@pytest.mark.asyncio
async def test_upload_does_not_replace_existing_file_without_explicit_choice(page):
    api, request, root = page
    (root / "tools").mkdir()
    (root / "tools/a.py").write_bytes(b"old")
    request.get_json.return_value = {"name": "a.py", "content_base64": "bmV3"}
    assert (await api.upload_shared_task_tool())["status_code"] == 409
    assert (root / "tools/a.py").read_bytes() == b"old"
    request.get_json.return_value["replace"] = True
    assert (await api.upload_shared_task_tool())["success"]
    assert (root / "tools/a.py").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_shared_tools_require_real_dashboard_login(page):
    api, request, _ = page
    request.username = ""
    assert (await api.get_shared_task_tools())["status_code"] == 401
    assert (await api.upload_shared_task_tool())["status_code"] == 401
