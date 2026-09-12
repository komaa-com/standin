# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Runtime helpers shared by the generated StandIn call protocol bindings."""

from __future__ import annotations

import base64
import json
import re
from typing import Any

_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_MAX_SAFE_INTEGER = 2**53 - 1


def clean(value: Any) -> str | None:
    """Read blank strings and non-string identity fields as absent."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_message(raw: str | bytes) -> dict[str, Any] | None:
    """Drop malformed JSON while preserving unknown message types for the receive loop."""
    try:
        # json.loads(bytes) auto-detects UTF-16/32. The wire is UTF-8, matching
        # the TypeScript decoder, so do not accept a second byte encoding here.
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj = json.loads(raw, parse_constant=_reject_json_constant)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
        return None
    return obj


def decode_pcm(payload_base64: Any) -> bytes:
    """Decode canonical standard base64 containing complete, nonempty PCM16 samples."""
    if not isinstance(payload_base64, str) or not payload_base64:
        raise ValueError("audio.frame carries no payloadBase64")
    if _BASE64.fullmatch(payload_base64) is None:
        raise ValueError("audio.frame payloadBase64 is not valid base64")
    pcm = base64.b64decode(payload_base64, validate=True)
    # The syntax check catches padding and alphabet errors. Re-encoding also
    # rejects nonzero padding bits, which Python's decoder otherwise accepts.
    if encode_pcm(pcm) != payload_base64:
        raise ValueError("audio.frame payloadBase64 is not valid base64")
    if len(pcm) < 2 or len(pcm) % 2 != 0:
        raise ValueError(f"malformed PCM16 payload ({len(pcm)} bytes)")
    return pcm


def encode_pcm(pcm: bytes) -> str:
    """Encode raw PCM bytes using canonical standard base64."""
    return base64.b64encode(pcm).decode("ascii")


def encode(message: dict[str, Any]) -> str:
    """Serialize an outbound message as compact JSON."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def normalize_pong_timestamp(value: Any) -> int:
    """Echo safe integer timestamps, using zero for malformed or imprecise values."""
    # JavaScript has one numeric type, so an integral float must agree with
    # Number.isSafeInteger. Python bool is an int subclass and must be excluded.
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER
        and int(value) == value
    ):
        return int(value)
    return 0
