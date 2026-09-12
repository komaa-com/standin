# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What this plugin reads from the environment.

Environment only, matching every other plugin in the SDK and the way the
providers themselves expect their keys to arrive. Nothing here is read at
import: :func:`from_env` runs when you build a handler, so a worker that never
uses ElevenLabs never needs an ElevenLabs key.

One prefix, ``ELEVENLABS_``. The standalone bridge this replaces had grown two
(``ELEVENLABS_`` for the credentials and ``EL_`` for everything else), which is
the sort of thing that survives only until somebody has to document it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from standin._exceptions import StandInError

__all__ = ["ElevenLabsConfig"]

#: Refuse a host that is not ElevenLabs. The agent id and the API key both
#: travel to it, so a mistyped or injected host is credential exfiltration
#: rather than a failed call.
_ALLOWED_HOST_SUFFIX = ".elevenlabs.io"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StandInError(f"{name} is required to answer calls with ElevenLabs")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


@dataclass(frozen=True)
class ElevenLabsConfig:
    """Everything the ElevenLabs plugin needs, resolved once per worker."""

    api_key: str
    """``ELEVENLABS_API_KEY``. Never logged, never sent to StandIn."""

    agent_id: str
    """``ELEVENLABS_AGENT_ID``: which ElevenLabs agent answers the call."""

    host: str = "api.elevenlabs.io"
    """``ELEVENLABS_HOST``. Must be an elevenlabs.io host."""

    environment: str | None = None
    """``ELEVENLABS_ENVIRONMENT``, for agents deployed to a named environment."""

    first_message: str | None = None
    """``ELEVENLABS_FIRST_MESSAGE`` overrides the agent's opening line.

    Only applied when the agent's own security settings allow the override, so
    an agent that has not allowlisted it keeps its configured greeting.
    """

    agent_branch_id: str | None = None
    """``ELEVENLABS_AGENT_BRANCH_ID``, to answer with a specific branch."""

    log_transcripts: bool = False
    """``ELEVENLABS_LOG_TRANSCRIPTS``. Off by default, and gated a second time
    on the call being recorded: a transcript in your logs is a recording of the
    caller that they did not agree to."""

    @staticmethod
    def from_env() -> ElevenLabsConfig:
        """Read the configuration, or raise naming the variable that is missing."""
        host = os.environ.get("ELEVENLABS_HOST", "").strip() or "api.elevenlabs.io"
        if host != "elevenlabs.io" and not host.endswith(_ALLOWED_HOST_SUFFIX):
            raise StandInError(f"ELEVENLABS_HOST must be an elevenlabs.io host, got {host!r}")
        return ElevenLabsConfig(
            api_key=_required("ELEVENLABS_API_KEY"),
            agent_id=_required("ELEVENLABS_AGENT_ID"),
            host=host,
            environment=_optional("ELEVENLABS_ENVIRONMENT"),
            first_message=_optional("ELEVENLABS_FIRST_MESSAGE"),
            agent_branch_id=_optional("ELEVENLABS_AGENT_BRANCH_ID"),
            log_transcripts=os.environ.get("ELEVENLABS_LOG_TRANSCRIPTS") == "true",
        )
