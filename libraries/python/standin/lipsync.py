# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Lip-sync: the viseme timeline that makes the avatar's mouth match the words.

A realtime speech-to-speech model streams voice and hands back no phoneme
timings, so :func:`~standin.avatar.speech_marks` has nothing to carry and the
mouth never moves on the default path. This module closes that gap the only
honest way open to a worker: walk the spoken text, turn each character into a
mouth shape, and spread those shapes over the duration of the audio the worker
ACTUALLY SENT for that turn.

Spreading over sent audio is the whole reason the estimate is worth sending. The
shapes are approximate either way, but the timeline is pinned to a length the
worker measured, byte by byte, as the frames went out: the mouth opens when the
voice starts and closes when it stops, on a long sentence and a short one alike.
:class:`TurnLipSync` is that counter, and it is the piece to reach for first.

A duration GUESSED from text length or a words-per-minute rate is a different
proposition, and it is still worse than sending nothing. Its error compounds
sentence after sentence, so the mouth drifts further from the voice the longer
the call runs, and a mouth moving against the voice reads as broken in a way a
still mouth never does. Measured beats still; guessed loses to still.

Better again, when the speech provider hands back per-character timings, is
:func:`visemes_from_alignment`: same table, real times, no estimate at all.

The table covers Latin and Arabic in one map. Without the Arabic rows an Arabic
reply produces no tokens, carries no timeline, and the mouth simply does not
move for half the people who will be on these calls.

Timing anchor: ``t_ms`` counts from the START of that turn's audio, never from
the moment the message reaches the service. Where the duration is known before
playback (a text-to-speech path) send the timeline ahead of the first audio
frame. On a realtime path it is known only once the turn ends, so the marks
necessarily go out after the last chunk was handed over, and that is correct
only while the service still holds the turn's audio buffered for playout.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from .audio import BYTES_PER_SAMPLE
from .avatar import SpeechMark
from .protocol import SAMPLE_RATE_HZ

__all__ = [
    "CHAR_VISEMES",
    "SILENCE_VISEME",
    "TurnLipSync",
    "estimate_visemes",
    "viseme_for_char",
    "visemes_from_alignment",
]

#: A closed mouth, and what the space between two words becomes. Every other
#: unmapped character is skipped instead: punching silence into '3.5%' would
#: close the mouth in the middle of a spoken number.
SILENCE_VISEME = 0

#: A viseme id no character can carry, so the first token of a turn always
#: counts as a change. Starting the run collapser at 0 instead would swallow a
#: leading silence mark, which is what anchors the mouth shut before the first
#: vowel.
_NO_VISEME = -1

# The other SDK's \s, spelled out. Python's own shorthand also takes the C1
# controls and does not take U+FEFF, so left as \s the two SDKs would read a
# different number of tokens out of the same sentence and place the marks on
# different milliseconds. The trim that follows a substitution is spelled ' '
# for the same reason: by then every run this class matched is one space, while
# a bare strip() would go on to eat a C1 control the other SDK keeps.
_WHITESPACE_RUN = re.compile(
    r"[\t\n\x0b\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+"
)

# Mouth shape -> every character that wears it, in the avatar lane's 0 to 21
# numbering. All 26 Latin letters and all 28 Arabic letters are here on purpose:
# one common letter left out thins the timeline unevenly and the mouth stalls on
# that syllable. The Arabic rows carry the eight variant forms too.
_SHAPES: tuple[tuple[int, str], ...] = (
    (2, "a"),
    (2, "اأإآىة"),  # alef, with hamza above and below, madda, maqsura, teh marbuta
    (2, "َ"),  # fatha
    (4, "e"),
    (6, "iy"),
    (6, "يئ"),  # yeh, yeh with hamza
    (6, "ِ"),  # kasra
    (7, "uw"),
    (7, "و"),  # waw
    (7, "ُ"),  # damma
    (8, "o"),
    (12, "h"),
    (12, "هحعءؤ"),  # heh, hah, ain, hamza, waw with hamza
    (13, "r"),
    (13, "ر"),  # reh
    (14, "l"),
    (14, "ل"),  # lam
    (15, "szx"),
    (15, "سصز"),  # seen, sad, zain
    (16, "j"),
    (16, "شج"),  # sheen, jeem
    (18, "fv"),
    (18, "ف"),  # feh
    (19, "tdn"),
    (19, "تدنطضثذظ"),  # teh, dal, noon, tah, dad, theh, thal, zah
    (20, "kgcq"),
    (20, "كقغخ"),  # kaf, qaf, ghain, khah
    (21, "mbp"),
    (21, "مب"),  # meem, beh
)

#: Character to mouth shape, read-only. Sukun, shadda, tanween, the tatweel and
#: every presentation form are deliberately absent: a stretch mark and a doubling
#: mark carry no mouth shape of their own, and mapping them would insert phantom
#: mouth changes into an otherwise correct timeline.
CHAR_VISEMES: Mapping[str, int] = MappingProxyType(
    {char: viseme for viseme, chars in _SHAPES for char in chars}
)


def _round_half_up(value: float) -> int:
    """Round the way the other SDK's ``Math.round`` does.

    Python's built-in :func:`round` breaks a .5 tie to the even number, so the
    two SDKs would place the same mark on different milliseconds for the same
    text and duration. Times are the one thing they cannot disagree about.
    """
    return math.floor(value + 0.5)


def viseme_for_char(ch: str) -> int | None:
    """The mouth shape a character wears, or ``None`` when it has none.

    The lookup is on the raw character after lowercasing, with no Unicode
    normalization at all: adding NFKC here would change which characters map and
    the two SDKs would disagree on the same string. Digits, punctuation, the
    tatweel and the non-vowel diacritics come back ``None``, and every caller
    skips them rather than holding the mouth closed over them.
    """
    return CHAR_VISEMES.get(ch.lower())


def _token_for(ch: str) -> int | None:
    """One character's token: silence for a space, the map for anything else."""
    if ch == " ":
        return SILENCE_VISEME
    return viseme_for_char(ch)


def _timeline(marks: Sequence[SpeechMark]) -> list[SpeechMark]:
    """Collapse runs, then make the times strictly increasing.

    Two rules, both about what a renderer can actually use. A run of one shape
    ('mmm') is one mouth position, so only a CHANGE earns a mark, timed at the
    first character of the run; a mark per character would multiply the payload
    for an identical rendering. And when a long sentence is spread over a very
    short buffer the step falls under half a millisecond and neighbouring marks
    round onto the same one: the later shape wins, because a shape held for zero
    milliseconds is not renderable, and because :func:`~standin.avatar.speech_marks`
    sorts by ``(t_ms, viseme_id)`` and would otherwise pick a different winner
    than the one the walk ended on.
    """
    kept: list[SpeechMark] = []
    previous = _NO_VISEME
    for t_ms, viseme in marks:
        if viseme == previous:
            continue
        previous = viseme
        if kept and t_ms <= kept[-1][0]:
            kept[-1] = (kept[-1][0], viseme)
            continue
        kept.append((t_ms, viseme))
    return kept


def estimate_visemes(text: str | None, duration_ms: float) -> list[SpeechMark]:
    """Spread ``text`` over ``duration_ms`` as a viseme timeline.

    Pass the duration of the audio you actually sent for the turn, which
    :class:`TurnLipSync` counts for you. Anything else is a guess, and a guessed
    timeline is worse than no timeline at all.

    Args:
        text: what was spoken. Lowercased, whitespace runs collapsed, trimmed.
        duration_ms: how long that audio ran. Anything that is not a positive
            finite number returns no marks rather than being divided by,
            because an infinite or not-a-number timestamp desynchronises the
            mouth for the rest of the utterance.

    Returns:
        ``(t_ms, viseme_id)`` marks, strictly increasing in time, ready for
        :func:`~standin.avatar.speech_marks`. Empty when there is nothing to
        say: no text, no duration, or nothing in the text that has a mouth
        shape, which is the right answer for '3.5%' or an emoji on its own.

    Example:
        ```python
        marks = estimate_visemes(final_transcript, lipsync.duration_ms)
        if marks:
            await session.send_speech_marks(marks)
        ```
    """
    normalized = _WHITESPACE_RUN.sub(" ", (text or "").lower()).strip(" ")
    if not normalized or not math.isfinite(duration_ms) or duration_ms <= 0:
        return []
    # Iterating the str walks whole characters, so an astral one (an emoji, a
    # rare sign) is skipped once instead of being read as two broken halves.
    tokens = [token for token in map(_token_for, normalized) if token is not None]
    if not any(tokens):
        return []
    step = duration_ms / len(tokens)
    return _timeline([(_round_half_up(i * step), token) for i, token in enumerate(tokens)])


def visemes_from_alignment(
    characters: Sequence[str],
    start_times_seconds: Sequence[float],
) -> list[SpeechMark]:
    """Build the timeline from per-character timings the speech provider gave you.

    Real times are strictly better than an estimate and cost nothing when the
    provider already returns them, so prefer this whenever a synthesis call can
    hand back an alignment. Core takes the two plain sequences; normalising a
    vendor's field names is the speech plugin's job.

    Args:
        characters: the characters as the provider spoke them.
        start_times_seconds: when each one starts, in seconds from the start of
            the utterance.

    Returns:
        ``(t_ms, viseme_id)`` marks, or an empty list when the alignment holds
        no mouth shape at all (all spaces, all punctuation). Fall back to
        :func:`estimate_visemes` on an EMPTY result rather than on a missing
        alignment: a provider that returns timings for punctuation only has an
        alignment and still needs the estimate.

    Ragged sequences are tolerated: the walk stops at the shorter of the two.
    Providers do return mismatched lengths, and throwing there would lose the
    turn over a cosmetic hint.
    """
    count = min(len(characters), len(start_times_seconds))
    marks: list[SpeechMark] = []
    spoke = False
    for i in range(count):
        seconds = start_times_seconds[i]
        # A time that is not a finite number is provider sloppiness of the same
        # class as a ragged array, so it costs its own mark and nothing else.
        if not math.isfinite(seconds):
            continue
        token = _token_for(characters[i])
        if token is None:
            continue
        spoke = spoke or token != SILENCE_VISEME
        marks.append((max(0, _round_half_up(seconds * 1000)), token))
    if not spoke:
        return []
    return _timeline(marks)


class TurnLipSync:
    """Counts the audio one turn actually sent, then times the mouth to it.

    Feed it every buffer you hand to the call, and ask it for the timeline when
    that turn's text is final. It resets itself, so the next turn starts from
    zero:

    ```python
    lipsync = TurnLipSync()

    async for chunk in model_audio:          # the audio sink
        await session.send_audio(chunk)
        lipsync.audio_sent(chunk)

    async def on_transcript(text, is_final):  # the final transcript only
        if is_final:
            marks = lipsync.finish(text)
            if marks:
                await session.send_speech_marks(marks)

    async def on_barge_in():                  # playback cancelled
        lipsync.cancel()
    ```

    Emit once per turn, on the final transcript. A partial would send an
    ever-lengthening timeline several times over and the avatar would restart
    the mouth mid-sentence.

    :meth:`cancel` is not optional. On a barge-in the service drops audio the
    caller never heard, and a counter that keeps those milliseconds spreads the
    next turn's text over its own audio plus the discarded audio: the mouth runs
    long for the whole of that turn and every turn after it.
    """

    def __init__(self, *, sample_rate_hz: int = SAMPLE_RATE_HZ) -> None:
        """
        Args:
            sample_rate_hz: the rate of the PCM16 mono buffers passed to
                :meth:`audio_sent`. Defaults to the wire's own rate, which is
                what a plugin sending frames to the call is holding.
        """
        self._sample_rate_hz = sample_rate_hz
        self._duration_ms = 0

    @property
    def duration_ms(self) -> int:
        """Milliseconds of audio sent for the turn in progress. Starts at 0."""
        return self._duration_ms

    def audio_sent(self, pcm: bytes) -> None:
        """Add one PCM16 mono buffer that has gone out to the call."""
        self.audio_sent_ms(len(pcm) / BYTES_PER_SAMPLE / self._sample_rate_hz * 1000)

    def audio_sent_ms(self, ms: float) -> None:
        """Add a duration directly, for a sink that hands over encoded audio.

        Rounded per chunk rather than kept as a running float, so both SDKs
        accumulate the same integer for the same stream of chunks. A chunk that
        measures as nothing, or as no number at all, is ignored rather than
        taking the turn's count with it.
        """
        if not math.isfinite(ms) or ms <= 0:
            return
        self._duration_ms += _round_half_up(ms)

    def cancel(self) -> None:
        """Drop the count on a barge-in or a playback cancel, emitting nothing."""
        self._duration_ms = 0

    def finish(self, text: str | None) -> list[SpeechMark]:
        """Return the turn's timeline and reset the counter.

        Empty when no audio was sent or the text carries no mouth shape, and a
        caller sends nothing in that case. The reset happens either way: the
        next turn must not inherit these milliseconds.
        """
        marks = estimate_visemes(text, self._duration_ms)
        self._duration_ms = 0
        return marks
