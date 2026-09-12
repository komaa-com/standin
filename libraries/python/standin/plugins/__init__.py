# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Framework plugins, one subpackage each, all inside the one SDK.

* :mod:`standin.plugins.echo` - answers a real call with the caller's own
  voice. No framework, no extra, nothing to configure but the secret. Run it
  first when you suspect your own agent.
* :mod:`standin.plugins.elevenlabs` - an ElevenLabs agent takes the call.
* :mod:`standin.plugins.deepgram` - a Deepgram Voice Agent takes the call.
* :mod:`standin.plugins.cartesia` - a Cartesia Line agent takes the call.

  Those three are reached over an ordinary WebSocket, so they need no extra:
  aiohttp is already in the base install.

* :mod:`standin.plugins.livekit` - a LiveKit Agent takes the call.
  ``pip install "standin-sdk[livekit]"``.
* :mod:`standin.plugins.hermes_agent` - a Hermes agent takes the call.
  ``pip install "standin-sdk[hermes-agent]"``, inside the Hermes host.

This module imports none of them. That is the rule the whole one-package layout
rests on: ``import standin`` must succeed with only aiohttp installed, so a
plugin is loaded the moment somebody names it and not a moment sooner. See
:func:`standin.__getattr__`.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__: list[str] = []


def __getattr__(name: str) -> Any:
    """Import a plugin the first time somebody names it (PEP 562).

    Without this, ``standin.plugins.livekit`` after a plain
    ``import standin`` is an AttributeError saying the attribute does not
    exist - which is both wrong and unhelpful, because the module is right
    there and the real problem is a missing extra. Importing it here surfaces
    :class:`standin.PluginNotInstalled` instead, with the install line in
    it.
    """
    from standin import _PLUGINS

    if name in _PLUGINS:
        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
