# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Turn-taking for an agent that is not a realtime model.

A realtime speech-to-speech provider is handed the caller's audio and hands
audio back, and the turn-taking is theirs. Everything else is not like that. A
transcription service wants one utterance at a time, a language model wants text,
and a text-to-speech engine hands back a whole buffer that somebody has to feed
out at the rate a call consumes it.

That shape is the same whichever three services you pick, and four separate
bridges each wrote it before this module existed. Three pieces:

:class:`UtteranceSegmenter`
    Fifty frames a second in, one utterance per spoken phrase out.

:func:`decode_wav` and :func:`encode_wav`
    A speech engine hands you a WAV, and it is rarely the WAV you wanted.

:class:`PacedPlayback`
    A finished buffer out at the rate the call consumes it, interruptibly.

None of this is needed by a realtime plugin, and none of it costs one anything:
it is a module you do not import.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .audio import FRAME_BYTES, FRAME_MS, pcm16_rms, resample_pcm16
from .log import logger
from .protocol import SAMPLE_RATE_HZ

__all__ = [
    "DEFAULT_MAX_UTTERANCE_MS",
    "DEFAULT_MIN_UTTERANCE_MS",
    "DEFAULT_PREROLL_MS",
    "DEFAULT_SILENCE_MS",
    "DEFAULT_SPEECH_RMS",
    "PacedPlayback",
    "Playback",
    "UtteranceSegmenter",
    "decode_wav",
    "encode_wav",
]

#: Loudness at which a frame counts as speech rather than room noise. The same
#: scale :func:`~standin.audio.pcm16_rms` returns, 0.0 to 1.0.
DEFAULT_SPEECH_RMS = 0.02

#: How much quiet ends an utterance. Short enough that the agent does not feel
#: slow, long enough to survive the pause in the middle of a sentence.
DEFAULT_SILENCE_MS = 800

#: Audio kept from BEFORE the gate opened. The syllable that trips the gate is
#: part of the word, and without this every utterance starts clipped.
DEFAULT_PREROLL_MS = 240

#: Below this much VOICED audio an utterance is a cough, a door, a chair.
#: Transcribing one costs a request and returns nothing worth answering.
#:
#: Measured on the loud part alone, from the frame that opened the gate to the
#: last loud frame. Measuring the whole buffer instead would count the pre-roll
#: and the trailing silence, which together are over a second at the defaults,
#: so the floor could never fire and every click reached the transcriber.
#:
#: Low on purpose. A single short word is 150 ms of voice and must survive;
#: a click is a frame or two.
DEFAULT_MIN_UTTERANCE_MS = 120

#: A hard ceiling, so a stuck-open microphone or a television in the room does
#: not grow one utterance for the length of the call.
DEFAULT_MAX_UTTERANCE_MS = 30_000


class UtteranceSegmenter:
    """One utterance per spoken phrase, out of a continuous stream.

    A Microsoft Teams call delivers audio continuously: silence is still frames,
    fifty a second. A transcription service wants a phrase. This is the gate
    between them.

    Feed it every frame and take what comes back::

        for frame in caller_audio:
            utterance = segmenter.feed(frame)
            if utterance is not None:
                text = await transcribe(utterance)

    Four bounds, and each exists because of a specific failure:

    **Pre-roll**, because the syllable that trips the gate is part of the word.
    Without it every utterance begins mid-consonant and the transcript loses the
    first word of most sentences.

    **Trailing silence**, because a pause inside a sentence is not the end of
    one. Too short and the agent interrupts a thinking caller; too long and it
    feels slow.

    **A floor**, because a cough is not a turn. Sending one costs a request and
    returns nothing worth answering.

    **A ceiling**, because a stuck-open microphone or a television in the room
    never goes quiet, and without a cap one utterance grows for the whole call.
    """

    def __init__(
        self,
        speech_rms: float = DEFAULT_SPEECH_RMS,
        silence_ms: int = DEFAULT_SILENCE_MS,
        preroll_ms: int = DEFAULT_PREROLL_MS,
        min_utterance_ms: int = DEFAULT_MIN_UTTERANCE_MS,
        max_utterance_ms: int = DEFAULT_MAX_UTTERANCE_MS,
    ) -> None:
        self.speech_rms = speech_rms
        self.silence_ms = silence_ms
        self.min_utterance_ms = min_utterance_ms
        self.max_utterance_ms = max_utterance_ms
        # A ring, not a list: it holds pre-speech audio on every frame of a
        # silent call, which is most of them.
        frames = max(1, preroll_ms // FRAME_MS)
        self._preroll: deque[bytes] = deque(maxlen=frames)
        self._speech: list[bytes] = []
        self._quiet_ms = 0
        self._speaking = False
        #: Frames since the gate opened, and the last of them that was loud.
        #: The gap between the two is trailing silence, which is not speech.
        self._since_open = 0
        self._last_loud = 0

    @property
    def speaking(self) -> bool:
        """Whether the caller is mid-utterance right now."""
        return self._speaking

    def feed(self, pcm: bytes) -> bytes | None:
        """Take one frame. Returns a finished utterance, or ``None``.

        Never raises and never blocks: it runs on the receive path of a live
        call, once per frame.
        """
        if not pcm:
            return None
        loud = pcm16_rms(pcm) >= self.speech_rms

        if not self._speaking:
            if not loud:
                self._preroll.append(pcm)
                return None
            # Opening: the ring is what the caller actually started saying.
            self._speech = [*self._preroll, pcm]
            self._preroll.clear()
            self._speaking = True
            self._quiet_ms = 0
            self._since_open = 1
            self._last_loud = 1
            return None

        self._speech.append(pcm)
        self._since_open += 1
        if loud:
            self._last_loud = self._since_open
        self._quiet_ms = 0 if loud else self._quiet_ms + FRAME_MS

        if self._quiet_ms >= self.silence_ms:
            return self._finish()
        if self._duration_ms() >= self.max_utterance_ms:
            logger.info("standin: cutting an utterance at its %d ms ceiling", self.max_utterance_ms)
            return self._finish(force=True)
        return None

    def flush(self) -> bytes | None:
        """Take whatever is held, mid-utterance. For teardown, or a barge-in."""
        return self._finish() if self._speaking else None

    def reset(self) -> None:
        """Forget everything. The caller interrupted, or the turn is abandoned."""
        self._speech.clear()
        self._preroll.clear()
        self._quiet_ms = 0
        self._speaking = False
        self._since_open = 0
        self._last_loud = 0

    def _duration_ms(self) -> int:
        """Everything held, pre-roll and trailing silence included.

        The right measure for the ceiling: a stuck-open microphone is filling
        memory whether or not anybody is talking into it.
        """
        return len(self._speech) * FRAME_MS

    def _voiced_ms(self) -> int:
        """The loud part alone, which is what a floor has to judge."""
        return self._last_loud * FRAME_MS

    def _finish(self, force: bool = False) -> bytes | None:
        audio = b"".join(self._speech)
        voiced = self._voiced_ms()
        self.reset()
        if not force and voiced < self.min_utterance_ms:
            return None
        return audio or None


# ------------------------------------------------------------------- the WAV

_RIFF = b"RIFF"
_WAVE = b"WAVE"
_FMT = b"fmt "
_DATA = b"data"

#: Plain integer PCM.
_FORMAT_PCM = 1
#: IEEE float, which several engines emit at 32 bits.
_FORMAT_FLOAT = 3
#: The wrapper ffmpeg and several speech engines emit even for plain PCM. The
#: real format is a GUID in the extension, whose first two bytes are the tag
#: above, so reading those two bytes is enough.
_FORMAT_EXTENSIBLE = 0xFFFE


def decode_wav(data: bytes) -> bytes:
    """Read a WAV and return PCM16 mono at the call's rate.

    A speech engine hands you a WAV and it is rarely the one you wanted:
    32-bit float, 44.1 kHz, stereo, or wrapped in ``WAVE_FORMAT_EXTENSIBLE``,
    which ffmpeg emits even for plain PCM. Every one of those plays as noise if
    you put it on a call unconverted, and the failure is not obvious from the
    header.

    Chunks are walked rather than assumed at a fixed offset, because a real
    encoder puts ``LIST`` and ``fact`` chunks before the data and a fixed offset
    reads them as samples.

    Raises :class:`ValueError` on anything that is not a WAV this can read.
    """
    if len(data) < 12 or data[0:4] != _RIFF or data[8:12] != _WAVE:
        raise ValueError("not a RIFF/WAVE file")

    audio_format = channels = bits = 0
    rate = 0
    payload = b""
    at = 12
    while at + 8 <= len(data):
        chunk_id = data[at : at + 4]
        size = struct.unpack_from("<I", data, at + 4)[0]
        body = data[at + 8 : at + 8 + size]
        if chunk_id == _FMT and len(body) >= 16:
            audio_format, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", body, 0)
            if audio_format == _FORMAT_EXTENSIBLE and len(body) >= 26:
                # The real format is the first two bytes of the sub-format GUID.
                audio_format = struct.unpack_from("<H", body, 24)[0]
        elif chunk_id == _DATA:
            payload = body
        # Chunks are word-aligned, and an odd size carries a pad byte.
        at += 8 + size + (size & 1)

    if not channels or not rate:
        raise ValueError("the WAV has no readable fmt chunk")
    if not payload:
        raise ValueError("the WAV has no data chunk")

    if audio_format == _FORMAT_FLOAT and bits == 32:
        samples = _float32_to_pcm16(payload)
    elif audio_format == _FORMAT_PCM and bits == 16:
        samples = payload
    elif audio_format == _FORMAT_PCM and bits == 8:
        # 8-bit WAV is UNSIGNED, centred on 128. Read as signed it is a square
        # wave of noise.
        samples = b"".join(struct.pack("<h", (byte - 128) * 256) for byte in payload)
    else:
        raise ValueError(f"unsupported WAV format {audio_format} at {bits} bits")

    if channels > 1:
        samples = _downmix(samples, channels)
    if rate != SAMPLE_RATE_HZ:
        samples = resample_pcm16(samples, rate, SAMPLE_RATE_HZ)
    return samples


def encode_wav(pcm: bytes, sample_rate_hz: int = SAMPLE_RATE_HZ) -> bytes:
    """Wrap PCM16 mono in a WAV, for a service that will not take raw PCM."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        _RIFF,
        36 + len(pcm),
        _WAVE,
        _FMT,
        16,
        _FORMAT_PCM,
        1,
        sample_rate_hz,
        sample_rate_hz * 2,
        2,
        16,
        _DATA,
        len(pcm),
    )
    return header + pcm


def _float32_to_pcm16(payload: bytes) -> bytes:
    count = len(payload) // 4
    out = bytearray(count * 2)
    for index in range(count):
        value = struct.unpack_from("<f", payload, index * 4)[0]
        # Clamped, because a float WAV is allowed outside -1..1 and wrapping
        # there is the loudest possible click.
        clamped = -1.0 if value < -1.0 else (1.0 if value > 1.0 else value)
        struct.pack_into("<h", out, index * 2, int(clamped * 32767))
    return bytes(out)


def _downmix(pcm: bytes, channels: int) -> bytes:
    """Average the channels. Taking only the left loses whoever is on the right."""
    frame = channels * 2
    count = len(pcm) // frame
    out = bytearray(count * 2)
    for index in range(count):
        total = 0
        base = index * frame
        for channel in range(channels):
            total += struct.unpack_from("<h", pcm, base + channel * 2)[0]
        struct.pack_into("<h", out, index * 2, int(total / channels))
    return bytes(out)


# -------------------------------------------------------------- the playback


class Playback:
    """What happened to one buffer handed to :class:`PacedPlayback`."""

    __slots__ = ("interrupted", "sent_ms", "total_ms")

    def __init__(self, sent_ms: int, total_ms: int, interrupted: bool) -> None:
        self.sent_ms = sent_ms
        """How much actually reached the caller."""
        self.total_ms = total_ms
        """How much there was."""
        self.interrupted = interrupted
        """Whether it was cut short."""

    @property
    def complete(self) -> bool:
        return not self.interrupted

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        state = "interrupted" if self.interrupted else "complete"
        return f"<Playback {self.sent_ms}/{self.total_ms} ms {state}>"


#: Hand one wire frame to the call. Normally ``session.send_audio``.
FrameSink = Callable[[bytes], Awaitable[None]]


class PacedPlayback:
    """Feed a finished buffer out at the rate a call consumes it.

    A text-to-speech engine returns a whole utterance at once. A call takes 20
    milliseconds every 20 milliseconds. Sending the buffer in one go hands the
    service seconds of audio it must queue, and the queue is what makes a
    barge-in arrive too late to matter: the caller interrupts, the model stops,
    and the bot keeps talking for the length of what was already sent.

    So it goes out paced, and the pacing is on an ABSOLUTE clock. Sleeping 20 ms
    per frame accumulates every scheduling delay, and a minute of speech ends
    seconds behind where it should be; this one sleeps until the next frame is
    DUE, so a late frame is followed by a short sleep rather than a full one.

    One at a time, in order::

        playback = PacedPlayback(session.send_audio)
        result = await playback.say(pcm)
        if result.interrupted:
            ...

    :meth:`cancel` stops whatever is playing. The result says how much was
    heard, which is the difference between "I told them" and "I started to".
    """

    def __init__(self, send: FrameSink, frame_ms: int = FRAME_MS) -> None:
        self._send = send
        self._frame_ms = max(1, frame_ms)
        self._frame_bytes = FRAME_BYTES * self._frame_ms // FRAME_MS
        self._cancelled = False
        self._playing = False
        # Serialised, so two turns cannot interleave into one stream of audio
        # the caller hears as both at once.
        self._lock = asyncio.Lock()

    @property
    def playing(self) -> bool:
        return self._playing

    def cancel(self) -> None:
        """Stop what is playing. Safe from anywhere, including a receive loop."""
        self._cancelled = True

    async def say(self, pcm: bytes) -> Playback:
        """Play one buffer and return what the caller actually heard."""
        total_ms = len(pcm) // 2 * 1000 // SAMPLE_RATE_HZ
        if not pcm:
            return Playback(0, 0, interrupted=False)

        async with self._lock:
            self._cancelled = False
            self._playing = True
            sent = 0
            # The clock is absolute. Sleeping a fixed step per frame accumulates
            # every delay, and a long utterance finishes seconds late.
            due = time.monotonic()
            try:
                for at in range(0, len(pcm), self._frame_bytes):
                    if self._cancelled:
                        break
                    frame = pcm[at : at + self._frame_bytes]
                    await self._send(frame)
                    sent += len(frame)
                    due += self._frame_ms / 1000
                    delay = due - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
            finally:
                self._playing = False
                interrupted = self._cancelled
                self._cancelled = False

        return Playback(sent // 2 * 1000 // SAMPLE_RATE_HZ, total_ms, interrupted=interrupted)
