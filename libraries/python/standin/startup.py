# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What arrived before your agent was ready to hear it.

A call starts the moment StandIn dials. Your agent starts a little later: a
socket has to open, a room has to be joined, a session has to be configured.
Everything the caller does in that gap still arrives, and if nothing holds it,
it is gone.

Two things arrive in that gap and both matter:

**The caller's first words.** People start talking the instant the call
connects, and often the first thing they say is the reason they called. Drop it
and the agent opens by asking a question that was already answered.

**The first context.** The "there are three people here, stay quiet unless
addressed" sentence and the recording-status change both land within the first
moment of a meeting. Drop those and a group-call gate never engages and a
recording gate never opens.

This exists because every plugin was solving half of it. Of the nine plugins in
this SDK, two held both, and five held one and silently lost the other. Which
half each one lost was an accident of who wrote it.

Bounded on purpose. A socket that never opens must not grow a buffer for the
length of the call, so the oldest entries are dropped rather than the newest:
if something has to be lost, lose the stale audio and keep what just happened.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable

__all__ = ["MAX_PENDING_AUDIO", "MAX_PENDING_CONTEXT", "StartupBuffer"]

#: About four seconds of speech at the wire's frame size. Enough to hold an
#: opening sentence, far short of enough to hide a socket that never opened.
MAX_PENDING_AUDIO = 200

#: Context sentences are rare and each one is small. This is generous.
MAX_PENDING_CONTEXT = 20


class StartupBuffer:
    """Holds caller audio and call context until the agent can take them.

    Used by a handler whose provider needs setting up::

        async def on_start(self, session):
            agent = await connect()                      # the gap
            await self._pending.release(
                send_audio=agent.send_audio,
                send_context=agent.send_context,
            )

        async def on_caller_audio(self, pcm):
            if self._pending.holding:
                self._pending.audio(pcm)
            else:
                self._agent.send_audio(pcm)

    Order is preserved within each lane, and audio is released before context,
    because the provider needs the caller's words in the order they were said
    and the context is a note about the call rather than part of the
    conversation.
    """

    def __init__(
        self,
        max_audio: int = MAX_PENDING_AUDIO,
        max_context: int = MAX_PENDING_CONTEXT,
    ) -> None:
        self._audio: deque[bytes] = deque(maxlen=max(1, max_audio))
        self._context: deque[str] = deque(maxlen=max(1, max_context))
        self._holding = True
        self._dropped_audio = 0
        self._dropped_context = 0

    @property
    def holding(self) -> bool:
        """Whether the agent is still being set up."""
        return self._holding

    @property
    def dropped(self) -> tuple[int, int]:
        """How much was lost to the bounds, as (audio frames, context lines).

        Worth logging when it is not zero: it means the agent took long enough
        to start that the caller outran it.
        """
        return self._dropped_audio, self._dropped_context

    def audio(self, pcm: bytes) -> None:
        """Hold one frame of the caller's voice."""
        if not pcm:
            return
        if len(self._audio) == self._audio.maxlen:
            self._dropped_audio += 1
        self._audio.append(pcm)

    def context(self, text: str) -> None:
        """Hold one line of call context."""
        if not text:
            return
        if len(self._context) == self._context.maxlen:
            self._dropped_context += 1
        self._context.append(text)

    async def release(
        self,
        send_audio: Callable[[bytes], Awaitable[None] | None] | None = None,
        send_context: Callable[[str], Awaitable[None] | None] | None = None,
    ) -> tuple[int, int]:
        """Hand everything over, in order, and stop holding.

        Returns what was released, as (audio frames, context lines). Both
        callbacks may be sync or async, because a provider's send is often
        fire-and-forget.

        Safe to call twice: the second call releases nothing.
        """
        self._holding = False
        released_audio = 0
        released_context = 0
        while self._audio:
            frame = self._audio.popleft()
            if send_audio is not None:
                result = send_audio(frame)
                if result is not None and hasattr(result, "__await__"):
                    await result
            released_audio += 1
        while self._context:
            line = self._context.popleft()
            if send_context is not None:
                result = send_context(line)
                if result is not None and hasattr(result, "__await__"):
                    await result
            released_context += 1
        return released_audio, released_context

    def discard(self) -> None:
        """Throw it away. For a call that ended before the agent was ready."""
        self._holding = False
        self._audio.clear()
        self._context.clear()
