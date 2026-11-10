"""Verify independent Codex settings without editing live configuration."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrbot_plugin_private_companion.page_api import PrivateCompanionPageApi


@pytest.fixture
def config_page():
    page = PrivateCompanionPageApi.__new__(PrivateCompanionPageApi)
    page.plugin = SimpleNamespace(config={}, conversation_chat_provider_id="gemini")
    page._schema_key_index_cache = None
    return page


def test_codex_schema_and_bootstrap_independent_defaults(config_page):
    page = config_page
    for key, value in (
        ("task_execution_engine", "codex"),
        ("codex_reasoning_effort", "medium"),
    ):
        assert key in page._allowed_setting_keys()
        assert page._schema_item_for_key(key)["default"] == value
    source = Path(__file__).resolve().parents[1] / "plugin_bootstrap.py"
    tree = ast.parse(source.read_text(encoding="utf8"))
    assignments = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Attribute)
            and t.attr in {"task_execution_engine", "codex_reasoning_effort"}
            for t in n.targets
        )
    ]
    assert len(assignments) == 3
    function = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name == "_initialize_world_and_model_config"
    )
    body = [
        n
        for n in function.body
        if assignments[0].lineno <= n.lineno <= assignments[-1].lineno
    ]
    plugin = SimpleNamespace(_cfg_raw=lambda c, k, d: c.get(k, d))
    exec(
        compile(ast.Module(body=body, type_ignores=[]), str(source), "exec"),
        {"self": plugin, "c": {}},
    )
    assert plugin.task_execution_engine == "codex"
    assert plugin.codex_reasoning_effort == "medium"


@pytest.mark.parametrize(
    "key,value",
    [
        ("task_execution_engine", "other"),
        ("task_execution_engine", None),
        ("codex_reasoning_effort", ""),
        ("codex_reasoning_effort", "a" * 65),
        ("codex_reasoning_effort", "high\n"),
        ("codex_reasoning_effort", "high\x7f"),
        ("codex_reasoning_effort", True),
    ],
)
def test_invalid_codex_settings_rejected_in_chinese(config_page, key, value):
    with pytest.raises(ValueError, match="引擎|思考"):
        config_page._normalize_setting_value(key, value)


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "model-specific"])
def test_supported_nonempty_efforts_kept(config_page, effort):
    assert (
        config_page._normalize_setting_value("codex_reasoning_effort", effort) == effort
    )


def test_hot_update_roundtrip_exposes_overview_without_chat_mutation(config_page):
    page = config_page
    for key, value in (
        ("task_execution_engine", "native"),
        ("codex_reasoning_effort", "xhigh"),
    ):
        normalized = page._normalize_setting_value(key, value)
        page._set_config_value(key, normalized)
        page._apply_config_value(key, normalized)
        assert getattr(page.plugin, key) == value
        assert page._runtime_settings()[key] == value
        assert page._config_get_raw(key) == value
    assert page.plugin.conversation_chat_provider_id == "gemini"
    assert "gemini" not in json.dumps(page.plugin.config)
