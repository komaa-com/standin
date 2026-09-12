# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""StandIn signatures for WebSocket handshakes, chat POSTs and control requests.

WebSocket handshakes use ``HMAC-SHA256(secret, "{timestampMs}.{id}")``,
lowercase hex, carried in ``X-StandIn-Timestamp`` / ``X-StandIn-Signature``.

    inbound   StandIn dials the call listener; ``id`` is the callId in the URL
              path, and ``CallServer`` VERIFIES inside a replay window.
    outbound  the worker dials the chat channel; ``id`` is the channel name,
              and ``ChatChannel`` SIGNS.

Chat POSTs instead sign the exact body bytes with a five-minute replay window.
Control requests use v2, binding the method, path and hash of the whole body.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

TIMESTAMP_HEADER = "X-StandIn-Timestamp"
SIGNATURE_HEADER = "X-StandIn-Signature"
SIGNATURE_V2_HEADER = "X-StandIn-Signature-V2"

#: Handshakes are dialed and answered immediately; anything older is a replay.
REPLAY_WINDOW_MS = 60_000

#: Chat POSTs allow delayed relay retries; WebSocket upgrades still use 60 s.
CHAT_REPLAY_WINDOW_MS = 300_000


def now_ms() -> int:
    return int(time.time() * 1000)


def sign_handshake(secret: str, timestamp_ms: int | str, handshake_id: str) -> str:
    """Signature for a WebSocket upgrade."""
    payload = f"{timestamp_ms}.{handshake_id}".encode()
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_handshake(
    secret: str,
    timestamp: str | None,
    handshake_id: str,
    signature: str | None,
    current_ms: int | None = None,
) -> bool:
    """Constant-time check of an inbound upgrade. Empty inputs fail CLOSED."""
    if not secret or not timestamp or not signature:
        return False
    # Restrict the timestamp to ASCII decimal, matching the TypeScript SDK.
    # int() alone also accepts underscores and Unicode digits.
    if re.fullmatch(r"-?[0-9]+", timestamp.strip()) is None:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now_ms() if current_ms is None else current_ms) - ts) > REPLAY_WINDOW_MS:
        return False
    expected = sign_handshake(secret, timestamp, handshake_id)
    # compare_digest RAISES TypeError when either str holds a non-ASCII character, and this function is
    # reachable by anyone who can open a socket. Unguarded, a signature header of "\u00fcnicode" turns an
    # unauthenticated upgrade into a 500 instead of a 401 - a crash path, and an oracle that tells a caller
    # "malformed" apart from "wrong". A signature that is not ASCII is not a signature.
    try:
        return hmac.compare_digest(expected, signature.strip().lower())
    except TypeError:
        return False


def sign_body(secret: str, timestamp_ms: int | str, raw_body: str | bytes) -> str:
    """Sign a chat POST's exact transmitted bytes as ``{timestampMs}.{rawBody}``.

    Strings are UTF-8 encoded. Serialize once and send those same bytes; parsing
    and re-serializing JSON can change whitespace, key order or Unicode escaping.
    """
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    payload = f"{timestamp_ms}.".encode() + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_body(
    secret: str,
    timestamp: str | None,
    raw_body: str | bytes,
    signature: str | None,
    current_ms: int | None = None,
    window_ms: int = CHAT_REPLAY_WINDOW_MS,
) -> bool:
    """Verify an inbound chat POST, allowing 300 s of clock skew by default.

    Missing or malformed headers fail closed. This is separate from the
    WebSocket chat channel, whose channel-name signature has a 60 s window.
    """
    if not secret or not timestamp or not signature:
        return False
    if re.fullmatch(r"-?[0-9]+", timestamp.strip()) is None:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now_ms() if current_ms is None else current_ms) - ts) > window_ms:
        return False
    try:
        return hmac.compare_digest(
            sign_body(secret, timestamp, raw_body), signature.strip().lower()
        )
    except TypeError:
        return False


def canonical_request(method: str, path: str, raw_body: str | bytes) -> str:
    """Return ``METHOD\\npath\\nsha256_hex(body)`` for a v2 control request.

    The path is the HTTP request path, without the origin or query string,
    matching the worker verifier. The raw body covers every field, including
    ``tenantId``, which selects the organisation to call.
    """
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    return f"{method.upper()}\n{path}\n{hashlib.sha256(body).hexdigest()}"


def sign_request(
    secret: str, timestamp_ms: int | str, method: str, path: str, raw_body: str | bytes
) -> str:
    """Sign an outbound control request for ``X-StandIn-Signature-V2``.

    Pass the exact bytes you will transmit. v1 handshakes remain supported for
    WebSockets; control requests should send this v2 header.
    """
    return sign_handshake(secret, timestamp_ms, canonical_request(method, path, raw_body))
