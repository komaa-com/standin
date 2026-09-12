# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What the Cartesia plugin reads from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from standin._exceptions import StandInError

__all__ = ["CartesiaConfig"]

#: Refuse a host that is not Cartesia: the API key travels to it.
_ALLOWED_HOST_SUFFIX = ".cartesia.ai"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StandInError(f"{name} is required to answer calls with Cartesia")
    return value


@dataclass(frozen=True)
class CartesiaConfig:
    """Everything the Cartesia plugin needs, resolved once per worker."""

    api_key: str
    """``CARTESIA_API_KEY``. Used ONLY to mint a per-call token over HTTPS, so
    the long-lived key never rides the agent socket itself."""

    agent_id: str
    """``CARTESIA_AGENT_ID``: which Line agent answers the call."""

    api_host: str = "api.cartesia.ai"
    """``CARTESIA_API_HOST``."""

    version: str = "2025-04-16"
    """``CARTESIA_VERSION``, sent as the API version header."""

    voice_id: str | None = None
    """``CARTESIA_VOICE_ID``, to override the agent's configured voice."""

    introduction: str | None = None
    """``CARTESIA_INTRODUCTION``: what the agent says first."""

    system_prompt: str | None = None
    """``CARTESIA_SYSTEM_PROMPT``.

    Left unset, the agent keeps the prompt you wrote on Cartesia's platform and
    this plugin adds nothing to it. Set it and the caller's details are
    appended to YOUR prompt. Nothing here ever silently replaces a prompt
    written on the other side.
    """

    @staticmethod
    def from_env() -> CartesiaConfig:
        """Read the configuration, or raise naming the variable that is missing."""
        host = os.environ.get("CARTESIA_API_HOST", "").strip() or "api.cartesia.ai"
        if host != "cartesia.ai" and not host.endswith(_ALLOWED_HOST_SUFFIX):
            raise StandInError(f"CARTESIA_API_HOST must be a cartesia.ai host, got {host!r}")
        return CartesiaConfig(
            api_key=_required("CARTESIA_API_KEY"),
            agent_id=_required("CARTESIA_AGENT_ID"),
            api_host=host,
            version=os.environ.get("CARTESIA_VERSION", "").strip() or "2025-04-16",
            voice_id=os.environ.get("CARTESIA_VOICE_ID", "").strip() or None,
            introduction=os.environ.get("CARTESIA_INTRODUCTION", "").strip() or None,
            system_prompt=os.environ.get("CARTESIA_SYSTEM_PROMPT", "").strip() or None,
        )
