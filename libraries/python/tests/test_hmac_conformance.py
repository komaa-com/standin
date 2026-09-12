# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Shared authentication vectors, including the worker's independent v2 pin."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

# Import the public surface so these tests also guard package exports.
from standin import (
    CHAT_REPLAY_WINDOW_MS,
    REPLAY_WINDOW_MS,
    SIGNATURE_HEADER,
    SIGNATURE_V2_HEADER,
    TIMESTAMP_HEADER,
    canonical_request,
    sign_body,
    sign_handshake,
    sign_request,
    verify_body,
    verify_handshake,
)

pytestmark = pytest.mark.unit
V = json.loads((Path(__file__).resolve().parents[3] / "protocol/conformance.json").read_text())


def _body(case: dict[str, Any]) -> str | bytes:
    return case["body"] if "body" in case else base64.b64decode(case["bodyBase64"])


@pytest.mark.parametrize("case", V["hmacBody"]["sign"], ids=lambda case: case["name"])
def test_body_matches_shared_vector(case: dict[str, Any]) -> None:
    body = _body(case)
    assert sign_body(case["secret"], case["timestampMs"], body) == case["expected"]
    assert verify_body(
        case["secret"], case["timestampMs"], body, case["expected"], int(case["timestampMs"])
    )
    if isinstance(body, str):
        assert (
            sign_body(case["secret"], int(case["timestampMs"]), body.encode()) == case["expected"]
        )


@pytest.mark.parametrize("case", V["hmacBody"]["clockSkew"], ids=lambda case: str(case["skewMs"]))
def test_post_and_websocket_replay_windows_stay_distinct(case: dict[str, Any]) -> None:
    c = V["hmacBody"]["sign"][0]
    now = int(c["timestampMs"]) + case["skewMs"]
    assert (
        verify_body(c["secret"], c["timestampMs"], c["body"], c["expected"], now)
        is case["expected"]
    )
    channel_signature = sign_handshake(c["secret"], c["timestampMs"], "chat")
    assert verify_handshake(c["secret"], c["timestampMs"], "chat", channel_signature, now) is (
        abs(case["skewMs"]) <= 60_000
    )


@pytest.mark.parametrize("timestamp", V["hmacBody"]["malformedTimestamps"])
def test_signed_malformed_timestamps_fail_closed_on_both_lanes(timestamp: str) -> None:
    # Sign the malformed value so a permissive parser cannot hide behind an
    # invalid signature. Unicode digits and underscores are accepted by int().
    assert not verify_body(
        "secret", timestamp, "hello", sign_body("secret", timestamp, "hello"), 1_700_000_000_000
    )
    assert not verify_handshake(
        "secret", timestamp, "chat", sign_handshake("secret", timestamp, "chat"), 1_700_000_000_000
    )


@pytest.mark.parametrize("signature", [None, "", "ab", "0" * 64, "ü" * 32, "🙂" * 16])
def test_body_malformed_signatures_fail_closed(signature: str | None) -> None:
    c = V["hmacBody"]["sign"][0]
    assert not verify_body(
        c["secret"], c["timestampMs"], c["body"], signature, int(c["timestampMs"])
    )


def test_body_missing_credentials_and_tampering_fail_closed() -> None:
    c = V["hmacBody"]["sign"][0]
    now = int(c["timestampMs"])
    for secret in ("", "wrong-secret"):
        assert not verify_body(secret, c["timestampMs"], c["body"], c["expected"], now)
    assert not verify_body(c["secret"], None, c["body"], c["expected"], now)
    assert not verify_body(c["secret"], c["timestampMs"], c["body"] + " ", c["expected"], now)
    assert not verify_body(c["secret"], str(now + 1), c["body"], c["expected"], now)
    assert not verify_body(c["secret"], c["timestampMs"], "chat", c["expected"], now)


def test_body_signature_normalization_and_explicit_window() -> None:
    c = V["hmacBody"]["sign"][0]
    now = int(c["timestampMs"])
    assert verify_body(c["secret"], c["timestampMs"], c["body"], f" {c['expected'].upper()}\n", now)
    assert verify_body(
        c["secret"], c["timestampMs"], c["body"], c["expected"], now + 60_000, 60_000
    )
    assert not verify_body(
        c["secret"], c["timestampMs"], c["body"], c["expected"], now + 60_001, 60_000
    )


@pytest.mark.parametrize("case", V["hmacV2"]["sign"], ids=lambda case: case["name"])
def test_v2_matches_shared_vector(case: dict[str, Any]) -> None:
    body = _body(case)
    assert canonical_request(case["method"], case["path"], body) == case["canonical"]
    assert (
        sign_request(case["secret"], case["timestampMs"], case["method"], case["path"], body)
        == case["expected"]
    )
    if isinstance(body, str):
        assert (
            sign_request(
                case["secret"],
                int(case["timestampMs"]),
                case["method"],
                case["path"],
                body.encode(),
            )
            == case["expected"]
        )


@pytest.mark.parametrize("case", V["hmacV2"]["tamperedRequests"], ids=lambda case: case["name"])
def test_v2_rejects_request_substitution(case: dict[str, Any]) -> None:
    original = V["hmacV2"]["sign"][0]
    assert (
        sign_request(
            original["secret"], original["timestampMs"], case["method"], case["path"], case["body"]
        )
        != original["expected"]
    )


def test_v2_binds_the_timestamp_and_cannot_be_a_v1_user_signature() -> None:
    c = V["hmacV2"]["sign"][0]
    assert (
        sign_request(c["secret"], int(c["timestampMs"]) + 1, c["method"], c["path"], c["body"])
        != c["expected"]
    )
    assert sign_handshake(c["secret"], c["timestampMs"], "u1") != c["expected"]
    assert (
        sign_request(c["secret"], c["timestampMs"], "post", c["path"], c["body"]) == c["expected"]
    )


def test_header_names_and_windows_match_the_wire_contract() -> None:
    assert TIMESTAMP_HEADER.lower() == "x-standin-timestamp"
    assert SIGNATURE_HEADER.lower() == "x-standin-signature"
    assert SIGNATURE_V2_HEADER.lower() == "x-standin-signature-v2"
    assert REPLAY_WINDOW_MS == 60_000
    assert CHAT_REPLAY_WINDOW_MS == 300_000
