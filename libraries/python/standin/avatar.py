# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The avatar lane: the face the caller sees while your agent talks.

StandIn renders the avatar tile. Your worker does not draw it, stream it, or
know how it is made - it sends two hints and the service does the rest:

* :func:`expression` names the emotion the face should wear.
* :func:`speech_marks` carries the viseme timeline for one utterance, which is
  what makes the mouth match the words.

Both are **additive and best-effort**. A service that does not implement one
ignores it, an unknown emotion falls back to neutral, and neither ever affects
the audio the caller hears. That is why nothing here can fail a call: the worst
case is a face that stays neutral while the voice is unchanged.

Neither needs a provider that offers it. :func:`infer_emotion` reads the emotion
straight out of the reply text, with no extra model call and no added latency,
and :mod:`standin.lipsync` builds a viseme timeline for a provider that returns
no timings at all.

Send :func:`speech_marks` whenever the timeline is spread over a duration you
MEASURED: the audio actually sent for that turn, or real per-character timings
from the speech provider. That beats a still mouth and is the default on a
realtime path, where the model hands back no timings and the mouth would
otherwise never move. A timeline spread over a duration GUESSED from text length
or a words-per-minute rate is still worse than none, because its error compounds
with every sentence and a mouth moving out of step with the voice is more
distracting than a still one. The distinction is the duration, not the phonemes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ._protocol_runtime import encode
from .protocol import TYPE_EXPRESSION, TYPE_SPEECH_MARKS

__all__ = [
    "EMOTIONS",
    "MAX_EMOTION_CHARS",
    "MAX_VISEME_ID",
    "ExpressionCue",
    "SpeechMark",
    "expression",
    "infer_emotion",
    "speech_marks",
]

#: The emotions StandIn knows by name. An open set: sending something else is
#: allowed and renders as neutral, so a newer sender and an older service still
#: interoperate.
EMOTIONS = ("neutral", "happy", "sad", "surprised", "thinking")

#: Visemes use the Azure Speech numbering, 0 to 21, which is what the avatar
#: expects. A mark outside that range is dropped rather than sent.
MAX_VISEME_ID = 21

#: An emotion reaches the avatar tile, and the string usually came from a model
#: that whoever is on the call is steering. Bounded here, where the message is
#: built, so a plugin cannot forget to bound it.
MAX_EMOTION_CHARS = 40

#: One viseme mark: milliseconds from the start of the utterance, and which
#: mouth shape to hold.
SpeechMark = tuple[int, int]


def expression(emotion: str) -> str:
    """Build an ``expression``: the emotion the avatar should wear.

    Affects the video tile only, never the audio. Raises on an empty value or
    one longer than :data:`MAX_EMOTION_CHARS`; everything else is the service's
    to interpret, and an unknown emotion renders as neutral.
    """
    text = emotion.strip()
    if not text:
        raise ValueError("expression needs an emotion")
    if len(text) > MAX_EMOTION_CHARS:
        raise ValueError(f"an emotion must be at most {MAX_EMOTION_CHARS} characters")
    return encode({"type": TYPE_EXPRESSION, "emotion": text})


def speech_marks(marks: Iterable[SpeechMark]) -> str:
    """Build a ``speech.marks``: the viseme timeline for one utterance.

    Each mark is ``(t_ms, viseme_id)`` on that utterance's own audio timeline.
    Marks are sorted ascending and marks outside the viseme range are dropped,
    because the avatar reads the timeline in order and one bad entry would
    desynchronise the mouth for the rest of the utterance.
    """
    cleaned = sorted(
        (int(t_ms), int(viseme_id))
        for t_ms, viseme_id in marks
        if t_ms >= 0 and 0 <= viseme_id <= MAX_VISEME_ID
    )
    message: dict[str, Any] = {
        "type": TYPE_SPEECH_MARKS,
        # A reserved timeline anchor the lane does not use yet. Both SDKs send 0.
        "ts": 0,
        "marks": [{"tMs": t_ms, "visemeId": viseme_id} for t_ms, viseme_id in cleaned],
    }
    return encode(message)


# ------------------------------------------------------- reading the emotion

#: Astonishment the words do not carry. Read on the RAW text, before
#: lowercasing, because punctuation is how a model writes a startled reply when
#: none of the surprise words below appear in it.
_SURPRISE_PUNCTUATION = re.compile(r"[?!]{2,}")

# The lexicon. Word boundaries on every entry, or 'nicety' reads as happy and
# 'greatly' reads as happy, and the face flickers on words nobody stressed.
# ASCII bounds because the lexicon is ASCII: left to Python's Unicode \b, an
# English word running straight into an Arabic one ('سlove') would carry no
# boundary here and a happy one in the other SDK, whose \b is ASCII already.
_SURPRISED_WORDS = re.compile(
    r"\b(?:wow|whoa|woah|oh no|oh my|no way|unbelievable|incredible"
    r"|astonish\w*|surpris\w*)\b",
    re.ASCII,
)
_SAD_WORDS = re.compile(
    r"\b(?:sorry|apolog\w*|unfortunately|regret\w*|afraid|sadly|bad news"
    r"|failed|unable to|i can't|i cannot|i'm unable)\b",
    re.ASCII,
)
_HAPPY_WORDS = re.compile(
    r"\b(?:glad|great|awesome|wonderful|fantastic|excellent|congrat\w*|happy"
    r"|love|perfect|good news|success\w*|thank\w*|welcome|nice|well done)\b",
    re.ASCII,
)

#: Models emit the typographic apostrophe constantly, and matching only the
#: ASCII one would let the most common apologetic phrasing there is, "I can't",
#: infer neutral.
_SMART_APOSTROPHE = "’"


def infer_emotion(text: str) -> str:
    """Infer the emotion a reply should be worn with, from its words alone.

    No extra model call and no added latency, which is the only reason a cue can
    exist on a live call at all. Returns one of ``surprised``, ``sad``, ``happy``
    or ``neutral``: pass it straight to :func:`expression`.

    First match wins, in that order, rather than a score. A startled 'wow!' must
    not be averaged away by a polite 'thanks', and an apology must not be masked
    by an incidental 'nice'. A mixed reply therefore resolves to the
    higher-priority reading, which is acceptable precisely because a realtime
    caller re-infers as more of the reply arrives and the face self-corrects.

    The lexicon is ENGLISH ONLY, and that is a documented gap rather than an
    oversight: a reply in Arabic, or in any other language, infers ``neutral``.
    Neutral is always a safe face, and a silent guess at the language would put
    a confident wrong one on the tile. Note that the viseme table in
    :mod:`standin.lipsync` IS bilingual, so an implementer who expects the same
    of this one will go looking for a bug that is not there.
    """
    if not text or not text.strip():
        return "neutral"
    if _SURPRISE_PUNCTUATION.search(text):
        return "surprised"
    words = text.lower().replace(_SMART_APOSTROPHE, "'")
    if _SURPRISED_WORDS.search(words):
        return "surprised"
    if _SAD_WORDS.search(words):
        return "sad"
    if _HAPPY_WORDS.search(words):
        return "happy"
    return "neutral"


class ExpressionCue:
    """Decides WHEN an expression is worth sending, so the face is neither stale
    nor chattering.

    Ask it on every assistant transcript, partial and final alike, and send
    whatever comes back. Waiting for the final transcript would land the cue as
    the sentence ends, with the face stale for the whole time a happy or
    apologetic reply was being spoken:

    ```python
    cues = ExpressionCue()

    async def on_transcript(text: str, is_final: bool) -> None:
        emotion = cues.cue(text)
        if emotion:
            await session.express(emotion)

    async def run_tool(name: str) -> str:
        emotion = cues.thinking(True)
        if emotion:
            await session.express(emotion)
        try:
            return await tools.run(name)
        finally:
            emotion = cues.thinking(False)
            if emotion:
                await session.express(emotion)
    ```

    One instance per call: what it remembers is the last emotion that call was
    sent, which is what stops a partial-per-word stream from sending dozens of
    identical messages.
    """

    def __init__(self) -> None:
        self._last_sent: str | None = None
        self._thinking = False

    @property
    def last_sent(self) -> str | None:
        """The emotion this call was last told to wear, or ``None`` before the
        first cue."""
        return self._last_sent

    def cue(self, text: str) -> str | None:
        """The emotion to send for this chunk of reply, or ``None`` to send
        nothing.

        ``None`` means the face is already right: either the reading has not
        changed since the last cue, or a tool is running and the thinking face
        must hold. Without that suppression a transcript chunk arriving mid-tool
        makes the avatar look finished while it is still working.
        """
        if self._thinking:
            return None
        emotion = infer_emotion(text)
        if emotion == self._last_sent:
            return None
        self._last_sent = emotion
        return emotion

    def thinking(self, on: bool) -> str | None:
        """Enter or leave the thinking face; returns what to send, if anything.

        Only a transition acts, so setting the same state twice sends nothing.
        Leaving sends ``neutral`` when the thinking face is still the last thing
        this call was told to wear: the model may say nothing at all after a
        tool result, and with no transcript to re-infer from the face would
        stick mid-thought for the rest of the call. Call it from a ``finally``
        so a tool that raised leaves the face behind it.
        """
        if on == self._thinking:
            return None
        self._thinking = on
        if on:
            self._last_sent = "thinking"
            return "thinking"
        if self._last_sent == "thinking":
            self._last_sent = "neutral"
            return "neutral"
        return None
