# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Reading configuration, and failing usefully when it is wrong.

Every plugin needs the same four things: a value that must be set, one that may
be, a boolean, and a check that a vendor host is really that vendor's. Each one
had written its own, so the error a user saw for a missing key depended on which
provider they happened to pick.

The host check is the one worth reading twice. Your API key travels to whatever
host the configuration names, so a mistyped or injected host is not a failed
call, it is credential exfiltration. Pinning the suffix costs one line and
closes it.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._exceptions import StandInError

__all__ = ["flag", "json_object", "optional", "required", "vendor_host"]


def required(name: str, purpose: str = "") -> str:
    """Read a variable that must be set, or raise naming it.

    ``purpose`` completes the sentence "X is required to ...", so write it as a
    verb phrase: ``"answer calls with ElevenLabs"``.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        tail = f" to {purpose}" if purpose else ""
        raise StandInError(f"{name} is required{tail}")
    return value


def optional(name: str, default: str | None = None) -> str | None:
    """Read a variable that may be set. Blank reads as absent."""
    return os.environ.get(name, "").strip() or default


def flag(name: str, default: bool = False) -> bool:
    """Read a boolean. Only ``true`` is true, so a typo is off rather than on.

    Deliberately strict. A setting that turns a guard OFF must not be turned off
    by ``TRUE``, ``1`` or ``yes`` landing in a config file by accident, and the
    ones here that matter are all guards.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw == "true"


def vendor_host(name: str, default: str, suffix: str) -> str:
    """Read a host and refuse one that is not the vendor's.

    Your API key travels to this host. A mistyped or injected value is
    credential exfiltration rather than a failed call, which is why this raises
    instead of warning.
    """
    host = os.environ.get(name, "").strip() or default
    bare = suffix.lstrip(".")
    if host != bare and not host.endswith(suffix):
        raise StandInError(f"{name} must be a {bare} host, got {host!r}")
    return host


def json_object(name: str) -> dict[str, str]:
    """Read a JSON object of strings, or raise saying it must be one.

    Used for header maps, which carry YOUR credentials to somebody else's
    endpoint, so the value is never logged on the failure path.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        parsed: Any = json.loads(raw)
    except ValueError as err:
        raise StandInError(f"{name} must be a JSON object") from err
    if not isinstance(parsed, dict):
        raise StandInError(f"{name} must be a JSON object")
    return {str(k): str(v) for k, v in parsed.items()}
