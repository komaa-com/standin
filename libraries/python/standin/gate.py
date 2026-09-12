# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Who the assistant answers, and what makes it stop talking.

Two decisions, both taken on a FINISHED caller turn, both deterministic in code
rather than left to the model:

* **the group gate** - in a meeting the assistant stays silent until somebody
  addresses it by name, then a short follow-up window lets the conversation
  continue without repeating the name every sentence. A 1:1 call always answers.
* **verbal interrupts** - "stop", "wait", "توقف", "arrête" cut playback whether
  or not the model would have chosen to stop.

They live together because they consume the same thing: one transcript, once.

**The trap this avoids.** A participant count never arrives on the meeting-join
path, so a gate that decides "is this a group call?" from the count alone is dead
on exactly the calls it exists for, and the assistant answers every turn of every
meeting it is invited to. The primary signal here is instead a thread id
beginning ``19:``, which is Microsoft's own marker for a meeting or channel
thread and is present in ``session.start`` on every call. The participant count
is kept as a second, corroborating signal. Either one is enough.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

__all__ = [
    "DEFAULT_FOLLOW_UP_WINDOW_MS",
    "GateDecision",
    "GroupGate",
    "is_addressed",
    "is_meeting_thread",
    "is_verbal_interrupt",
]

DEFAULT_FOLLOW_UP_WINDOW_MS = 12_000

#: Microsoft's thread-id prefix for a meeting or channel conversation. A 1:1
#: call has no such thread at all, so the presence of one is the signal.
_MEETING_THREAD_PREFIX = "19:"


def is_meeting_thread(thread_id: str | None) -> bool:
    """Is this call attached to a Microsoft Teams meeting or channel thread?

    The group signal that actually arrives. See the module docstring: the
    participant count that used to carry this does not reach a bot that joined
    through the meeting, so a gate keyed on it alone never fires.
    """
    return (thread_id or "").strip().startswith(_MEETING_THREAD_PREFIX)


@dataclass(frozen=True)
class GateDecision:
    """The outcome for one finished caller turn.

    Attributes:
        respond: speak an answer to this turn.
        addressed: the turn named the assistant. Opens the follow-up window,
            and is worth knowing separately from ``respond`` - a turn inside the
            window is answered without having been addressed.
    """

    respond: bool
    addressed: bool


def is_addressed(transcript: str, wake_phrases: tuple[str, ...]) -> bool:
    """Case-insensitive, word-boundary match of any wake phrase.

    Boundaries rather than substrings, so "assistant" matches and "assistants"
    does not. Python's ``\\w`` is Unicode-aware by default, so an Arabic wake
    phrase behaves the same as a Latin one. An empty phrase list never matches.
    """
    if not transcript or not wake_phrases:
        return False
    lowered = transcript.lower()
    for phrase in wake_phrases:
        phrase = phrase.strip().lower()
        if phrase and re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            return True
    return False


class GroupGate:
    """The group-call gate for one call. Holds the follow-up window's state.

    Args:
        wake_phrases: what addresses the assistant.
        require_address: turn the gate off entirely.
        follow_up_window_ms: how long an addressed turn keeps the floor open.
        thread_id: the call's Microsoft Teams thread. A meeting thread arms the gate.
    """

    def __init__(
        self,
        *,
        wake_phrases: tuple[str, ...],
        require_address: bool = True,
        follow_up_window_ms: int = DEFAULT_FOLLOW_UP_WINDOW_MS,
        thread_id: str = "",
    ) -> None:
        self.wake_phrases = wake_phrases
        self.require_address = require_address
        self.follow_up_window_ms = follow_up_window_ms
        self._meeting_thread = is_meeting_thread(thread_id)
        self._human_count = 0
        self._last_addressed_ms: float | None = None

    @property
    def is_group(self) -> bool:
        """More than one human on this call, by either signal."""
        return self._meeting_thread or self._human_count >= 2

    @property
    def active(self) -> bool:
        """Is the gate actually muting anything right now?

        A gate with no wake phrase configured can never be opened, so it would
        mute the assistant for the whole call. Treat "no trigger configured" as
        gate off.
        """
        return self.is_group and self.require_address and any(p.strip() for p in self.wake_phrases)

    def note_participants(self, count: int) -> None:
        """Record a participant count from call context.

        Corroborating, never authoritative: a count that says 1 does not clear a
        meeting thread. The count is the signal that goes missing, so it may add
        certainty and must not remove it.
        """
        self._human_count = max(self._human_count, count)

    def decide(self, transcript: str, now_ms: float) -> GateDecision:
        """Answer this turn, or stay out of the meeting?"""
        addressed = is_addressed(transcript, self.wake_phrases)
        if not self.active:
            return GateDecision(respond=True, addressed=addressed)
        if addressed:
            self._last_addressed_ms = now_ms
            return GateDecision(respond=True, addressed=True)
        if (
            self._last_addressed_ms is not None
            and now_ms - self._last_addressed_ms <= self.follow_up_window_ms
        ):
            return GateDecision(respond=True, addressed=False)
        return GateDecision(respond=False, addressed=False)


# ---- verbal interrupts ---------------------------------------------------

#: Whole-utterance interrupt phrases, normalised, in the languages the shipped
#: bridge supported. Whole utterance and not substring on purpose: "stop by the
#: store" is a sentence, not an interruption.
_INTERRUPT_PHRASES: frozenset[str] = frozenset(
    {
        # English
        "stop",
        "stop stop",
        "stop stop stop",
        "wait",
        "wait wait",
        "hold on",
        "hold up",
        "never mind",
        "nevermind",
        "quiet",
        "be quiet",
        "shut up",
        "enough",
        "cancel",
        "cancel that",
        # Arabic
        "توقف",
        "قف",
        "خلاص",
        "كفى",
        "اسكت",
        "بس",
        "كفاية",
        # French
        "arrête",
        "arrêtez",
        "arrête toi",
        "attends",
        "attendez",
        "ça suffit",
        "tais toi",
        "taisez vous",
        "laisse tomber",
        "annule",
        "annulez",
        # German
        "stopp",
        "halt",
        "warte",
        "warten sie",
        "moment",
        "moment mal",
        "das reicht",
        "es reicht",
        "sei still",
        "ruhe",
        "abbrechen",
        "vergiss es",
    }
)

#: Filler peeled off both ends before matching, so "um, please stop" still reads
#: as an interrupt.
_FILLER: frozenset[str] = frozenset(
    {
        # English
        "um",
        "uh",
        "er",
        "ok",
        "okay",
        "please",
        "hey",
        "yeah",
        "no",
        # Arabic
        "من",
        "فضلك",
        "يا",
        # French
        "euh",
        "bah",
        "bon",
        "alors",
        "s",
        "il",
        "te",
        "vous",
        "plaît",
        "plait",
        # German
        "äh",
        "ähm",
        "also",
        "na",
        "mal",
        "bitte",
    }
)

#: Arabic diacritics and tatweel. Stripped so a vocalised "تَوَقَّف" matches.
_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰۖ-ۭـ]")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = _TASHKEEL.sub("", text).lower()
    return re.sub(r"\s+", " ", _NON_WORD.sub(" ", text)).strip()


def _strip_edges(tokens: list[str], wake_lists: list[list[str]]) -> list[str]:
    """Peel filler and wake phrases off both ends until nothing changes.

    Iterative rather than one pass: "hermes, please stop" has a wake phrase
    outside a filler word, and either order occurs.
    """

    def peel_leading(toks: list[str]) -> bool:
        changed = False
        while toks and toks[0] in _FILLER:
            toks.pop(0)
            changed = True
        for wl in wake_lists:
            if wl and toks[: len(wl)] == wl:
                del toks[: len(wl)]
                return True
        return changed

    def peel_trailing(toks: list[str]) -> bool:
        changed = False
        while toks and toks[-1] in _FILLER:
            toks.pop()
            changed = True
        for wl in wake_lists:
            if wl and len(toks) >= len(wl) and toks[len(toks) - len(wl) :] == wl:
                del toks[len(toks) - len(wl) :]
                return True
        return changed

    while tokens and peel_leading(tokens):
        pass
    while tokens and peel_trailing(tokens):
        pass
    return tokens


def is_verbal_interrupt(transcript: str, wake_phrases: tuple[str, ...] = ()) -> bool:
    """Is this whole utterance just a request to stop talking?"""
    norm = _normalize(transcript)
    if not norm:
        return False
    wake_lists = [_normalize(p).split(" ") for p in wake_phrases if _normalize(p)]
    tokens = _strip_edges([t for t in norm.split(" ") if t], wake_lists)
    return " ".join(tokens) in _INTERRUPT_PHRASES
