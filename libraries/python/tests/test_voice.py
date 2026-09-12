# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Turn-taking for an agent that is not a realtime model.

Three pieces, and every bound in them exists because of a specific failure: an
utterance clipped at the front, a turn that ends on a thinking pause, a cough
sent for transcription, a television that never goes quiet, a WAV that plays as
noise, a barge-in that arrives after the bot has already said the rest.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from standin.audio import FRAME_MS
from standin.voice import (
    PacedPlayback,
    UtteranceSegmenter,
    decode_wav,
    encode_wav,
)

pytestmark = pytest.mark.unit

#: One 20 ms frame of each kind.
LOUD = (8000).to_bytes(2, "little", signed=True) * 320
QUIET = b"\x00\x00" * 320


def _segmenter(**kwargs) -> UtteranceSegmenter:
    defaults = {"silence_ms": 100, "min_utterance_ms": 40, "preroll_ms": 40}
    return UtteranceSegmenter(**{**defaults, **kwargs})


def _drive(segmenter: UtteranceSegmenter, frames: list[bytes]) -> list[bytes]:
    out = []
    for frame in frames:
        got = segmenter.feed(frame)
        if got is not None:
            out.append(got)
    return out


# -------------------------------------------------------------- segmentation


def test_one_utterance_comes_out_of_a_continuous_stream():
    segmenter = _segmenter()
    utterances = _drive(segmenter, [QUIET] * 3 + [LOUD] * 10 + [QUIET] * 6)
    assert len(utterances) == 1


def test_the_syllable_that_opened_the_gate_is_not_clipped():
    """Without pre-roll every utterance begins mid-consonant, and the transcript
    loses the first word of most sentences."""
    segmenter = _segmenter(preroll_ms=60)
    utterances = _drive(segmenter, [QUIET] * 5 + [LOUD] * 10 + [QUIET] * 6)
    # Three frames of pre-roll plus ten of speech plus the silence that ended it.
    assert len(utterances[0]) > 10 * len(LOUD)


def test_a_pause_inside_a_sentence_does_not_end_the_turn():
    """A caller thinking mid-sentence is not a caller who has finished."""
    segmenter = _segmenter(silence_ms=200)
    frames = [LOUD] * 5 + [QUIET] * 5 + [LOUD] * 5 + [QUIET] * 12
    utterances = _drive(segmenter, frames)
    assert len(utterances) == 1


def test_a_cough_is_not_a_turn():
    """Sending one costs a transcription request and returns nothing worth
    answering."""
    segmenter = _segmenter(min_utterance_ms=200)
    assert _drive(segmenter, [LOUD] * 2 + [QUIET] * 8) == []


def test_a_room_that_never_goes_quiet_is_cut_at_the_ceiling():
    """A stuck-open microphone or a television never trips a silence check, and
    without a cap one utterance grows for the whole call."""
    segmenter = _segmenter(max_utterance_ms=200)
    utterances = _drive(segmenter, [LOUD] * 40)
    assert utterances
    assert len(utterances[0]) <= (200 // FRAME_MS + 2) * len(LOUD)


def test_flush_takes_what_is_held_mid_utterance():
    segmenter = _segmenter()
    _drive(segmenter, [LOUD] * 5)
    assert segmenter.speaking is True
    assert segmenter.flush() is not None
    assert segmenter.speaking is False
    assert segmenter.flush() is None


def test_reset_abandons_the_turn():
    segmenter = _segmenter()
    _drive(segmenter, [LOUD] * 5)
    segmenter.reset()
    assert segmenter.speaking is False
    assert segmenter.flush() is None


def test_an_empty_frame_is_ignored():
    assert _segmenter().feed(b"") is None


# ---------------------------------------------------------------- the WAV


def _wav(
    fmt: int, channels: int, rate: int, bits: int, payload: bytes, extensible: bool = False
) -> bytes:
    body = struct.pack(
        "<HHIIHH",
        0xFFFE if extensible else fmt,
        channels,
        rate,
        rate * channels * bits // 8,
        channels * bits // 8,
        bits,
    )
    if extensible:
        # cbSize, valid bits, channel mask, then the sub-format GUID whose first
        # two bytes carry the real format tag.
        body += struct.pack("<HHI", 22, bits, 3) + struct.pack("<H", fmt) + b"\x00" * 14
    return (
        struct.pack("<4sI4s", b"RIFF", 36 + len(body) - 16 + len(payload), b"WAVE")
        + struct.pack("<4sI", b"fmt ", len(body))
        + body
        + struct.pack("<4sI", b"data", len(payload))
        + payload
    )


def test_a_wav_round_trips():
    pcm = b"\x01\x02" * 1600
    assert decode_wav(encode_wav(pcm)) == pcm


def test_a_float_wav_is_read_as_pcm16():
    """Several engines emit 32-bit float, which plays as noise unconverted."""
    payload = struct.pack("<4f", 0.0, 0.5, -0.5, 1.0)
    pcm = decode_wav(_wav(3, 1, 16000, 32, payload))
    assert len(pcm) == 8
    assert struct.unpack("<4h", pcm) == (0, 16383, -16383, 32767)


def test_a_float_outside_the_range_is_clamped_not_wrapped():
    """Wrapping is the loudest possible click, and a float WAV is allowed
    outside minus one to one."""
    payload = struct.pack("<2f", 4.0, -4.0)
    assert struct.unpack("<2h", decode_wav(_wav(3, 1, 16000, 32, payload))) == (32767, -32767)


def test_the_extensible_wrapper_is_unwrapped():
    """ffmpeg and several speech engines emit WAVE_FORMAT_EXTENSIBLE even for
    plain PCM, and reading the tag literally rejects a perfectly good file."""
    pcm = b"\x01\x02" * 100
    assert decode_wav(_wav(1, 1, 16000, 16, pcm, extensible=True)) == pcm


def test_stereo_is_averaged_not_halved():
    """Taking only the left channel loses whoever is on the right."""
    payload = struct.pack("<4h", 1000, 3000, -1000, -3000)
    assert struct.unpack("<2h", decode_wav(_wav(1, 2, 16000, 16, payload))) == (2000, -2000)


def test_an_eight_bit_wav_is_read_as_unsigned():
    """8-bit WAV is centred on 128. Read as signed it is a square wave of noise."""
    assert struct.unpack("<2h", decode_wav(_wav(1, 1, 16000, 8, bytes([128, 255])))) == (0, 32512)


def test_another_rate_is_resampled_to_the_call():
    payload = b"\x01\x02" * 4410
    pcm = decode_wav(_wav(1, 1, 44100, 16, payload))
    # 4410 samples at 44.1 kHz is 100 ms, which is 1600 samples at 16 kHz.
    assert abs(len(pcm) // 2 - 1600) <= 2


def test_chunks_before_the_data_are_walked_not_assumed():
    """A real encoder puts LIST and fact chunks first, and a fixed offset reads
    them as samples."""
    pcm = b"\x01\x02" * 100
    wav = bytearray(encode_wav(pcm))
    extra = struct.pack("<4sI", b"LIST", 4) + b"INFO"
    wav[12:12] = extra
    struct.pack_into("<I", wav, 4, len(wav) - 8)
    assert decode_wav(bytes(wav)) == pcm


@pytest.mark.parametrize(
    "data",
    [b"", b"not a wav at all", b"RIFF" + b"\x00" * 8],
)
def test_something_that_is_not_a_wav_is_refused(data: bytes):
    with pytest.raises(ValueError):
        decode_wav(data)


def test_a_format_this_cannot_read_says_so():
    with pytest.raises(ValueError, match="unsupported WAV format"):
        decode_wav(_wav(1, 1, 16000, 24, b"\x00" * 30))


# ------------------------------------------------------------- the playback


async def test_a_buffer_goes_out_one_wire_frame_at_a_time():
    sent: list[bytes] = []

    async def send(pcm: bytes) -> None:
        sent.append(pcm)

    # The default frame is the wire frame: 20 ms, 640 bytes. frame_ms scales
    # the frame SIZE with it, so a smaller one is a smaller frame, not a faster
    # clock.
    playback = PacedPlayback(send)
    result = await playback.say(QUIET * 5)
    assert len(sent) == 5
    assert all(len(frame) == 640 for frame in sent)
    assert result.complete and result.interrupted is False
    assert result.sent_ms == result.total_ms == 100


async def test_a_barge_in_stops_it_and_the_result_says_how_much_was_heard():
    """The difference between "I told them" and "I started to"."""
    sent: list[bytes] = []
    playback: PacedPlayback

    async def send(pcm: bytes) -> None:
        sent.append(pcm)
        if len(sent) == 2:
            playback.cancel()

    playback = PacedPlayback(send)
    result = await playback.say(QUIET * 10)
    assert result.interrupted is True
    assert result.complete is False
    assert 0 < result.sent_ms < result.total_ms


async def test_two_turns_do_not_interleave():
    """Left unserialised the caller hears both at once."""
    order: list[str] = []

    async def send(pcm: bytes) -> None:
        order.append(pcm[:1].hex())
        await asyncio.sleep(0)

    playback = PacedPlayback(send)
    first = asyncio.create_task(playback.say(b"\xaa\x00" * 320 * 3))
    second = asyncio.create_task(playback.say(b"\xbb\x00" * 320 * 3))
    await asyncio.gather(first, second)
    # Every frame of the first turn, then every frame of the second.
    assert order == ["aa"] * 3 + ["bb"] * 3


async def test_a_failed_turn_does_not_poison_the_next_one():
    calls = {"n": 0}

    async def send(pcm: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the socket went away")

    playback = PacedPlayback(send)
    with pytest.raises(RuntimeError):
        await playback.say(QUIET)
    assert (await playback.say(QUIET)).complete


async def test_nothing_to_say_is_not_an_error():
    async def send(pcm: bytes) -> None:  # pragma: no cover - never called
        raise AssertionError("nothing should be sent")

    result = await PacedPlayback(send).say(b"")
    assert result.total_ms == 0 and result.complete


async def test_the_frame_size_follows_the_frame_length():
    """frame_ms is the length of a frame, so it scales the frame's SIZE. A
    plugin whose provider hands back 10 ms chunks gets 320-byte frames."""
    sent: list[bytes] = []

    async def send(pcm: bytes) -> None:
        sent.append(pcm)

    await PacedPlayback(send, frame_ms=10).say(QUIET)
    assert [len(f) for f in sent] == [320, 320]


# ------------------------------------------------------------- both languages


def test_the_two_sdks_agree_on_the_wav_container(tmp_path):
    """Both halves hand-roll this container, and a WAV one writes that the other
    misreads is a call that plays noise. The proof is the other reader."""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    dist = Path(__file__).resolve().parents[2] / "typescript" / "dist" / "voice.js"
    if node is None or not dist.exists():
        pytest.skip("needs node and a built TypeScript half")

    pcm = bytes(range(256)) * 8
    out = tmp_path / "from-typescript.wav"
    script = (
        f"import {{ encodeWav, decodeWav }} from {json.dumps(dist.as_uri())};"
        f"import {{ writeFileSync, readFileSync }} from 'node:fs';"
        f"const pcm = Buffer.from({json.dumps(list(pcm))});"
        f"writeFileSync({json.dumps(str(out))}, encodeWav(pcm));"
        # and read back the one Python wrote
        f"const mine = decodeWav(readFileSync({json.dumps(str(tmp_path / 'from-python.wav'))}));"
        f"process.stdout.write(mine.equals(pcm) ? 'match' : 'differ');"
    )
    (tmp_path / "from-python.wav").write_bytes(encode_wav(pcm))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        check=True,
        timeout=60,
        capture_output=True,
        text=True,
    )

    # TypeScript read what Python wrote.
    assert result.stdout.strip() == "match"
    # And Python reads what TypeScript wrote.
    assert decode_wav(out.read_bytes()) == pcm


def test_a_click_is_not_an_utterance_but_a_short_word_is():
    """The floor judges the LOUD part. Measuring the whole buffer counts the
    pre-roll and the trailing silence, over a second at the defaults, so the
    floor could never fire and every click reached the transcriber."""
    from standin.voice import FRAME_MS, UtteranceSegmenter

    frame = 16_000 * FRAME_MS // 1000 * 2
    loud = b"\x00\x40" * (frame // 2)
    quiet = bytes(frame)

    def utter(loud_frames: int) -> bytes | None:
        segmenter = UtteranceSegmenter()
        for _ in range(loud_frames):
            segmenter.feed(loud)
        for _ in range(45):
            got = segmenter.feed(quiet)
            if got is not None:
                return got
        return None

    # One frame of noise: a door, a chair, a tap on the microphone.
    assert utter(1) is None
    # Two hundred milliseconds of voice: "yes". It must survive.
    assert utter(10) is not None
