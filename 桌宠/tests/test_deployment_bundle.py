import importlib.util
from pathlib import Path

import pytest


def installer():
    path = Path(__file__).resolve().parents[2] / "云端部署" / "安装云端.py"
    spec = importlib.util.spec_from_file_location("desktop_cloud_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proxy_patch_keeps_tls_settings_and_fallback():
    source = "{\n admin off\n}\nhttps://example.com {\n tls internal\n reverse_proxy 127.0.0.1:6185\n log {\n output stdout\n }\n}\n"
    result = installer().caddy_patch(source)
    assert "admin off" in result
    assert "tls internal" in result
    assert "handle /desktop/* {" in result
    assert "handle_path" not in result
    assert result.count("reverse_proxy 127.0.0.1:6185") == 1
    assert "reverse_proxy 127.0.0.1:18210" in result


def test_unknown_existing_proxy_is_preserved():
    with pytest.raises(ValueError):
        installer().caddy_patch(
            "https://example.com {\n reverse_proxy different:6185\n}"
        )
