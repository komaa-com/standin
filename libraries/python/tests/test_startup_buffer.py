# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Nothing the caller does before the agent is ready is lost.

Two things land in that gap and both matter: the caller's first words, which are
often the reason they called, and the first context, which is what makes a
group-call gate engage and a recording gate open.

Before this was shared, five of nine plugins held one lane and silently dropped
the other, and which half they lost was an accident of who wrote them.
"""

from __future__ import annotations

import pytest

from standin.startup import MAX_PENDING_AUDIO, MAX_PENDING_CONTEXT, StartupBuffer

pytestmark = pytest.mark.unit


async def test_both_lanes_survive_the_gap():
    buffer = StartupBuffer()
    buffer.audio(b"first words")
    buffer.context("There are 3 human participants on this call.")
    buffer.audio(b"more words")

    audio: list[bytes] = []
    context: list[str] = []
    released = await buffer.release(send_audio=audio.append, send_context=context.append)

    assert audio == [b"first words", b"more words"]
    assert context == ["There are 3 human participants on this call."]
    assert released == (2, 1)
    assert buffer.holding is False


async def test_audio_is_released_before_context():
    """The provider needs the caller's words in the order they were said; the
    context is a note about the call rather than part of the conversation."""
    buffer = StartupBuffer()
    buffer.context("a note")
    buffer.audio(b"words")

    order: list[str] = []
    await buffer.release(
        send_audio=lambda pcm: order.append("audio"),
        send_context=lambda text: order.append("context"),
    )
    assert order == ["audio", "context"]


async def test_an_async_sender_is_awaited():
    """A provider's send is sometimes fire-and-forget and sometimes not."""
    buffer = StartupBuffer()
    buffer.audio(b"words")
    seen: list[bytes] = []

    async def send(pcm: bytes) -> None:
        seen.append(pcm)

    await buffer.release(send_audio=send)
    assert seen == [b"words"]


async def test_releasing_twice_releases_nothing_the_second_time():
    buffer = StartupBuffer()
    buffer.audio(b"words")
    assert await buffer.release(send_audio=lambda p: None) == (1, 0)
    assert await buffer.release(send_audio=lambda p: None) == (0, 0)


def test_the_bounds_drop_the_oldest_not_the_newest():
    """If something has to be lost, lose the stale audio and keep what just
    happened. A socket that never opens must not grow for the whole call."""
    buffer = StartupBuffer(max_audio=3, max_context=2)
    for i in range(10):
        buffer.audio(str(i).encode())
    for i in range(10):
        buffer.context(f"line {i}")

    assert buffer.dropped == (7, 8)


async def test_the_newest_is_what_survives_the_bound():
    buffer = StartupBuffer(max_audio=2)
    for i in range(5):
        buffer.audio(str(i).encode())
    audio: list[bytes] = []
    await buffer.release(send_audio=audio.append)
    assert audio == [b"3", b"4"]


def test_empty_input_is_ignored():
    buffer = StartupBuffer()
    buffer.audio(b"")
    buffer.context("")
    assert buffer.dropped == (0, 0)


async def test_discard_is_for_a_call_that_ended_first():
    buffer = StartupBuffer()
    buffer.audio(b"words")
    buffer.discard()
    assert buffer.holding is False
    assert await buffer.release(send_audio=lambda p: None) == (0, 0)


def test_the_defaults_are_bounded():
    assert 0 < MAX_PENDING_AUDIO <= 1000
    assert 0 < MAX_PENDING_CONTEXT <= 100
