# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The single boundary to the host Hermes runtime.

Every import of a Hermes module lives in this file and nowhere else in the
package, and the test suite enforces that with an AST walk. The rule earns its
keep three times: the whole rest of the plugin stays importable and testable
on an interpreter that has never seen Hermes, every version-sensitive symbol sits
in one place to re-check when the host moves, and ``import standin`` succeeds for
someone who will never install Hermes Agent at all.

Hermes Agent ships its own host, and it loads this plugin through the
``hermes_agent.plugins`` entry point. So the absence of the host is a NORMAL
state here, not an error condition: it is what CI, a fork's test run and
``pip install "standin-sdk[hermes-agent]"`` all look like.
Every function below therefore either degrades to a documented empty value or
raises :class:`HermesUnavailable` - one named exception carrying a sentence that
says which surface is missing and what that costs - never a bare traceback out
of an ``import`` statement deep in a call.

Surfaces, and why each one is reached the way it is:

* ``run_agent.AIAgent`` - RESIDENT. ``ctx.llm`` is completion-only and
  ``delegate_task`` needs a parent agent a serve process does not have, so a
  tool-capable consult has no public path. This is the one that matters: it is
  what makes ``hermes_agent_consult`` the caller's actual Hermes agent rather
  than a second, weaker assistant wearing its name.
* ``hermes_cli.config.load_config`` - RESIDENT. There is no ``ctx.config``
  accessor; the framework itself reads ``plugins.entries.<id>.config`` this way.
* ``agent.prompt_builder.load_soul_md`` / ``build_skills_system_prompt`` - the
  same identity and skills text Hermes injects into every chat prompt, so
  "what is your name?" gets one answer on a call and in chat.
* ``hermes_constants.get_hermes_home`` - public, mandated for all paths.

Deliberately NOT here: the Bot Framework file-card sender, image generation,
browser screenshots and the TTS provider chain. Those belong to surfaces this
plugin does not have on the StandIn seam (the avatar tile) or does not need
in realtime mode (the model speaks for itself).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .log import logger

__all__ = [
    "PLUGIN_KEY",
    "HermesUnavailable",
    "build_consult_agent",
    "host_available",
    "hermes_home",
    "load_hermes_config",
    "model_config_block",
    "plugin_config_block",
    "plugin_context",
    "probe_boundaries",
    "set_plugin_context",
    "skills_index_text",
    "soul_text",
]

#: The entry-point name, and the key ``plugins.entries.<key>`` uses in
#: config.yaml. Fixed on purpose, because it is an operator-facing key: an operator
#: upgrading keeps their existing config block.
PLUGIN_KEY = "msteams_bridge"

#: Hermes module roots. Named here rather than in the test so the list of what
#: counts as "the host" lives next to the code that imports it.
HOST_ROOTS = frozenset({"agent", "gateway", "hermes_cli", "hermes_constants", "run_agent", "tools"})


class HermesUnavailable(ImportError):
    """A Hermes host surface this plugin needs is not importable.

    An :class:`ImportError` subclass because that is what it is, and a named one
    so a caller can catch exactly this and say something useful. Raised only by
    the functions that CANNOT degrade - :func:`build_consult_agent` and
    :func:`hermes_home`. Everything else returns an empty value and logs.
    """


# ---- PluginContext -------------------------------------------------------

_ctx: Any = None


def set_plugin_context(ctx: Any) -> None:
    """Capture the ``PluginContext`` handed to ``register(ctx)``.

    Called once, by :func:`standin.plugins.hermes_agent.register`. Everything here
    works without it - under tests, or when the plugin is run standalone with
    ``python -m standin.plugins.hermes_agent`` - which is why nothing asserts on it.
    """
    global _ctx
    _ctx = ctx


def plugin_context() -> Any:
    """The captured ``PluginContext``, or ``None`` outside a Hermes host."""
    return _ctx


def host_available() -> bool:
    """Is a Hermes host importable in this interpreter?

    A cheap spec lookup, not an import: this is called to decide what to put in
    a log line or a status row, and importing ``run_agent`` to find out would
    pull the whole agent stack into a process that may only be running tests.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("hermes_constants") is not None
    except (ImportError, ValueError):
        return False


# ---- configuration -------------------------------------------------------


def load_hermes_config() -> dict:
    """The host's ``config.yaml`` as a dict, or ``{}``.

    Prefer the host's read-only loader, matching PluginContext.get_config.
    Older hosts can fall back to load_config. Every plugin value also has an
    environment fallback and a default when no host is installed.
    """
    try:
        from hermes_cli import config as host_config

        loader = getattr(host_config, "load_config_readonly", None) or host_config.load_config
        config = loader()
        return config if isinstance(config, dict) else {}
    except Exception:
        logger.debug("standin: no Hermes config available; using env and defaults", exc_info=True)
        return {}


def plugin_config_block() -> dict:
    """Canonical plugin settings, with legacy config values as per-key fallbacks.

    Hermes writes settings through PluginContext.set_config. Preserve the
    older config subtree for installations that have not migrated yet, using
    the same precedence as the host's plugin-relative get_config accessor.
    """
    entries = load_hermes_config().get("plugins", {})
    entries = entries.get("entries", {}) if isinstance(entries, dict) else {}
    entry = entries.get(PLUGIN_KEY) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return {}

    def overlay(legacy: Any, current: Any) -> dict:
        result = dict(legacy) if isinstance(legacy, dict) else {}
        if isinstance(current, dict):
            for key, value in current.items():
                result[key] = overlay(result.get(key), value) if isinstance(value, dict) else value
        return result

    return overlay(entry.get("config"), entry.get("settings"))


def model_config_block() -> dict:
    """The host's top-level ``model:`` block.

    The consult agent is built from it, so a bare ``AIAgent()`` cannot end up
    with an empty model and answer every question with a deployment error.
    """
    m = load_hermes_config().get("model") or {}
    return m if isinstance(m, dict) else {}


# ---- the agent -----------------------------------------------------------


def build_consult_agent(**kwargs: Any) -> Any:
    """Construct a tool-capable ``run_agent.AIAgent``.

    RESIDENT, and the reason this plugin exists: it is what makes the voice
    consult the caller's own Hermes agent, with their tools, files and skills,
    rather than a second assistant that merely sounds like it.

    Raises:
        HermesUnavailable: no Hermes host in this interpreter. The message names
            the surface, because the fix is always the same one thing - run the
            worker inside Hermes - and a raw ``ModuleNotFoundError: run_agent``
            does not say that to anybody.
    """
    try:
        from run_agent import AIAgent  # heavy: deferred to the first consult
    except ImportError as exc:
        raise HermesUnavailable(
            "the Hermes host is not importable (run_agent.AIAgent), so agent "
            "consults are unavailable: install this plugin into your Hermes "
            "environment and enable it with `plugins.enabled: [msteams_bridge]`"
        ) from exc
    try:
        return AIAgent(**kwargs)
    except TypeError:
        # An older AIAgent without session_id: drop it and retry rather than
        # failing the consult. Continuity is a feature, not a requirement.
        kwargs.pop("session_id", None)
        return AIAgent(**kwargs)


# ---- prompt material -----------------------------------------------------


def soul_text(max_chars: int = 6000) -> str:
    """The operator's SOUL.md persona, or ``""``.

    The same identity slot Hermes injects into every chat prompt. Identity comes
    FROM HERMES and never from this plugin: the realtime instructions are the
    voice-behaviour layer on top (brevity, delegation), not a second persona
    that would answer "who are you?" differently on a call than in chat.
    """
    text = ""
    try:
        from agent.prompt_builder import load_soul_md

        text = load_soul_md() or ""
    except Exception:
        # No loader: read the documented file directly, so a host that moved the
        # builder still gets its persona onto the call.
        try:
            text = (hermes_home() / "SOUL.md").read_text(encoding="utf-8")
        except Exception:
            return ""
    return text.strip()[:max_chars]


def skills_index_text(max_chars: int = 3500) -> str:
    """The compact skills index Hermes injects into chat prompts, or ``""``.

    The same builder, so the call knows about exactly the skills chat knows
    about. Trimmed because voice instructions must stay lean; every skill stays
    reachable through the consult even when its description is trimmed here.
    """
    try:
        from agent.prompt_builder import build_skills_system_prompt

        text = (build_skills_system_prompt() or "").strip()
    except Exception:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (more skills available - the agent can list them)"
    return text


def hermes_home() -> Path:
    """``get_hermes_home()``, the public accessor the contributing guide mandates.

    Raises:
        HermesUnavailable: outside a Hermes host.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError as exc:
        raise HermesUnavailable(
            "the Hermes host is not importable (hermes_constants.get_hermes_home), "
            "so the Hermes home directory cannot be resolved"
        ) from exc
    return Path(get_hermes_home())


# ---- startup probe -------------------------------------------------------


def _probe_ctx() -> bool:
    return _ctx is not None


def _probe_agent() -> bool:
    import importlib.util

    return importlib.util.find_spec("run_agent") is not None


def _probe_config() -> bool:
    import importlib.util

    return importlib.util.find_spec("hermes_cli") is not None


def _probe_prompt() -> bool:
    import importlib.util

    return importlib.util.find_spec("agent") is not None


#: (label, probe, what a miss costs). The cost column is the point: a status
#: line that says a surface is missing without saying what stops working sends
#: the operator to the source to find out.
_PROBES: tuple[tuple[str, Any, str], ...] = (
    ("ctx (PluginContext captured)", _probe_ctx, "running outside a Hermes host"),
    ("run_agent.AIAgent", _probe_agent, "hermes_agent_consult cannot run"),
    ("hermes_cli.config.load_config", _probe_config, "config.yaml is ignored; env only"),
    ("agent.prompt_builder", _probe_prompt, "no SOUL.md persona or skills index"),
)


def probe_boundaries() -> list[dict[str, Any]]:
    """Check every host surface and REPORT, never raise.

    Run at ``serve`` startup so a missing surface is one line in the log before
    the first call, rather than a failed tool on turn forty with a caller on the
    line. Each row is ``{surface, ok, detail}``.
    """
    rows: list[dict[str, Any]] = []
    for label, probe, cost in _PROBES:
        try:
            ok = bool(probe())
        except Exception:
            ok = False
        rows.append({"surface": label, "ok": ok, "detail": "" if ok else cost})
    return rows
