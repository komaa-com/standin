# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Stop the model answering its own playback.

On a speakerphone the assistant's own voice loops back into the caller's
microphone, the realtime model's VAD hears it, and the model replies to itself.
Left alone it does that in a loop, with the caller silent, until somebody hangs
up.

The fix is a playout clock. Wall-clock send time is useless here because the
model streams audio far faster than realtime, so the guard accumulates the
DURATION of what it has sent instead and treats "now < playout end + tail" as
"we are probably still being heard". While that holds, inbound audio is dropped
unless it is loud enough to be a person actually interrupting.

Until the caller's first real turn, no barge-in is allowed at all: the opening
greeting echoing back is exactly the loud thing that would otherwise make the
assistant interrupt and re-greet itself.

This began life inside one plugin, on the argument that a playout clock belongs
to whoever calls ``send_audio``. That was half right. The clock is generic - it
needs only the length of the audio sent and the loudness of what came back, both
of which every plugin has - so it lives here now, and two plugins stopped
carrying their own copy of it. What stays per plugin is the TAIL: how long a
given provider's VAD keeps hearing us is a property of that provider, not of the
wire, which is why it is a constructor argument rather than a constant.

Pure logic with an injectable clock, so all of it is unit-testable.
"""

from __future__ import annotations

import time

from .audio import pcm16_rms

#: ``pcm16_rms`` is re-exported from :mod:`standin.audio`, where the single
#: definition lives, because an echo guard, a barge-in check and a voice
#: segmenter all want it.
__all__ = ["ECHO_BARGE_IN_RMS", "ECHO_SUPPRESSION_WINDOW_MS", "EchoGuard", "pcm16_rms"]

#: How long after our own audio should have finished playing the guard keeps
#: treating inbound sound as probable echo.
ECHO_SUPPRESSION_WINDOW_MS = 600

#: Loudness above which in-window caller audio is a real interruption rather
#: than our own voice coming back.
ECHO_BARGE_IN_RMS = 0.04


def _now_ms() -> float:
    return time.monotonic() * 1000.0


class EchoGuard:
    """Decides, per inbound frame, whether the caller's audio reaches the model.

    Args:
        enabled: turn the whole guard off. Useful on a headset-only deployment
            where there is no acoustic path back into the microphone.
        tail_window_ms: how long after our audio should have finished playing we
            still treat inbound audio as suspect. Covers the network and jitter
            buffer between us and the caller's speaker.
        barge_in_rms: loudness a frame must reach, while we are speaking, to be
            believed as a real interruption rather than our own echo.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        tail_window_ms: int = 600,
        barge_in_rms: float = 0.04,
    ) -> None:
        self.enabled = enabled
        self.tail_window_ms = tail_window_ms
        self.barge_in_rms = barge_in_rms
        self._playout_end_ms = 0.0
        self._first_turn = False

    def note_output(self, duration_ms: float, now: float | None = None) -> None:
        """Record that ``duration_ms`` of our audio was handed to StandIn.

        ``max(now, ...)`` rather than a bare add: after a gap in speaking, the
        old horizon is in the past and adding to it would leave the clock behind
        real time for the whole rest of the call.
        """
        now = _now_ms() if now is None else now
        self._playout_end_ms = max(now, self._playout_end_ms) + duration_ms

    def collapse(self, now: float | None = None) -> None:
        """A barge-in was accepted: playback is cut and the caller has the floor.

        Pulls the horizon fully behind the tail window so :meth:`speaking` is
        immediately false. Without it the guard would keep filtering the caller's
        audio for the length of the buffer we just cancelled - which is precisely
        the words they interrupted us to say.
        """
        now = _now_ms() if now is None else now
        self._playout_end_ms = now - self.tail_window_ms

    def mark_caller_turn(self) -> None:
        """The caller has spoken a real turn; barge-in is allowed from now on."""
        self._first_turn = True

    def speaking(self, now: float | None = None) -> bool:
        """Is our own audio still likely to be audible at the caller's end?"""
        now = _now_ms() if now is None else now
        return now < self._playout_end_ms + self.tail_window_ms

    def allow_input(self, rms: float, now: float | None = None) -> bool:
        """Should this inbound frame reach the model?"""
        if not self.enabled:
            return True
        now = _now_ms() if now is None else now
        if not self.speaking(now):
            return True
        if not self._first_turn:
            return False  # the opening greeting must not interrupt itself
        return rms >= self.barge_in_rms
