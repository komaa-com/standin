# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Showing the model what the caller is showing, without being asked.

:class:`~standin.vision_tools.VisionTools` answers when a model reaches for
``look``. That is the right shape most of the time, and it has a blind spot: the
model has to know there is something to look at. Somebody who shares a deck and
says "what do you think?" has told it nothing it can act on.

Ambient vision closes that by pushing what changed on screen into the
conversation between turns. It is OFF unless a plugin turns it on, because it
spends money on every scene change and not every deployment wants that.

Three things keep it from being expensive or creepy:

**A recording gate**, checked before a frame is even stored. Sending somebody's
screen to a model continuously is a different promise from glancing at it once,
and the recording is what told them their call is being kept.

**Change detection**, so a screen nobody touched costs nothing. The latch is the
digest of the last frame actually DELIVERED for that source, not the last one
seen: a delivery that failed has to be retried, not skipped.

**A reserve**, so the ambient lane cannot spend the whole budget and leave the
caller's own "look at this" with nothing left.

What to DO with an image stays in the plugin, because only it knows how to hand
one to its provider without forcing a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .log import logger
from .vision import VideoFrame, fallback_owner, frame_caption, frame_digest, frame_owner
from .vision_tools import VisionBudget

__all__ = [
    "AMBIENT_BACKSTOP_MS",
    "AMBIENT_SOURCE_ORDER",
    "DEFAULT_AMBIENT_MAX_PER_MINUTE",
    "MAX_QUEUED_AMBIENT_IMAGES",
    "AmbientImage",
    "AmbientSink",
    "AmbientVision",
]

#: How often to look again even when no frame arrived. A screen share can go
#: quiet without ending, and the last thing on it is still what is being
#: discussed.
AMBIENT_BACKSTOP_MS = 6_000

#: Images held while the provider's socket is not up yet. Bounded: a sink that
#: never comes up would otherwise hold the whole call's video.
MAX_QUEUED_AMBIENT_IMAGES = 6

#: Screen share first. Somebody presenting is nearly always talking about the
#: screen rather than about their face.
AMBIENT_SOURCE_ORDER = ("screenshare", "camera")

#: The default ceiling when a plugin gives no budget of its own.
DEFAULT_AMBIENT_MAX_PER_MINUTE = 30


@dataclass(frozen=True)
class AmbientImage:
    """One frame, ready to hand to a provider, with who it came from."""

    source: str
    mime: str
    data_base64: str
    width: int
    height: int
    ts: int
    owner: str
    """Who is showing it. Degrades to "the caller" rather than vanishing."""

    caption: str
    """A sentence to put beside the image, so the model knows whose screen it is."""

    @property
    def data_url(self) -> str:
        return f"data:{self.mime};base64,{self.data_base64}"


#: Hand one image to the provider.
#:
#: It MUST NOT make the agent reply: ambient vision is context, and an agent
#: that answers every scene change talks over the person presenting. It MUST
#: raise on failure, because a silent failure latches a frame that never
#: arrived and the model never sees that screen again.
AmbientSink = Callable[[AmbientImage], Awaitable[None]]


class AmbientVision:
    """Push what changed on screen into the conversation, between turns.

    Built by a plugin that knows how to deliver an image without forcing a
    reply::

        ambient = AmbientVision(session, self._push_image, enabled=True, budget=budget)
        ...
        async def on_video_frame(self, frame): ambient.offer(frame)

    :meth:`offer` is synchronous and never blocks: it runs on the receive path
    of a live call, and the vision work happens off it.
    """

    def __init__(
        self,
        session: object,
        deliver: AmbientSink,
        *,
        enabled: bool = False,
        budget: VisionBudget | None = None,
        require_recording: bool = True,
        sink_ready: Callable[[], bool] | None = None,
        change_key: Callable[[VideoFrame], str] | None = None,
        on_delivered: Callable[[AmbientImage], None] | None = None,
        backstop_ms: int = AMBIENT_BACKSTOP_MS,
        queue_max: int = MAX_QUEUED_AMBIENT_IMAGES,
    ) -> None:
        self._session = session
        self._deliver = deliver
        self._enabled = enabled
        self._require_recording = require_recording
        self._sink_ready = sink_ready
        self._change_key = change_key or (lambda frame: frame_digest(frame.data_base64))
        self._on_delivered = on_delivered
        self._backstop_ms = max(500, backstop_ms)
        self._queue_max = max(1, queue_max)

        if budget is None:
            self._budget = VisionBudget(max_per_minute=DEFAULT_AMBIENT_MAX_PER_MINUTE)
            logger.debug(
                "standin: ambient vision has its own budget, so ambient and explicit looks "
                "are capped separately"
            )
        else:
            self._budget = budget
            if enabled and budget.max_per_minute == 0:
                logger.warning(
                    "standin: ambient vision is on with an uncapped budget; every scene "
                    "change will be charged. Set max_per_minute, or leave enabled off."
                )

        self._latest: dict[str, VideoFrame] = {}
        self._latched: dict[str, str] = {}
        self._queue: list[AmbientImage] = []
        self._flushing = False
        self._dirty = False
        self._released = False
        self._delivered = 0
        self._said_holding = False
        self._backstop: asyncio.Task[None] | None = None

    @property
    def queued(self) -> int:
        """Images held because the provider was not ready for them."""
        return len(self._queue)

    @property
    def delivered(self) -> int:
        return self._delivered

    def offer(self, frame: VideoFrame) -> None:
        """Take one frame. Synchronous, non-blocking, and never raises."""
        if not self._enabled or self._released:
            return
        if self._require_recording and not getattr(self._session, "recording_active", False):
            # Gated before it is STORED, not just before it is sent. Keeping a
            # frame captured while the gate was shut would let opening the gate
            # surface something from before the caller was told.
            return
        self._latest[frame.source] = frame
        self._arm_backstop()
        self.flush()

    def flush(self) -> None:
        """Look now. Idempotent, and safe to call from anywhere."""
        if not self._enabled or self._released:
            return
        if self._flushing:
            # One more pass after this one, rather than two at once.
            self._dirty = True
            return
        self._flushing = True
        asyncio.ensure_future(self._run())

    async def aclose(self) -> None:
        """Stop, and stay stopped."""
        self._released = True
        self._latest.clear()
        self._latched.clear()
        self._queue.clear()
        if self._backstop is not None:
            self._backstop.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._backstop
            self._backstop = None

    # ---- the pass --------------------------------------------------------

    async def _run(self) -> None:
        try:
            while True:
                await self._pass()
                if not self._dirty or self._released:
                    return
                self._dirty = False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: the ambient vision pass failed")
        finally:
            self._flushing = False

    async def _pass(self) -> None:
        if self._released:
            return
        ready = self._sink_ready is None or self._sink_ready()
        if ready and self._queue:
            # What was held goes first, oldest first: the model should see the
            # screen change in the order it happened.
            await self._drain()

        for source in AMBIENT_SOURCE_ORDER:
            if self._released:
                return
            frame = self._latest.get(source)
            if frame is None:
                continue
            key = self._change_key(frame)
            if self._latched.get(source) == key:
                # Nothing changed. A frozen screen costs nothing.
                continue

            token = self._budget.try_consume_ambient()
            if token is None:
                # Out of the ambient allowance. Stop the pass rather than
                # trying the next source, which would spend the same exhausted
                # budget.
                return

            image = self._image(frame)
            if not ready:
                # Charged and latched: it is going to be sent, just not yet.
                # Refunding here would re-send the same screen when the sink
                # comes up.
                self._latched[source] = key
                self._hold(image)
                continue
            try:
                await self._deliver(image)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # The latch is untouched, so the same screen is tried again.
                self._budget.refund(token)
                logger.debug("standin: an ambient frame did not reach the model: %s", err)
                continue
            if self._released:
                return
            self._latched[source] = key
            self._delivered += 1
            if self._on_delivered is not None:
                with contextlib.suppress(Exception):
                    self._on_delivered(image)

    async def _drain(self) -> None:
        held, self._queue = self._queue, []
        for image in held:
            if self._released:
                return
            try:
                await self._deliver(image)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # Already charged and already latched. Dropped rather than
                # re-queued, or a dead sink grows the queue for ever.
                logger.debug("standin: a held ambient frame was dropped: %s", err)
                continue
            self._delivered += 1
            if self._on_delivered is not None:
                with contextlib.suppress(Exception):
                    self._on_delivered(image)

    def _hold(self, image: AmbientImage) -> None:
        if not self._said_holding:
            logger.info("standin: holding ambient frames until the provider is ready")
            self._said_holding = True
        self._queue.append(image)
        # Oldest out: what is on screen NOW is worth more than what was.
        del self._queue[: -self._queue_max]

    def _image(self, frame: VideoFrame) -> AmbientImage:
        owner = frame_owner(frame) or fallback_owner(frame.source)
        return AmbientImage(
            source=frame.source,
            mime=frame.mime,
            data_base64=frame.data_base64,
            width=frame.width,
            height=frame.height,
            ts=frame.ts,
            owner=owner,
            caption=frame_caption(owner),
        )

    def _arm_backstop(self) -> None:
        """Armed on the first accepted frame, so a call with no video has no timer."""
        if self._backstop is None and not self._released:
            self._backstop = asyncio.ensure_future(self._tick())

    async def _tick(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while not self._released:
                await asyncio.sleep(self._backstop_ms / 1000)
                self.flush()
