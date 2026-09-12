# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The audio the wire carries, and the two helpers every plugin needs.

The StandIn wire is PCM 16 kHz, 16-bit, mono, little-endian in both directions.
Almost nothing else is. The realtime speech-to-speech models speak 24 kHz, most
TTS vendors emit 22.05 or 24 kHz, and none of them chunk on the wire's frame
boundary. So every plugin that is not a pure passthrough ends up writing
the same two things:

* a resampler, because the rates differ
* a frame aligner, because a resampled buffer does not divide evenly into the
  wire's 640-byte frame, and dropping the remainder clips the end of every turn

They live here rather than in each plugin because they are properties of the
WIRE, not of any framework - the same reason the sequence number and the
outbound timeline live in :class:`~.call_server.CallServer`. A plugin that has
to reimplement them is a plugin the SDK failed.

Dependency-free on purpose: speech at these rates does not need a windowed-sinc
filter, and the alternative is putting numpy or scipy on the critical path of
every audio frame of every call.
"""

from __future__ import annotations

import array
import math
import sys

from .protocol import SAMPLE_RATE_HZ

__all__ = [
    "BYTES_PER_SAMPLE",
    "FRAME_BYTES",
    "FRAME_MS",
    "REALTIME_SAMPLE_RATE_HZ",
    "FrameAligner",
    "frame_duration_ms",
    "pcm16_rms",
    "resample_pcm16",
]

#: 16-bit mono.
BYTES_PER_SAMPLE = 2

#: The nominal frame StandIn sends: 20 ms of PCM16 at 16 kHz = 320 samples.
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 640

#: What the realtime speech-to-speech models speak. Named here rather than in a
#: plugin because the RATIO is what forces the residual buffer below, and that
#: is an audio concern rather than a provider one.
REALTIME_SAMPLE_RATE_HZ = 24_000


def frame_duration_ms(pcm: bytes) -> float:
    """Duration of a PCM16 mono buffer in milliseconds.

    Use this for a playout clock rather than counting frames: outbound chunk
    lengths are NOT fixed, so a frame count drifts against real time.
    """
    return len(pcm) / (SAMPLE_RATE_HZ * BYTES_PER_SAMPLE) * 1000.0


def resample_pcm16(pcm: bytes, src_hz: int, dst_hz: int) -> bytes:
    """Linear-interpolation resample of PCM16 mono.

    An odd trailing byte is dropped rather than raising: a truncated frame is a
    glitch, but a raised exception in the audio path is a dropped call.

    Args:
        pcm: PCM16 mono, little-endian.
        src_hz: rate ``pcm`` is at.
        dst_hz: rate to produce. Equal rates return the input unchanged.
    """
    if src_hz == dst_hz or not pcm:
        return pcm
    if len(pcm) % 2:
        pcm = pcm[:-1]
    n_in = len(pcm) // 2
    if n_in == 0:
        return b""
    src = memoryview(pcm).cast("h")
    n_out = max(1, round(n_in * dst_hz / src_hz))
    step = n_in / n_out
    out = bytearray(n_out * 2)
    dst = memoryview(out).cast("h")
    for i in range(n_out):
        pos = i * step
        j = int(pos)
        if j >= n_in - 1:
            dst[i] = src[n_in - 1]
        else:
            frac = pos - j
            dst[i] = int(src[j] + (src[j + 1] - src[j]) * frac)
    return bytes(out)


class FrameAligner:
    """Chops arbitrary-length PCM buffers into whole wire frames, carrying the
    remainder.

    Resampled 24 kHz deltas do not divide evenly into the wire's 640-byte frame,
    so without a residual the leftover bytes are dropped and every turn loses a
    few milliseconds at the seams. Over a call that is audible as clipped word
    endings.

    Example:
        ```python
        aligner = FrameAligner()
        for chunk in provider_audio:            # arbitrary lengths
            for frame in aligner.push(chunk):   # whole 640-byte frames
                await session.send_audio(frame)
        tail = aligner.flush()                  # end of turn
        if tail is not None:
            await session.send_audio(tail)
        ```
    """

    def __init__(self, frame_bytes: int = FRAME_BYTES) -> None:
        self._frame_bytes = frame_bytes
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """Bytes held back, waiting for a whole frame."""
        return len(self._buf)

    def push(self, pcm: bytes) -> list[bytes]:
        """Add a buffer; return whatever whole frames are now available."""
        self._buf.extend(pcm)
        out: list[bytes] = []
        while len(self._buf) >= self._frame_bytes:
            out.append(bytes(self._buf[: self._frame_bytes]))
            del self._buf[: self._frame_bytes]
        return out

    def flush(self) -> bytes | None:
        """Zero-pad and return the residual at end of turn, or ``None`` when empty.

        Padding rather than dropping: the tail of the last word matters more
        than a few milliseconds of silence.
        """
        if not self._buf:
            return None
        tail = bytes(self._buf) + b"\x00" * (self._frame_bytes - len(self._buf))
        self._buf.clear()
        return tail

    def reset(self) -> None:
        """Drop the residual without emitting it - use on a barge-in, where the
        held-back bytes belong to a turn the caller just interrupted."""
        self._buf.clear()


def pcm16_rms(pcm: bytes) -> float:
    """Root-mean-square amplitude of PCM16 mono, normalised to 0.0 - 1.0.

    How loud a frame is, which is what an echo guard, a barge-in check and a
    voice segmenter each need. It lives here because all three want it and
    every plugin that wanted it had been writing it again.

    Dependency-free for the same reason the resampler above is: this runs on
    every inbound frame of every call, and numpy on that path buys microseconds
    at the cost of a wheel on every deployment.

    An odd trailing byte is dropped rather than raising. A truncated frame is a
    glitch; an exception in the audio path is a dropped call.
    """
    if len(pcm) < 2:
        return 0.0
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = array.array("h")
    samples.frombytes(pcm)
    if sys.byteorder == "big":
        samples.byteswap()
    acc = 0.0
    for sample in samples:
        value = sample / 32768.0
        acc += value * value
    return math.sqrt(acc / len(samples))
