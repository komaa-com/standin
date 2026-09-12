# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Canonical Hermes settings must control the plugin, including caller policy."""

import sys
from types import ModuleType

import pytest

from standin.plugins.hermes_agent import api as api
from standin.plugins.hermes_agent.config import resolve_config


def test_canonical_settings_override_legacy_caller_policy(monkeypatch):
    config = {
        "plugins": {
            "entries": {
                "msteams_bridge": {
                    "config": {"allow_all": True, "allowlist": ["old-caller"]},
                    "settings": {"allow_all": False, "allowlist": ["allowed-caller"]},
                }
            }
        }
    }
    monkeypatch.setattr(api, "load_hermes_config", lambda: config)
    resolved = resolve_config()
    assert resolved.allow_all is False
    # A tuple, not a list: HermesConfig.allowlist is declared tuple[str, ...] so a
    # caller policy cannot be mutated after it is resolved. Asserting the exact
    # type here is the point, not incidental.
    assert resolved.allowlist == ("allowed-caller",)


def test_nested_settings_preserve_legacy_per_key_fallbacks_without_mutating_config(monkeypatch):
    legacy = {"realtime": {"voice": "alloy", "backend": "azure"}, "session_scope": "per-aad"}
    current = {"realtime": {"voice": "marin"}}
    monkeypatch.setattr(
        api,
        "load_hermes_config",
        lambda: {
            "plugins": {"entries": {"msteams_bridge": {"config": legacy, "settings": current}}}
        },
    )
    assert api.plugin_config_block() == {
        "realtime": {"voice": "marin", "backend": "azure"},
        "session_scope": "per-aad",
    }
    assert legacy["realtime"]["voice"] == "alloy"
    assert "backend" not in current["realtime"]


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"plugins": []},
        {"plugins": {"entries": []}},
        {"plugins": {"entries": {"msteams_bridge": "invalid"}}},
    ],
)
def test_missing_or_malformed_entry_degrades_to_empty(monkeypatch, config):
    monkeypatch.setattr(api, "load_hermes_config", lambda: config)
    assert api.plugin_config_block() == {}


def test_host_read_only_loader_is_preferred(monkeypatch):
    package = ModuleType("hermes_cli")
    module = ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: {"model": "read-only-model"}

    def must_not_write():
        raise AssertionError("mutating config loader should not be called")

    module.load_config = must_not_write
    package.config = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    assert api.load_hermes_config() == {"model": "read-only-model"}


def test_older_host_loader_is_supported(monkeypatch):
    package = ModuleType("hermes_cli")
    module = ModuleType("hermes_cli.config")
    module.load_config = lambda: {"model": "older-host-model"}
    package.config = module
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    assert api.load_hermes_config() == {"model": "older-host-model"}
