# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Caller audio in, a spoken answer out, for agents that are not speech to speech.

A speech-to-speech model hears the caller and talks back, and a plugin for one
is mostly a socket. Most agents are not that. They read text, they write text,
and getting them onto a phone call means four things in a row: work out where
the caller stopped talking, turn that into words, ask the agent, and say the
answer back at the rate a call consumes audio.

:mod:`standin.voice` has each of those pieces. This is the thing that runs them
in order, holds the turn together, and gets the awkward parts right:

**One turn at a time.** An agent asked two questions at once answers neither
well. A new utterance supersedes the one in flight rather than racing it.

**Barge-in actually stops the answer.** Somebody who interrupts has stopped
listening, and a worker that keeps streaming a paragraph at them is talking to
nobody. The buffered audio is dropped at the same moment the new utterance
opens, not when the old one finishes.

**A silence is not a turn.** A cough, a door, a second of traffic: the segmenter
opens on any loud frame, and waking the agent for every one of them is a bill
and a caller being answered at random.

**Nothing here raises into the call.** A provider that fails says so, in a
sentence, out loud. Silence is the one thing a caller cannot interpret.

    lane = VoiceLane(session, transcribe=stt, answer=agent, synthesize=tts)

    async def on_caller_audio(self, pcm: bytes) -> None:
        await lane.feed(pcm)
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from .handler import CallSession
from .log import logger
from .voice import PacedPlayback, UtteranceSegmenter

__all__ = [
    "TROUBLE_ANSWERING",
    "TROUBLE_HEARING",
    "TROUBLE_SPEAKING",
    "Answer",
    "Synthesize",
    "Transcribe",
    "VoiceLane",
    "VoiceTurn",
]

#: What the caller hears when a step of the lane fails.
#:
#: Spoken, not logged and swallowed. Somebody on a phone call cannot tell a
#: broken transcriber from an agent that is thinking, and will keep waiting.
TROUBLE_HEARING = "Sorry, I did not catch that."
TROUBLE_ANSWERING = "Sorry, I am having trouble answering just now."
TROUBLE_SPEAKING = "Sorry, I am having trouble speaking just now."

#: Caller audio (PCM16 mono, 16 kHz) to words. Empty means nothing was said.
Transcribe = Callable[[bytes], Awaitable[str]]

#: Words to an answer. Either the whole thing, or sentences as they are written.
Answer = Callable[[str], Awaitable[str] | AsyncIterator[str]]

#: An answer to speech (PCM16 mono, 16 kHz). Either one buffer, or chunks.
Synthesize = Callable[[str], Awaitable[bytes] | AsyncIterator[bytes]]


@dataclass(frozen=True)
class VoiceTurn:
    """One exchange, after it is over."""

    heard: str
    said: str
    interrupted: bool = False
    """Whether the caller cut the answer short. Not a failure: it is the most
    common way a real conversation goes."""

    error: str = ""


class VoiceLane:
    """Runs one call's worth of listen, transcribe, answer, speak.

    Built by a plugin, which supplies the three steps. Everything about pacing,
    interruption and turn-taking is here, because getting those wrong is what
    makes a working provider sound broken.
    """

    def __init__(
        self,
        session: CallSession,
        transcribe: Transcribe,
        answer: Answer,
        synthesize: Synthesize,
        segmenter: UtteranceSegmenter | None = None,
        on_turn: Callable[[VoiceTurn], None] | None = None,
    ) -> None:
        self._session = session
        self._transcribe = transcribe
        self._answer = answer
        self._synthesize = synthesize
        self._segmenter = segmenter or UtteranceSegmenter()
        self._on_turn = on_turn
        self._playback = PacedPlayback(session.send_audio)
        self._turn: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def speaking(self) -> bool:
        """Whether the agent is talking right now."""
        return self._playback.playing

    @property
    def turn(self) -> asyncio.Task[None] | None:
        """The turn in flight, if any. Await it to let one finish."""
        return self._turn

    @property
    def busy(self) -> bool:
        """Whether a turn is in flight, including the model's own thinking."""
        return self._turn is not None and not self._turn.done()

    async def feed(self, pcm: bytes) -> None:
        """Take one frame of caller audio. Never raises, never blocks."""
        if self._closed:
            return
        was_speaking = self._segmenter.speaking
        utterance = self._segmenter.feed(pcm)
        if not was_speaking and self._segmenter.speaking and self._playback.playing:
            # The caller started over the top of the answer. Drop what is
            # buffered NOW rather than when this utterance finishes: the extra
            # second of talking at somebody who has stopped listening is the
            # whole difference between a call that feels alive and one that
            # does not.
            await self.barge_in()
        if utterance is not None:
            self._begin(utterance)

    async def barge_in(self) -> None:
        """Stop talking, immediately. The caller interrupted."""
        self._playback.cancel()
        with contextlib.suppress(Exception):
            await self._session.cancel_playback()

    async def say(self, text: str) -> VoiceTurn:
        """Speak a line the agent did not have to be asked for.

        A greeting, a handover, something that arrived from outside the call.
        """
        # Same guard as feed(). A line handed in after teardown would otherwise
        # synthesize and send on a call that has already gone.
        if self._closed:
            return VoiceTurn("", "", error="the call has ended")
        return await self._speak(text, heard="")

    async def aclose(self) -> None:
        """Stop everything. Called once, on teardown."""
        self._closed = True
        self._playback.cancel()
        self._segmenter.reset()
        turn, self._turn = self._turn, None
        if turn is not None:
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await turn

    # ---- one turn ---------------------------------------------------------

    def _begin(self, utterance: bytes) -> None:
        """Start a turn, superseding whatever was in flight.

        Detached on purpose: this is reached from the receive path of a live
        call, and awaiting a model there stops frames arriving.
        """
        previous, self._turn = self._turn, None
        if previous is not None and not previous.done():
            # One turn at a time. An agent asked two questions at once answers
            # neither well, and both answers would be spoken over each other.
            previous.cancel()
        self._turn = asyncio.ensure_future(self._run(utterance))

    async def _run(self, utterance: bytes) -> None:
        try:
            heard = await self._hear(utterance)
            if heard is None:
                return
            await self._respond(heard)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: the voice turn failed")

    async def _hear(self, utterance: bytes) -> str | None:
        try:
            heard = (await self._transcribe(utterance) or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: could not transcribe the caller: %s", err)
            await self._speak(TROUBLE_HEARING, heard="", error=str(err))
            return None
        if not heard:
            # A cough, a door, a second of traffic. The segmenter opens on any
            # loud frame, and waking the agent for every one of them is a bill
            # and a caller being answered at random.
            logger.debug("standin: an utterance transcribed to nothing; no turn")
            return None
        return heard

    async def _respond(self, heard: str) -> None:
        spoken: list[str] = []
        interrupted = False
        try:
            reply = self._answer(heard)
            if isinstance(reply, AsyncIterator):
                # Sentence by sentence, so the caller hears the beginning of a
                # long answer while the rest is still being written.
                async for piece in reply:
                    if not piece.strip():
                        continue
                    turn = await self._speak(piece, heard=heard)
                    spoken.append(turn.said)
                    if turn.interrupted:
                        interrupted = True
                        break
                if not spoken:
                    return
                self._finished(VoiceTurn(heard, " ".join(spoken), interrupted))
                return
            said = (await reply or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: the agent did not answer: %s", err)
            await self._speak(TROUBLE_ANSWERING, heard=heard, error=str(err))
            return
        if not said:
            logger.debug("standin: the agent answered with nothing; staying quiet")
            return
        self._finished(await self._speak(said, heard=heard))

    async def _speak(self, text: str, heard: str, error: str = "") -> VoiceTurn:
        """Say one piece of an answer, and report what the caller heard."""
        line = (text or "").strip()
        if not line:
            return VoiceTurn(heard, "", error=error)
        try:
            audio = self._synthesize(line)
            if isinstance(audio, AsyncIterator):
                interrupted = False
                async for chunk in audio:
                    played = await self._playback.say(chunk)
                    if played.interrupted:
                        interrupted = True
                        break
                return VoiceTurn(heard, line, interrupted, error)
            played = await self._playback.say(await audio)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: could not speak: %s", err)
            if line != TROUBLE_SPEAKING:
                # One retry, with the sentence that says what happened. Without
                # it a synthesis failure is indistinguishable from a dropped
                # call, and the caller waits for an answer that is not coming.
                return await self._speak(TROUBLE_SPEAKING, heard=heard, error=str(err))
            return VoiceTurn(heard, "", error=str(err))
        return VoiceTurn(heard, line, played.interrupted, error)

    def _finished(self, turn: VoiceTurn) -> None:
        if self._on_turn is None:
            return
        with contextlib.suppress(Exception):
            self._on_turn(turn)
