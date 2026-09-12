# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What the Deepgram plugin reads from the environment.

One prefix, ``DEEPGRAM_``, plus the SDK-wide ``STANDIN_VISION_*`` that every
plugin shares for looking at a screen share.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from standin._exceptions import StandInError

__all__ = ["DeepgramConfig"]

#: Refuse a host that is not Deepgram: the API key travels to it.
_ALLOWED_HOST_SUFFIX = ".deepgram.com"

DEFAULT_INSTRUCTIONS = (
    "You are a helpful voice assistant on a live Microsoft Teams call. You are speaking "
    "aloud: keep replies short, natural and conversational, and never use markdown, "
    "lists or emoji."
)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StandInError(f"{name} is required to answer calls with Deepgram")
    return value


def _host(name: str, default: str) -> str:
    host = os.environ.get(name, "").strip() or default
    if host != "deepgram.com" and not host.endswith(_ALLOWED_HOST_SUFFIX):
        raise StandInError(f"{name} must be a deepgram.com host, got {host!r}")
    return host


@dataclass(frozen=True)
class DeepgramConfig:
    """Everything the Deepgram plugin needs, resolved once per worker."""

    api_key: str
    """``DEEPGRAM_API_KEY``. Never logged, never sent to StandIn."""

    agent_host: str = "agent.deepgram.com"
    """``DEEPGRAM_AGENT_HOST``, the Voice Agent socket host."""

    api_host: str = "api.deepgram.com"
    """``DEEPGRAM_API_HOST``, for the REST calls."""

    listen_model: str = "nova-3"
    """``DEEPGRAM_LISTEN_MODEL``: speech to text."""

    speak_model: str = "aura-2-thalia-en"
    """``DEEPGRAM_SPEAK_MODEL``: text to speech."""

    think_provider: str = "open_ai"
    """``DEEPGRAM_THINK_PROVIDER``: which LLM vendor Deepgram should reason with."""

    think_model: str = "gpt-4o-mini"
    """``DEEPGRAM_THINK_MODEL``."""

    think_endpoint_url: str | None = None
    """``DEEPGRAM_THINK_ENDPOINT_URL``, to point the thinking step at your own
    model instead of Deepgram's default vendor route."""

    think_endpoint_headers: dict[str, str] = field(default_factory=dict)
    """``DEEPGRAM_THINK_ENDPOINT_HEADERS``, a JSON object. This carries YOUR
    model credentials, so it is never logged."""

    language: str = "en"
    """``DEEPGRAM_LANGUAGE``."""

    instructions: str = DEFAULT_INSTRUCTIONS
    """``DEEPGRAM_INSTRUCTIONS``: the agent's base prompt."""

    greeting: str | None = None
    """``DEEPGRAM_GREETING``: what the agent says first, if anything."""

    log_transcripts: bool = False
    """``DEEPGRAM_LOG_TRANSCRIPTS``. Off by default, and gated a second time on
    the call actually being recorded."""

    @staticmethod
    def from_env() -> DeepgramConfig:
        """Read the configuration, or raise naming the variable that is missing."""
        raw_headers = os.environ.get("DEEPGRAM_THINK_ENDPOINT_HEADERS", "").strip()
        headers: dict[str, str] = {}
        if raw_headers:
            try:
                parsed: Any = json.loads(raw_headers)
            except ValueError as err:
                raise StandInError("DEEPGRAM_THINK_ENDPOINT_HEADERS must be a JSON object") from err
            if not isinstance(parsed, dict):
                raise StandInError("DEEPGRAM_THINK_ENDPOINT_HEADERS must be a JSON object")
            headers = {str(k): str(v) for k, v in parsed.items()}
        return DeepgramConfig(
            api_key=_required("DEEPGRAM_API_KEY"),
            agent_host=_host("DEEPGRAM_AGENT_HOST", "agent.deepgram.com"),
            api_host=_host("DEEPGRAM_API_HOST", "api.deepgram.com"),
            listen_model=os.environ.get("DEEPGRAM_LISTEN_MODEL", "").strip() or "nova-3",
            speak_model=os.environ.get("DEEPGRAM_SPEAK_MODEL", "").strip() or "aura-2-thalia-en",
            think_provider=os.environ.get("DEEPGRAM_THINK_PROVIDER", "").strip() or "open_ai",
            think_model=os.environ.get("DEEPGRAM_THINK_MODEL", "").strip() or "gpt-4o-mini",
            think_endpoint_url=os.environ.get("DEEPGRAM_THINK_ENDPOINT_URL", "").strip() or None,
            think_endpoint_headers=headers,
            language=os.environ.get("DEEPGRAM_LANGUAGE", "").strip() or "en",
            instructions=os.environ.get("DEEPGRAM_INSTRUCTIONS", "").strip()
            or DEFAULT_INSTRUCTIONS,
            greeting=os.environ.get("DEEPGRAM_GREETING", "").strip() or None,
            log_transcripts=os.environ.get("DEEPGRAM_LOG_TRANSCRIPTS") == "true",
        )
