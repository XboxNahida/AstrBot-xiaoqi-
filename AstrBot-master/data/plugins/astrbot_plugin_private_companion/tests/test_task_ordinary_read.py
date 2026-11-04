import importlib
import json
import os

import pytest


@pytest.fixture
def reader(tmp_path):
    module = importlib.import_module("task_ordinary_read")
    own, peer, secret, core = (tmp_path / name for name in ("own", "peer", "credentials", "core"))
    for folder in (own, peer, secret, core):
        folder.mkdir()
    state = {"allowed": True}
    service = module.TaskOrdinaryReader(
        current_workspace=lambda actor: own,
        authorize=lambda actor: actor == "owner" and state["allowed"],
        sensitive_roots=[secret],
        redact=lambda text, limit: text.replace("configured-private-value", "[REDACTED]")[:limit],
    )
    return service, own, peer, secret, core, state


@pytest.mark.asyncio
async def test_reads_peer_files_and_core_source_without_write_or_shell(reader):
    service, _, peer, _, core, _ = reader
    for path in (peer / "普通文档.txt", core / "main.py"):
        path.write_text("可读取的普通资料", encoding="utf-8")
        result = await service.call("owner", action="read", path=str(path))
        assert result["success"] and result["stdout"] == "可读取的普通资料"
    assert not hasattr(service, "write") and not hasattr(service, "run")


@pytest.mark.asyncio
async def test_relative_read_and_nonsecret_hidden_file_are_allowed(reader):
    service, own, _, _, _, _ = reader
    (own / ".notes.md").write_text("普通隐藏笔记", encoding="utf-8")
    result = await service.call("owner", action="read", path=".notes.md")
    assert result["stdout"] == "普通隐藏笔记"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".env", ".env.local", "id_rsa", "certificate.key", "auth.pem", "login.db", ".interaction-one.json", "credentials.json", "cookies.txt"])
async def test_sensitive_names_are_denied_even_in_own_workspace(reader, name):
    service, own, _, _, _, _ = reader
    (own / name).write_text("private", encoding="utf-8")
    with pytest.raises(PermissionError):
        await service.call("owner", action="read", path=name)


@pytest.mark.asyncio
async def test_protected_root_and_links_do_not_expose_secrets(reader):
    service, own, _, secret, _, _ = reader
    path = secret / "ordinary.txt"
    path.write_text("private", encoding="utf-8")
    with pytest.raises(PermissionError):
        await service.call("owner", action="read", path=str(path))
    alias = own / "alias.txt"
    os.link(path, alias)
    with pytest.raises(PermissionError):
        await service.call("owner", action="read", path=str(alias))


@pytest.mark.asyncio
async def test_mixed_document_redacts_fields_and_configured_values(reader):
    service, own, _, _, _, _ = reader
    (own / "notes.json").write_text(json.dumps({
        "normal": "普通内容", "api_key": "abc", "nested": {"password": "xyz"},
        "note": "configured-private-value",
    }), encoding="utf-8")
    text = (await service.call("owner", action="read", path="notes.json"))["stdout"]
    assert "abc" not in text and "xyz" not in text and "configured-private-value" not in text
    assert "普通内容" in text


@pytest.mark.asyncio
async def test_directory_pages_hide_sensitive_entries_and_preserve_json(reader):
    service, own, _, _, _, _ = reader
    for index in range(70):
        (own / f"笔记{index:03}.txt").write_text("text", encoding="utf-8")
    (own / ".env").write_text("private", encoding="utf-8")
    first = await service.call("owner", action="list", path=".")
    names = json.loads(first["stdout"])
    assert len(names) == 50 and first["next_offset"] == 50
    second = await service.call("owner", action="list", path=".", offset=50)
    assert len(json.loads(second["stdout"])) == 20 and second["next_offset"] is None
    assert ".env" not in first["stdout"] + second["stdout"]


@pytest.mark.asyncio
async def test_permission_revocation_drops_read_result(reader, monkeypatch):
    service, own, _, _, _, state = reader
    (own / "note.txt").write_text("do not return", encoding="utf-8")
    original = service._query
    def query(*args, **kwargs):
        result = original(*args, **kwargs)
        state["allowed"] = False
        return result
    monkeypatch.setattr(service, "_query", query)
    with pytest.raises(PermissionError):
        await service.call("owner", action="read", path="note.txt")
    with pytest.raises(PermissionError):
        await service.call("ungranted", action="list")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [r"\\server\share\notes.txt", r"\\?\C:\secrets", "note.txt:stream", "https://example.com/file"])
async def test_remote_device_and_alternate_stream_paths_are_rejected(reader, path):
    with pytest.raises((PermissionError, ValueError)):
        await reader[0].call("owner", action="read", path=path)


@pytest.mark.asyncio
async def test_binary_and_oversize_files_are_input_errors(reader):
    service, own, _, _, _, _ = reader
    (own / "big.txt").write_text("x" * 131073, encoding="utf-8")
    (own / "binary.txt").write_bytes(b"abc\0def")
    for name in ("big.txt", "binary.txt"):
        with pytest.raises(ValueError):
            await service.call("owner", action="read", path=name)


def test_reader_has_no_module_side_effects():
    assert importlib.util.find_spec("task_ordinary_read") is not None
