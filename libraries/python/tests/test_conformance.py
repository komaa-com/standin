# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The Python half of the shared conformance suite.

Reads ``protocol/conformance.json`` - the same file the TypeScript SDK's
``conformance.test.ts`` reads - and asserts the same expectations. That is what
makes "the two SDKs are at parity" a property CI proves rather than a claim a
README makes.

Both generated protocols share one schema snapshot, and their behavior is
checked against one set of vectors. When you find a parity bug, add the case to the vectors first: one
language will fail, and that tells you which one to fix.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from standin._hmac import sign_handshake, verify_handshake
from standin.audio import FrameAligner, pcm16_rms, resample_pcm16
from standin.avatar import expression, speech_marks
from standin.protocol import (
    SAMPLE_RATE_HZ,
    assistant_cancel,
    audio_frame,
    decode_pcm,
    parse_message,
    parse_session_start,
    pong,
    session_end,
)
from standin.vision import display_frame, display_image, parse_video_frame

pytestmark = pytest.mark.unit

_VECTORS_PATH = Path(__file__).resolve().parents[3] / "protocol" / "conformance.json"
VECTORS: dict[str, Any] = json.loads(_VECTORS_PATH.read_text())


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c.get("name", str(i)) for i, c in enumerate(cases)]


# ----------------------------------------------------------------------- hmac


@pytest.mark.parametrize("case", VECTORS["hmac"]["sign"], ids=_ids(VECTORS["hmac"]["sign"]))
def test_hmac_signature_matches_vector(case: dict[str, Any]) -> None:
    assert sign_handshake(case["secret"], case["timestampMs"], case["id"]) == case["expected"]


@pytest.mark.parametrize(
    "case", VECTORS["hmac"]["verifyRejects"], ids=_ids(VECTORS["hmac"]["verifyRejects"])
)
def test_hmac_verify_rejects(case: dict[str, Any]) -> None:
    # current_ms pinned to the vector's timestamp so freshness never decides
    # these cases - each one is about the input being malformed, not stale.
    try:
        current = int(case["timestamp"])
    except (TypeError, ValueError):
        current = 1735689600000
    assert (
        verify_handshake(
            case["secret"], case["timestamp"], case["id"], case["signature"], current_ms=current
        )
        is False
    )


def test_hmac_roundtrip_verifies() -> None:
    case = VECTORS["hmac"]["sign"][0]
    sig = sign_handshake(case["secret"], case["timestampMs"], case["id"])
    assert verify_handshake(
        case["secret"], case["timestampMs"], case["id"], sig, current_ms=int(case["timestampMs"])
    )


# --------------------------------------------------------------- sessionStart


@pytest.mark.parametrize(
    "case", VECTORS["sessionStart"]["accepts"], ids=_ids(VECTORS["sessionStart"]["accepts"])
)
def test_session_start_parses(case: dict[str, Any]) -> None:
    got = parse_session_start(case["input"])
    want = case["expected"]
    assert got.call_id == want["callId"]
    assert got.thread_id == want["threadId"]
    assert got.direction == want["direction"]
    assert got.recording_status == want["recordingStatus"]
    assert got.tenant_id == want["tenantId"]
    assert got.caller.aad_id == want["caller"]["aadId"]
    assert got.caller.display_name == want["caller"]["displayName"]
    assert got.caller.tenant_id == want["caller"]["tenantId"]


@pytest.mark.parametrize(
    "case", VECTORS["sessionStart"]["rejects"], ids=_ids(VECTORS["sessionStart"]["rejects"])
)
def test_session_start_rejects(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_session_start(case["input"])


# --------------------------------------------------------------- parseMessage


@pytest.mark.parametrize(
    "case", VECTORS["parseMessage"]["dropped"], ids=_ids(VECTORS["parseMessage"]["dropped"])
)
def test_parse_message_drops(case: dict[str, Any]) -> None:
    assert parse_message(case["input"]) is None


@pytest.mark.parametrize(
    "case", VECTORS["parseMessage"]["accepted"], ids=_ids(VECTORS["parseMessage"]["accepted"])
)
def test_parse_message_accepts(case: dict[str, Any]) -> None:
    frame = parse_message(case["input"])
    assert frame is not None
    assert frame["type"] == case["expectedType"]


@pytest.mark.parametrize(
    "case",
    VECTORS["parseMessage"]["bytesDropped"],
    ids=_ids(VECTORS["parseMessage"]["bytesDropped"]),
)
def test_parse_message_drops_invalid_wire_encoding(case: dict[str, Any]) -> None:
    assert parse_message(base64.b64decode(case["inputBase64"])) is None


# ------------------------------------------------------------------ decodePcm


@pytest.mark.parametrize(
    "case", VECTORS["decodePcm"]["accepts"], ids=_ids(VECTORS["decodePcm"]["accepts"])
)
def test_decode_pcm_accepts(case: dict[str, Any]) -> None:
    assert len(decode_pcm(case["payloadBase64"])) == case["expectedBytes"]


@pytest.mark.parametrize(
    "case", VECTORS["decodePcm"]["rejects"], ids=_ids(VECTORS["decodePcm"]["rejects"])
)
def test_decode_pcm_rejects(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        decode_pcm(case["payloadBase64"])


# -------------------------------------------------------------- audioTimeline


def test_audio_timeline_is_integer_division() -> None:
    """The server owns seq and timestampMs. A float in either SDK drifts the two
    timelines apart over a long call."""
    assert VECTORS["audioTimeline"]["sampleRateHz"] == SAMPLE_RATE_HZ
    seq = 0
    sent_ms = 0
    for step in VECTORS["audioTimeline"]["steps"]:
        pcm = b"\x00" * step["pcmBytes"]
        seq += 1
        frame = json.loads(audio_frame(seq, sent_ms, pcm))
        assert frame["seq"] == step["expectedSeq"], step
        assert frame["timestampMs"] == step["expectedTimestampMs"], step
        sent_ms += (len(pcm) // 2) * 1000 // SAMPLE_RATE_HZ


# ----------------------------------------------------------- contextSentences


def _participants(count: int) -> str:
    if count <= 1:
        return "This is a 1:1 call with a single human caller."
    return (
        f"There are {count} human participants on this call. Stay quiet unless directly addressed."
    )


@pytest.mark.parametrize("case", VECTORS["contextSentences"]["participants"])
def test_participants_sentence(case: dict[str, Any]) -> None:
    assert _participants(case["count"]) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["contextSentences"]["dtmf"])
def test_dtmf_sentence(case: dict[str, Any]) -> None:
    assert f'The caller pressed the "{case["digit"]}" key on their keypad.' == case["expected"]


@pytest.mark.parametrize("case", VECTORS["contextSentences"]["recording"])
def test_recording_sentence(case: dict[str, Any]) -> None:
    sentence = (
        "The Microsoft Teams call recording is now ACTIVE."
        if case["status"] == "active"
        else "The Microsoft Teams call recording is not active."
    )
    assert sentence == case["expected"]


# ------------------------------------------------------------- outboundFrames


@pytest.mark.parametrize("case", VECTORS["outboundFrames"]["pong"])
def test_pong_frame(case: dict[str, Any]) -> None:
    assert json.loads(pong(case["input"])) == case["expected"]


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -float("inf")])
def test_pong_rejects_nonfinite_timestamps(timestamp: float) -> None:
    assert json.loads(pong(timestamp)) == {"type": "pong", "ts": 0}


@pytest.mark.parametrize("case", VECTORS["outboundFrames"]["sessionEnd"])
def test_session_end_frame(case: dict[str, Any]) -> None:
    assert json.loads(session_end(case["input"])) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["outboundFrames"]["assistantCancel"])
def test_assistant_cancel_frame(case: dict[str, Any]) -> None:
    assert json.loads(assistant_cancel(case["input"])) == case["expected"]


# ----------------------------------------------------------------------- audio


@pytest.mark.parametrize(
    "case", VECTORS["audio"]["resample"], ids=_ids(VECTORS["audio"]["resample"])
)
def test_resample_matches_vector(case: dict[str, Any]) -> None:
    """Both SDKs must resample identically, or the same agent sounds different
    depending on which language its plugin happens to be written in."""
    got = resample_pcm16(base64.b64decode(case["inputBase64"]), case["srcHz"], case["dstHz"])
    assert len(got) == case["expectedBytes"]
    assert base64.b64encode(got).decode() == case["expectedBase64"]


@pytest.mark.parametrize(
    "case", VECTORS["audio"]["frameAligner"], ids=_ids(VECTORS["audio"]["frameAligner"])
)
def test_frame_aligner_matches_vector(case: dict[str, Any]) -> None:
    """A resampled chunk does not divide evenly into the wire frame; dropping
    the remainder clips the end of every turn."""
    aligner = FrameAligner(case["frameBytes"])
    for push in case["pushes"]:
        frames = aligner.push(b"\x00" * push["inputBytes"])
        assert len(frames) == push["framesOut"], push
        assert all(len(f) == case["frameBytes"] for f in frames)
        assert aligner.pending == push["pendingAfter"], push
    tail = aligner.flush()
    if case["flushBytes"] is None:
        assert tail is None
    else:
        assert tail is not None and len(tail) == case["flushBytes"]
    assert aligner.flush() is None  # idempotent


# ------------------------------------------------------------------------ chat


@pytest.mark.parametrize(
    "case",
    VECTORS["chat"]["parseInbound"]["accepts"],
    ids=_ids(VECTORS["chat"]["parseInbound"]["accepts"]),
)
def test_chat_parse_inbound_accepts(case: dict[str, Any]) -> None:
    from standin.chat import parse_inbound

    m = parse_inbound(case["body"])
    w = case["expected"]
    assert m.tenant_id == w["tenantId"]
    assert m.conversation_id == w["conversationId"]
    assert m.activity_id == w["activityId"]
    assert m.scope == w["scope"]
    assert m.text == w["text"]
    assert m.sender_name == w["senderName"]
    assert m.sender_aad_id == w["senderAadId"]
    assert m.sender_is_guest == w["senderIsGuest"]
    assert m.sender_is_linked_owner == w["senderIsLinkedOwner"]
    assert m.attachments == w["attachments"]
    assert m.mentions == w["mentions"]
    assert m.locale == w["locale"]
    assert m.card_action == w["cardAction"]
    assert m.is_personal == w["isPersonal"]


@pytest.mark.parametrize(
    "case",
    VECTORS["chat"]["parseInbound"]["rejects"],
    ids=_ids(VECTORS["chat"]["parseInbound"]["rejects"]),
)
def test_chat_parse_inbound_rejects(case: dict[str, Any]) -> None:
    from standin.chat import parse_inbound

    with pytest.raises(ValueError):
        parse_inbound(case["body"])


@pytest.mark.parametrize(
    "case", VECTORS["chat"]["buildReply"], ids=_ids(VECTORS["chat"]["buildReply"])
)
def test_chat_build_reply(case: dict[str, Any]) -> None:
    """tenantId and conversationId echo the inbound EXACTLY: that check is the
    cross-tenant leak guard the whole relay rests on, so a divergence between the
    two SDKs here is a security bug."""
    from standin.chat import build_reply, parse_inbound

    inbound = parse_inbound(VECTORS["chat"]["parseInbound"]["accepts"][0]["body"])
    assert build_reply(inbound, "the answer", case["kind"]) == case["expected"]


# ---------------------------------------------------------------------- vision


@pytest.mark.parametrize(
    "case", VECTORS["video"]["parseAccepts"], ids=_ids(VECTORS["video"]["parseAccepts"])
)
def test_video_frame_parses(case: dict[str, Any]) -> None:
    frame = parse_video_frame(case["input"])
    assert frame is not None
    expected = case["expected"]
    assert frame.source == expected["source"]
    assert frame.ts == expected["ts"]
    assert frame.width == expected["width"]
    assert frame.height == expected["height"]
    assert frame.mime == expected["mime"]
    assert frame.data_base64 == expected["dataBase64"]
    assert frame.participant_id == expected["participantId"]
    assert frame.participant_name == expected["participantName"]
    # The two forms a provider asks for: raw bytes to upload, a data URL to
    # paste into a vision request.
    assert frame.data == base64.b64decode(expected["dataBase64"])
    assert frame.data_url == f"data:{expected['mime']};base64,{expected['dataBase64']}"


@pytest.mark.parametrize(
    "case", VECTORS["video"]["parseRejects"], ids=_ids(VECTORS["video"]["parseRejects"])
)
def test_video_frame_drops_unusable(case: dict[str, Any]) -> None:
    """Dropped, never raised: one malformed image must not end a live call."""
    assert parse_video_frame(case["input"]) is None


@pytest.mark.parametrize(
    "case", VECTORS["video"]["displayImage"], ids=_ids(VECTORS["video"]["displayImage"])
)
def test_display_image_frame(case: dict[str, Any]) -> None:
    built = display_image(
        case["dataBase64"],
        case.get("mime", "image/jpeg"),
        case.get("durationMs"),
        case.get("mode"),
        case.get("caption"),
    )
    assert json.loads(built) == case["expected"]


@pytest.mark.parametrize(
    "case", VECTORS["video"]["displayFrame"], ids=_ids(VECTORS["video"]["displayFrame"])
)
def test_display_frame_frame(case: dict[str, Any]) -> None:
    built = display_frame(
        case["seq"],
        case["ts"],
        case["dataBase64"],
        case.get("mime", "image/jpeg"),
        case.get("width"),
        case.get("height"),
    )
    assert json.loads(built) == case["expected"]


def test_display_image_accepts_raw_bytes_and_base64_identically() -> None:
    """bytes and base64 are two spellings of one image, not two behaviours."""
    raw = b"\xff\xd8\xff\xe0"
    assert display_image(raw) == display_image(base64.b64encode(raw).decode())


@pytest.mark.parametrize(
    ("image", "mime", "reason"),
    [
        (b"", "image/jpeg", "carries no image data"),
        (b"\xff\xd8", "image/gif", "mime must be one of"),
        ("not base64!", "image/jpeg", "not valid base64"),
    ],
)
def test_display_image_refuses_what_the_service_would_reject(
    image: bytes | str, mime: str, reason: str
) -> None:
    """Refuse here, where the error names the problem, rather than letting the
    service close the socket in the middle of a call."""
    with pytest.raises(ValueError, match=reason):
        display_image(image, mime)


def test_display_image_refuses_an_oversized_image() -> None:
    from standin.vision import MAX_IMAGE_BYTES

    with pytest.raises(ValueError, match="over the"):
        display_image(b"\x00" * (MAX_IMAGE_BYTES + 1))


# ---------------------------------------------------------------------- avatar


@pytest.mark.parametrize(
    "case", VECTORS["avatar"]["expression"], ids=_ids(VECTORS["avatar"]["expression"])
)
def test_expression_frame(case: dict[str, Any]) -> None:
    assert json.loads(expression(case["input"])) == case["expected"]


@pytest.mark.parametrize(
    "case",
    VECTORS["avatar"]["expressionRejects"],
    ids=_ids(VECTORS["avatar"]["expressionRejects"]),
)
def test_expression_needs_an_emotion(case: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        expression(case["input"])


@pytest.mark.parametrize(
    "case", VECTORS["avatar"]["speechMarks"], ids=_ids(VECTORS["avatar"]["speechMarks"])
)
def test_speech_marks_frame(case: dict[str, Any]) -> None:
    """One bad mark would desynchronise the mouth for the rest of the utterance,
    so the builder drops it rather than sending it."""
    marks = [(m["tMs"], m["visemeId"]) for m in case["input"]]
    assert json.loads(speech_marks(marks)) == case["expected"]


@pytest.mark.parametrize(
    "case", VECTORS["audio"]["pcm16Rms"]["cases"], ids=_ids(VECTORS["audio"]["pcm16Rms"]["cases"])
)
def test_pcm16_rms_matches_vector(case: dict[str, Any]) -> None:
    """Both SDKs must read the same loudness off the same frame, or a barge-in
    threshold tuned in one language misfires in the other."""
    got = pcm16_rms(base64.b64decode(case["pcmBase64"]))
    assert got == pytest.approx(case["expected"], abs=1e-9)
