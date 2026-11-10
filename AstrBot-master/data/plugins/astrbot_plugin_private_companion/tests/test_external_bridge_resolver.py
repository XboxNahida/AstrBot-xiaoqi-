# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

from astrbot_plugin_private_companion.external_bridge_resolver import (
    _identity_segments,
    _metadata_matches_star,
    _module_candidates,
    invalidate_external_bridge_cache,
    resolve_external_bridge,
)


def test_resolver_caches_positive_lookup_and_rechecks_lifecycle() -> None:
    calls = 0
    active = {"value": True}

    class Api:
        def bridge_lifecycle_status(self):
            return {"active": active["value"]}

    api = Api()

    def getter():
        nonlocal calls
        calls += 1
        return api

    module_name = "astrbot_plugin_test_bridge.main"
    module = types.ModuleType(module_name)
    module.get_test_api = getter
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        owner = SimpleNamespace()
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is api
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is api
        assert calls == 1

        active["value"] = False
        assert resolve_external_bridge(
            owner,
            cache_key="test",
            module_names=(module_name,),
            getter_name="get_test_api",
            star_name="astrbot_plugin_test_bridge",
        ) is None
    finally:
        invalidate_external_bridge_cache(owner)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_resolver_uses_short_negative_cache_and_registered_star_fallback() -> None:
    owner = SimpleNamespace(
        context=SimpleNamespace(
            get_registered_star=lambda _name: SimpleNamespace(
                star_cls=SimpleNamespace(extension_api=SimpleNamespace(status=lambda: {"enabled": True}))
            )
        )
    )
    assert resolve_external_bridge(
        owner,
        cache_key="registered",
        module_names=("astrbot_plugin_missing_bridge.main",),
        getter_name="get_missing_api",
        star_name="astrbot_plugin_missing_bridge",
    ) is not None


def test_disabled_plugin_remains_discoverable_as_installed() -> None:
    api = SimpleNamespace(status=lambda: {"enabled": False, "available": False})
    module_name = "astrbot_plugin_disabled_bridge.main"
    module = types.ModuleType(module_name)
    module.get_disabled_api = lambda: api
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    owner = SimpleNamespace()
    try:
        assert resolve_external_bridge(
            owner,
            cache_key="disabled",
            module_names=(module_name,),
            getter_name="get_disabled_api",
            star_name="astrbot_plugin_disabled_bridge",
        ) is api
    finally:
        invalidate_external_bridge_cache(owner)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_explicit_inactive_lifecycle_is_rejected() -> None:
    api = SimpleNamespace(
        bridge_lifecycle_status=lambda: {"active": False},
        status=lambda: {"enabled": True},
    )
    module_name = "astrbot_plugin_inactive_bridge.main"
    module = types.ModuleType(module_name)
    module.get_inactive_api = lambda: api
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    owner = SimpleNamespace()
    try:
        assert resolve_external_bridge(
            owner,
            cache_key="inactive",
            module_names=(module_name,),
            getter_name="get_inactive_api",
            star_name="astrbot_plugin_inactive_bridge",
        ) is None
    finally:
        invalidate_external_bridge_cache(owner)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_resolver_uses_current_registered_star_before_stale_module_alias() -> None:
    stale_api = SimpleNamespace(status=lambda: {"enabled": True})
    current_api = SimpleNamespace(status=lambda: {"enabled": True})
    module_name = "astrbot_plugin_reload_bridge.main"
    stale_module = types.ModuleType(module_name)
    stale_module.get_reload_api = lambda: stale_api
    current_module = types.ModuleType("data.plugins.reload_generation_42.main")
    current_module.PLUGIN_NAME = "astrbot_plugin_reload_bridge"
    current_module.get_reload_api = lambda: current_api
    metadata = SimpleNamespace(
        activated=True,
        name="renamed-by-loader",
        root_dir_name="custom-folder",
        module_path=current_module.__name__,
        module=current_module,
        star_cls=None,
    )
    owner = SimpleNamespace(
        context=SimpleNamespace(
            get_all_stars=lambda: [metadata],
            get_registered_star=lambda _name: None,
        )
    )
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = stale_module
    try:
        assert resolve_external_bridge(
            owner,
            cache_key="reload",
            module_names=(module_name,),
            getter_name="get_reload_api",
            star_name="astrbot_plugin_reload_bridge",
        ) is current_api
    finally:
        invalidate_external_bridge_cache(owner)
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_registered_star_identity_can_expose_extension_api_without_fixed_module_name() -> None:
    api = SimpleNamespace(status=lambda: {"enabled": False})
    plugin = SimpleNamespace(extension_api=api)
    metadata = SimpleNamespace(
        activated=True,
        name="display-only-name",
        root_dir_name="astrbot_plugin_custom_bridge",
        module_path="data.plugins.randomized_loader.main",
        module=None,
        star_cls=plugin,
    )
    owner = SimpleNamespace(
        context=SimpleNamespace(
            get_all_stars=lambda: [metadata],
            get_registered_star=lambda _name: None,
        )
    )
    assert resolve_external_bridge(
        owner,
        cache_key="custom",
        module_names=("astrbot_plugin_custom_bridge.main",),
        getter_name="get_custom_api",
        star_name="astrbot_plugin_custom_bridge",
    ) is api


def test_identity_scan_ignores_objects_that_raise_during_string_conversion() -> None:
    class ExplodingString:
        def __str__(self) -> str:
            raise RuntimeError("torch namespace lookup failed")

    assert _identity_segments(ExplodingString()) == set()


def test_metadata_matching_ignores_unstringifiable_plugin_name() -> None:
    class ExplodingString:
        def __str__(self) -> str:
            raise RuntimeError("torch namespace lookup failed")

    metadata = SimpleNamespace(
        name="",
        root_dir_name="",
        module_path="",
        module=SimpleNamespace(__name__="", PLUGIN_NAME=ExplodingString()),
        star_cls=None,
    )
    assert not _metadata_matches_star(metadata, "some_plugin")


def test_module_scan_ignores_unstringifiable_plugin_name() -> None:
    module_name = "astrbot_plugin_torch_namespace_probe.main"
    module = types.ModuleType(module_name)

    class ExplodingString:
        def __str__(self) -> str:
            raise RuntimeError("torch namespace lookup failed")

    module.PLUGIN_NAME = ExplodingString()
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        candidates = _module_candidates(
            ("astrbot_plugin_missing_bridge.main",),
            getter_name="get_missing_api",
            star_name="astrbot_plugin_missing_bridge",
        )
        assert module not in candidates
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def test_module_scan_does_not_trigger_unrelated_lazy_module_attributes(monkeypatch) -> None:
    import astrbot_plugin_private_companion.external_bridge_resolver as resolver

    accesses = []
    lazy = types.ModuleType("unrelated.lazy_extension")

    def lazy_attribute(name):
        accesses.append(name)
        return None

    lazy.__getattr__ = lazy_attribute
    real = types.ModuleType("renamed_plugin.main")
    real.get_test_api = lambda: object()
    monkeypatch.setattr(resolver, "sys", SimpleNamespace(modules={lazy.__name__: lazy, real.__name__: real}))
    candidates = resolver._module_candidates(
        ("missing.main",), getter_name="get_test_api", star_name="test_plugin"
    )
    assert candidates == [real]
    assert accesses == []


def test_module_scan_does_not_evaluate_non_string_plugin_identity(monkeypatch) -> None:
    import astrbot_plugin_private_companion.external_bridge_resolver as resolver

    accesses = []

    class LazyIdentity:
        def __str__(self):
            accesses.append("str")
            return "some_other_plugin"

    unrelated = types.ModuleType("unrelated.identity_proxy")
    unrelated.PLUGIN_NAME = LazyIdentity()
    monkeypatch.setattr(resolver, "sys", SimpleNamespace(modules={unrelated.__name__: unrelated}))
    assert resolver._module_candidates(
        ("missing.main",), getter_name="get_test_api", star_name="test_plugin"
    ) == []
    assert accesses == []


def test_module_scan_ignores_proxy_that_claims_to_be_a_module(monkeypatch) -> None:
    import astrbot_plugin_private_companion.external_bridge_resolver as resolver

    accesses = []

    class ModuleProxy:
        @property
        def __class__(self):
            accesses.append("__class__")
            return types.ModuleType

    monkeypatch.setattr(resolver, "sys", SimpleNamespace(modules={"unrelated.cffi": ModuleProxy()}))
    assert resolver._module_candidates(
        ("missing.main",), getter_name="get_test_api", star_name="test_plugin"
    ) == []
    assert accesses == []
