# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT
# GENERATED from protocol/schema.yaml; do not hand-edit.
# Schema SHA-256: 44aab1ddff7b0dea5e067d3d34583fea7271da06aa521b4e1b10b1718d6a79e9
# Regenerate with: python3 protocol/generate.py

"""Generated call context and wire builders with stable SDK defaults.

Codec validation and additive unknown-message handling live in
``_protocol_runtime``. Avatar messages remain outside the handler API.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from ._protocol_runtime import (
    clean,
    encode,
    normalize_pong_timestamp,
)
from ._protocol_runtime import decode_pcm as decode_pcm
from ._protocol_runtime import parse_message as parse_message

SAMPLE_RATE_HZ = 16_000
NUM_CHANNELS = 1

TYPE_SESSION_START = "session.start"
TYPE_SESSION_END = "session.end"
TYPE_RECORDING_STATUS = "recording.status"
TYPE_AUDIO_FRAME = "audio.frame"
TYPE_VIDEO_FRAME = "video.frame"
TYPE_PARTICIPANTS = "participants"
TYPE_DTMF = "dtmf"
TYPE_PING = "ping"
TYPE_ASSISTANT_SAY = "assistant.say"
TYPE_ASSISTANT_CANCEL = "assistant.cancel"
TYPE_EXPRESSION = "expression"
TYPE_SPEECH_MARKS = "speech.marks"
TYPE_DISPLAY_IMAGE = "display.image"
TYPE_DISPLAY_FRAME = "display.frame"
TYPE_PONG = "pong"


@dataclass(frozen=True)
class Caller:
    """Caller identity; blank or absent values normalize to None."""

    aad_id: str | None = None
    display_name: str | None = None
    tenant_id: str | None = None


@dataclass(frozen=True)
class SessionStart:
    """Call context with the SDK's compatible constructor order and defaults."""

    call_id: str
    thread_id: str
    caller: Caller
    direction: str = "inbound"
    recording_status: str | None = None
    tenant_id: str | None = None


def parse_session_start(msg: dict[str, Any]) -> SessionStart:
    """Read call context; only callId lacks a safe default."""
    call_id = clean(msg.get("callId"))
    if not call_id:
        raise ValueError("session.start is missing callId")
    raw_caller = msg.get("caller")
    caller_obj = raw_caller if isinstance(raw_caller, dict) else {}
    direction = clean(msg.get("direction")) or "inbound"
    return SessionStart(
        call_id=call_id,
        thread_id=clean(msg.get("threadId")) or "",
        caller=Caller(
            aad_id=clean(caller_obj.get("aadId")),
            display_name=clean(caller_obj.get("displayName")),
            tenant_id=clean(caller_obj.get("tenantId")),
        ),
        direction=direction if direction in ["inbound", "outbound"] else "inbound",
        recording_status=clean(msg.get("recordingStatus")),
        tenant_id=clean(msg.get("tenantId")),
    )


def audio_frame(seq: int, timestamp_ms: int, pcm: bytes) -> str:
    """Build an outbound ``audio.frame`` JSON frame."""
    return encode(
        {
            "type": TYPE_AUDIO_FRAME,
            "seq": seq,
            "timestampMs": timestamp_ms,
            "payloadBase64": base64.b64encode(pcm).decode("ascii"),
        }
    )


def pong(ts: Any) -> str:
    """Build an outbound ``pong`` JSON frame."""
    return encode(
        {
            "type": TYPE_PONG,
            "ts": normalize_pong_timestamp(ts),
        }
    )


def assistant_cancel(turn_id: int) -> str:
    """Build an outbound ``assistant.cancel`` JSON frame."""
    return encode(
        {
            "type": TYPE_ASSISTANT_CANCEL,
            "turnId": turn_id,
        }
    )


def session_end(reason: str) -> str:
    """Build an outbound ``session.end`` JSON frame."""
    return encode(
        {
            "type": TYPE_SESSION_END,
            "reason": reason,
        }
    )
