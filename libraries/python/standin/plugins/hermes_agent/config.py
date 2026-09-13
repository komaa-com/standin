# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What the operator configures, and where it is read from.

Two sources, in this order: the ``plugins.entries.msteams_bridge.config`` block
in the host's ``config.yaml``, then a ``MSTEAMS_BRIDGE_*`` environment variable.
Both are supported because both are real: the config block is what the Hermes
plugin docs teach, and the variables are what a container sets. Neither is a
migration path for the other.

This file is deliberately short. Everything about the LISTENER - host, port,
path, the HMAC secret, connection caps, the pre-start and idle watchdogs -
belongs to :class:`standin.CallServer` and its ``STANDIN_*`` variables, and is
not reimplemented here. What is left is genuinely this plugin's policy: who
may call, what continuity a caller gets between calls, and how the assistant
behaves in a room with other people in it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .api import plugin_config_block

__all__ = [
    "DEFAULT_WAKE_PHRASES",
    "PluginConfig",
    "caller_allowed",
    "plugin_env",
    "resolve_config",
    "session_key",
]

#: Addressing the assistant by one of these in a group call is what un-mutes it.
DEFAULT_WAKE_PHRASES: tuple[str, ...] = ("assistant", "hermes")


def plugin_env(name: str, default: str = "") -> str:
    """Read one ``MSTEAMS_BRIDGE_*`` variable. A single indirection point."""
    return os.getenv(name, default)


@dataclass(frozen=True)
class PluginConfig:
    """Resolved plugin policy for a worker. Built by :func:`resolve_config`."""

    #: Wait for Microsoft Teams to report recording ACTIVE before the assistant speaks or
    #: listens. On by default: in most tenants the recording banner is what tells
    #: the humans in the room that a bot is participating.
    require_recording: bool = True

    #: After the call ends, write minutes into the Microsoft Teams chat. Off
    #: unless the operator asks: a recap is customer conversation leaving the
    #: call, and the default must not post one nobody requested.
    meeting_recap: bool = False

    #: Agent memory continuity: ``per-call`` (a fresh session every time),
    #: ``per-thread`` (one session per Microsoft Teams conversation) or ``per-aad`` (one
    #: session per person, across every call they make).
    session_scope: str = "per-call"

    #: Phrases that address the assistant in a group call.
    wake_phrases: tuple[str, ...] = DEFAULT_WAKE_PHRASES

    #: Stay silent in a group call until addressed. Turning this off makes the
    #: assistant answer every turn in a meeting, which is rarely what a meeting
    #: wants.
    require_address: bool = True

    #: After an addressed turn, keep answering for this long without the name,
    #: so a back-and-forth does not need "assistant," on every sentence.
    follow_up_window_ms: int = 12_000

    #: AAD object ids allowed to call. EMPTY MEANS DENY ALL unless
    #: :attr:`allow_all` is set: an unset allowlist must not read as "open".
    allowlist: tuple[str, ...] = ()

    #: Match the allowlist against display names too. Off by default - a display
    #: name is caller-supplied and spoofable, an AAD object id is not.
    allowlist_allow_names: bool = False

    #: Explicit opt-in to accept any caller when the allowlist is empty.
    allow_all: bool = False

    #: Seconds a single agent consult may take before the model is told, in
    #: words, that it did not finish. See :class:`~.consult.AgentConsult`.
    consult_timeout_s: float = 45.0

    #: Override the model the consult agent runs on. Empty means the host's own
    #: ``model:`` block, which is what an operator almost always wants.
    consult_model: str = ""

    #: Extra key/value pairs from the config block that this version does not
    #: know about. Kept rather than dropped so a forward-compatible config does
    #: not silently lose settings a later version will read.
    extra: dict[str, Any] = field(default_factory=dict, repr=False)


def _pick(block: dict, key: str, env: str, default: str = "") -> str:
    """One value: the config block first, then the environment, then a default."""
    value = block.get(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    return plugin_env(env, "").strip() or default


def _bool(block: dict, key: str, env: str, default: bool) -> bool:
    raw = _pick(block, key, env)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _list(block: dict, key: str, env: str) -> tuple[str, ...]:
    """A list from a YAML sequence, or from a comma-separated string.

    A YAML scalar is accepted as a comma list rather than ignored: writing
    ``wake_phrases: "assistant, hermes"`` is a natural mistake, and silently
    treating it as one four-word phrase would mute the assistant forever.
    """
    raw = block.get(key)
    if isinstance(raw, (list, tuple)):
        items = [str(v).strip() for v in raw]
    elif isinstance(raw, str) and raw.strip():
        items = [p.strip() for p in raw.split(",")]
    else:
        items = [p.strip() for p in plugin_env(env, "").split(",")]
    return tuple(i.lower() for i in items if i)


_KNOWN = frozenset(
    {
        "require_recording",
        "meeting_recap",
        "session_scope",
        "wake_phrases",
        "require_address",
        "follow_up_window_ms",
        "allowlist",
        "allowlist_allow_names",
        "allow_all",
        "consult_timeout_s",
        "consult_model",
        "realtime",
    }
)

_SCOPES = ("per-call", "per-thread", "per-aad")


def resolve_config(block: dict | None = None) -> PluginConfig:
    """Resolve the plugin's policy.

    Args:
        block: the ``plugins.entries.msteams_bridge.config`` mapping. Read from
            the host when omitted; pass ``{}`` to force environment-only, which
            is what the tests do.
    """
    if block is None:
        block = plugin_config_block()

    scope = _pick(block, "session_scope", "MSTEAMS_BRIDGE_SESSION_SCOPE", "per-call").lower()
    if scope not in _SCOPES:
        # An unrecognised scope must not silently become the WIDEST one: a typo
        # in "per-aad" would then share one agent session between callers.
        scope = "per-call"

    def _float(key: str, env: str, default: float) -> float:
        try:
            return float(_pick(block, key, env, str(default)))
        except ValueError:
            return default

    def _int(key: str, env: str, default: int) -> int:
        try:
            return int(float(_pick(block, key, env, str(default))))
        except ValueError:
            return default

    return PluginConfig(
        require_recording=_bool(
            block, "require_recording", "MSTEAMS_BRIDGE_REQUIRE_RECORDING", True
        ),
        meeting_recap=_bool(block, "meeting_recap", "MSTEAMS_BRIDGE_MEETING_RECAP", False),
        session_scope=scope,
        wake_phrases=_list(block, "wake_phrases", "MSTEAMS_BRIDGE_WAKE_PHRASES")
        or DEFAULT_WAKE_PHRASES,
        require_address=_bool(block, "require_address", "MSTEAMS_BRIDGE_REQUIRE_ADDRESS", True),
        follow_up_window_ms=_int(
            "follow_up_window_ms", "MSTEAMS_BRIDGE_FOLLOW_UP_WINDOW_MS", 12_000
        ),
        allowlist=_list(block, "allowlist", "MSTEAMS_BRIDGE_ALLOWLIST"),
        allowlist_allow_names=_bool(
            block, "allowlist_allow_names", "MSTEAMS_BRIDGE_ALLOWLIST_ALLOW_NAMES", False
        ),
        allow_all=_bool(block, "allow_all", "MSTEAMS_BRIDGE_ALLOW_ALL", False),
        consult_timeout_s=_float("consult_timeout_s", "MSTEAMS_BRIDGE_CONSULT_TIMEOUT_S", 45.0),
        consult_model=_pick(block, "consult_model", "MSTEAMS_BRIDGE_CONSULT_MODEL"),
        extra={k: v for k, v in block.items() if k not in _KNOWN},
    )


def caller_allowed(config: PluginConfig, aad_id: str | None, display_name: str | None) -> bool:
    """May this caller be answered?

    Deny by default. An empty allowlist means nobody unless ``allow_all`` is
    explicitly set, because the alternative - an unconfigured worker answering
    anyone in the tenant who finds its number - is the wrong default to have
    shipped once.
    """
    if not config.allowlist:
        return config.allow_all
    if (aad_id or "").strip().lower() in config.allowlist:
        return True
    if config.allowlist_allow_names and (display_name or "").strip().lower() in config.allowlist:
        return True
    return False


def session_key(config: PluginConfig, start: Any) -> str:
    """The agent session id for this call, per :attr:`PluginConfig.session_scope`.

    Falls back to the call id whenever the scope's own key is absent: a guest
    caller has no AAD id and a 1:1 call has no meeting thread, and two callers
    sharing an empty key would share one agent memory.
    """
    if config.session_scope == "per-thread":
        key = start.thread_id or start.call_id
    elif config.session_scope == "per-aad":
        key = (start.caller.aad_id or "") or start.call_id
    else:
        key = start.call_id
    return f"teams:{key}"
