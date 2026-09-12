# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Listen, transcribe, answer, speak, for agents that are not speech to speech.

The twin is ``libraries/typescript/src/lane.test.ts``.
"""

from __future__ import annotations

import asyncio

import pytest

from standin.lane import TROUBLE_ANSWERING, TROUBLE_HEARING, VoiceLane, VoiceTurn
from standin.voice import UtteranceSegmenter

pytestmark = pytest.mark.unit

FRAME_MS = 20
FRAME = 16_000 * FRAME_MS // 1000 * 2

LOUD = b"\x00\x40" * (FRAME // 2)
QUIET = bytes(FRAME)


class FakeCall:
    def __init__(self) -> None:
        self.call_id = "call-1"
        self.sent: list[bytes] = []
        self.cancelled = 0

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def cancel_playback(self) -> None:
        self.cancelled += 1


def _lane(call: FakeCall, **kwargs) -> VoiceLane:
    async def transcribe(pcm: bytes) -> str:
        return kwargs.pop("_heard", "what is the weather")

    async def answer(text: str) -> str:
        return "It is raining."

    async def synthesize(text: str) -> bytes:
        return LOUD * 3

    return VoiceLane(
        call,
        kwargs.pop("transcribe", transcribe),
        kwargs.pop("answer", answer),
        kwargs.pop("synthesize", synthesize),
        segmenter=kwargs.pop("segmenter", None),
        on_turn=kwargs.pop("on_turn", None),
    )


async def _utterance(lane: VoiceLane, loud_frames: int = 6, quiet_frames: int = 45) -> None:
    """Say something, then stop, so the segmenter closes the utterance."""
    for _ in range(loud_frames):
        await lane.feed(LOUD)
    for _ in range(quiet_frames):
        await lane.feed(QUIET)


async def test_one_utterance_becomes_one_spoken_answer():
    call = FakeCall()
    turns: list[VoiceTurn] = []
    lane = _lane(call, on_turn=turns.append)
    await _utterance(lane)
    await lane.turn

    assert turns == [VoiceTurn("what is the weather", "It is raining.", False)]
    assert call.sent  # the answer actually reached the caller
    await lane.aclose()


async def test_a_cough_does_not_wake_the_agent():
    """The segmenter opens on any loud frame, and waking the agent for every one
    of them is a bill and a caller being answered at random."""
    call = FakeCall()
    asked: list[str] = []

    async def transcribe(pcm: bytes) -> str:
        return "   "

    async def answer(text: str) -> str:
        asked.append(text)
        return "should never be said"

    lane = _lane(call, transcribe=transcribe, answer=answer)
    await _utterance(lane)
    await lane.turn

    assert asked == []
    assert call.sent == []
    await lane.aclose()


async def test_the_caller_hears_what_went_wrong_rather_than_silence():
    """Somebody on a phone call cannot tell a broken transcriber from an agent
    that is thinking, and will keep waiting."""
    call = FakeCall()
    said: list[str] = []

    async def synthesize(text: str) -> bytes:
        said.append(text)
        return LOUD

    async def transcribe(pcm: bytes) -> str:
        raise RuntimeError("the transcriber is down")

    lane = _lane(call, transcribe=transcribe, synthesize=synthesize)
    await _utterance(lane)
    await lane.turn
    assert said == [TROUBLE_HEARING]

    async def answer(text: str) -> str:
        raise RuntimeError("the model is down")

    said.clear()
    lane = _lane(call, answer=answer, synthesize=synthesize)
    await _utterance(lane)
    await lane.turn
    assert said == [TROUBLE_ANSWERING]
    await lane.aclose()


async def test_an_interruption_stops_the_answer_when_it_starts_not_when_it_ends():
    """The extra second of talking at somebody who has stopped listening is the
    whole difference between a call that feels alive and one that does not."""
    call = FakeCall()

    async def synthesize(text: str) -> bytes:
        return LOUD * 200  # four seconds of answer

    lane = _lane(call, synthesize=synthesize)
    await _utterance(lane)
    await asyncio.sleep(0.05)
    assert lane.speaking

    for _ in range(4):
        await lane.feed(LOUD)
    assert call.cancelled == 1

    # The cancel lands within a frame, not at the end of the buffer: four
    # seconds of answer, and what is left after the interruption is one frame.
    sent = len(call.sent)
    await asyncio.sleep(0.1)
    assert lane.speaking is False
    assert len(call.sent) - sent <= 1
    await lane.aclose()


async def test_a_second_question_supersedes_the_first_rather_than_racing_it():
    """An agent asked two questions at once answers neither well, and both
    answers would be spoken over each other."""
    call = FakeCall()
    answered: list[str] = []
    heard = iter(["first question", "second question"])

    async def transcribe(pcm: bytes) -> str:
        return next(heard, "second question")

    async def answer(text: str) -> str:
        answered.append(text)
        await asyncio.sleep(0.2)
        return f"answer to {text}"

    lane = _lane(call, transcribe=transcribe, answer=answer)
    await _utterance(lane)
    await asyncio.sleep(0.02)
    await _utterance(lane)
    await lane.turn

    # Both were asked; only the newer one was ever spoken.
    assert answered == ["first question", "second question"]
    await lane.aclose()


async def test_a_streamed_answer_is_spoken_as_it_is_written():
    call = FakeCall()
    spoken: list[str] = []

    async def answer(text: str):
        for piece in ("It is raining.", "Take a coat."):
            yield piece

    async def synthesize(text: str) -> bytes:
        spoken.append(text)
        return LOUD

    turns: list[VoiceTurn] = []
    lane = _lane(call, answer=answer, synthesize=synthesize, on_turn=turns.append)
    await _utterance(lane)
    await lane.turn

    # The caller hears the beginning of a long answer while the rest is still
    # being written.
    assert spoken == ["It is raining.", "Take a coat."]
    assert turns[0].said == "It is raining. Take a coat."
    await lane.aclose()


async def test_saying_something_nobody_asked_for_still_goes_through_the_lane():
    call = FakeCall()
    spoken: list[str] = []

    async def synthesize(text: str) -> bytes:
        spoken.append(text)
        return LOUD

    lane = _lane(call, synthesize=synthesize)
    turn = await lane.say("Thanks for taking the call.")
    assert turn.said == "Thanks for taking the call."
    assert spoken == ["Thanks for taking the call."]
    await lane.aclose()


async def test_teardown_stops_a_turn_in_flight():
    call = FakeCall()

    async def answer(text: str) -> str:
        await asyncio.sleep(5)
        raise AssertionError("the turn should have been dropped")

    lane = _lane(call, answer=answer)
    await _utterance(lane)
    await asyncio.sleep(0.02)
    assert lane.busy
    await lane.aclose()
    assert lane.busy is False
    # And feeding a closed lane is a no-op rather than an error.
    await lane.feed(LOUD)


async def test_the_segmenter_can_be_tuned_by_the_plugin():
    call = FakeCall()
    lane = _lane(call, segmenter=UtteranceSegmenter(silence_ms=200))
    await _utterance(lane, quiet_frames=12)
    await lane.turn
    assert call.sent
    await lane.aclose()


async def test_a_superseded_turn_does_not_apologise_over_the_one_that_replaced_it():
    """Its own failure belongs to a question nobody is waiting on any more."""
    call = FakeCall()
    spoken: list[str] = []
    heard = iter(["first question", "second question"])

    async def transcribe(pcm: bytes) -> str:
        return next(heard, "second question")

    async def answer(text: str) -> str:
        if text == "first question":
            await asyncio.sleep(0.15)
            raise RuntimeError("the model gave up on the old question")
        return "the newer answer"

    async def synthesize(text: str) -> bytes:
        spoken.append(text)
        return LOUD

    lane = _lane(call, transcribe=transcribe, answer=answer, synthesize=synthesize)
    await _utterance(lane)
    await asyncio.sleep(0.02)
    await _utterance(lane)
    await lane.turn
    await asyncio.sleep(0.25)
    assert spoken == ["the newer answer"]
    await lane.aclose()


async def test_a_line_handed_in_after_teardown_is_refused():
    """It would otherwise synthesize and send on a call that has already gone."""
    call = FakeCall()
    spoken: list[str] = []

    async def synthesize(text: str) -> bytes:
        spoken.append(text)
        return LOUD

    lane = _lane(call, synthesize=synthesize)
    await lane.aclose()
    turn = await lane.say("Are you still there?")
    assert turn.said == ""
    assert turn.error == "the call has ended"
    assert spoken == []
