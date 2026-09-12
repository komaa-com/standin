# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Putting your agent's own face on the bot's video tile.

StandIn renders an avatar by default. When your agent already produces video of
its own - an avatar worker, a rendered face, a camera - this streams that onto
the tile instead, as a continuous run of ``display.frame`` messages.

The awkward part is not sending frames. It is sending them at a rate that does
not hurt the call, and that is what :class:`TileStream` owns:

**Latest wins, and each frame goes at most once.** Frames are offered into a
single slot, never a queue. A ticker takes whatever is newest and sends it. That
means a source producing faster than the wire drops the middle frames rather
than falling behind, and a source that STOPS producing goes quiet rather than
repeating one stale frame forever. Silence is how a stream ends.

**The timestamp is the audio clock.** ``ts`` comes from
:attr:`~standin.CallSession.media_time_ms`, the same timeline the outbound audio
is stamped on. A wall clock keeps ticking through listening silence while the
audio clock does not, so a wall-clock stamp makes the two streams look like they
are drifting apart when they are in step.

**Video yields to audio.** The budget here is tighter than the audio one on
purpose. Both streams share a socket, and a caller forgives a dropped frame far
more readily than a break in the voice, so a loaded path goes quiet promptly
rather than building up seconds of skew.

Encoding is yours to supply or ours to find. Pass an ``encoder`` and the stream
uses it; pass nothing and it looks for Pillow. If neither is there, the tile
relay stays off with one line in the log and **the call is unaffected** - the
caller hears the agent and sees StandIn's own avatar, which is what they would
have seen anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

from .handler import CallSession
from .log import logger

__all__ = ["MAX_TILE_FPS", "TILE_HEIGHT", "TILE_WIDTH", "TileStream", "jpeg_encoder"]

#: The tile size frames are encoded to. Shipping an avatar's native resolution
#: only spends bandwidth on pixels the tile will not show.
TILE_WIDTH = 640
TILE_HEIGHT = 360

#: Encoder quality. Chosen where a talking head still looks right and the frame
#: still fits comfortably inside the wire envelope.
_JPEG_QUALITY = 58

#: A sender-side sanity clamp, not a protocol limit. A talking-head tile gains
#: nothing above this, and a higher rate only spends local CPU on encoding and
#: base64.
MAX_TILE_FPS = 20

#: Tighter than the audio buffer cap, deliberately. See the module docstring.
_VIDEO_BACKPRESSURE_BYTES = 320 * 1024

#: What an encoder is: packed RGB in, JPEG bytes out.
Encoder = Callable[[bytes, int, int], bytes]


def jpeg_encoder() -> Encoder | None:
    """Find an encoder, or return ``None`` having said why.

    Pillow is an optional extra precisely because most deployments never put
    their own video on the tile. A missing encoder is a tile relay that does not
    run, which is a smaller problem than a dependency every install pays for.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning(
            "standin: the avatar tile relay needs Pillow to encode frames "
            '(pip install "standin-sdk[tile]"); the relay is off and audio is unaffected'
        )
        return None

    def encode(rgb: bytes, width: int, height: int) -> bytes:
        import io

        image = Image.frombytes("RGB", (width, height), rgb)
        if (width, height) != (TILE_WIDTH, TILE_HEIGHT):
            image = image.resize((TILE_WIDTH, TILE_HEIGHT))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
        return buffer.getvalue()

    return encode


class TileStream:
    """A paced run of ``display.frame`` messages onto the bot's video tile.

    Built by a plugin that has video, driven by whatever produces it::

        tile = TileStream(session)
        await tile.start()
        ...
        tile.offer_rgb(rgb_bytes, width, height)   # as often as you like
        ...
        await tile.aclose()

    :meth:`offer_rgb` and :meth:`offer_jpeg` never block and never raise. They
    are meant to be called from a decode loop that must not be slowed down by
    the wire.
    """

    def __init__(
        self,
        session: CallSession,
        fps: int = 12,
        encoder: Encoder | None = None,
        max_buffered_bytes: int = _VIDEO_BACKPRESSURE_BYTES,
    ) -> None:
        self._session = session
        self._fps = max(1, min(int(fps), MAX_TILE_FPS))
        self._period = 1.0 / self._fps
        self._encoder = encoder
        self._resolved_encoder = False
        self._max_buffered = max_buffered_bytes
        #: The single newest frame awaiting a send. Never a queue.
        self._latest: tuple[bytes, int, int, bool] | None = None
        self._seq = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._dropped = 0
        self._last_drop_log = 0.0

    @property
    def frames_sent(self) -> int:
        """How many frames have reached the tile."""
        return self._seq

    @property
    def frames_dropped(self) -> int:
        """How many were dropped for backpressure. A healthy call has some."""
        return self._dropped

    def offer_rgb(self, rgb: bytes, width: int, height: int) -> None:
        """Offer packed RGB. Replaces whatever was waiting."""
        if not self._closed and rgb:
            self._latest = (rgb, width, height, False)

    def offer_jpeg(self, jpeg: bytes, width: int = TILE_WIDTH, height: int = TILE_HEIGHT) -> None:
        """Offer an already-encoded frame, skipping the encoder entirely.

        For a source that hands you JPEG already. Nothing is re-encoded, and no
        encoder needs to be installed.
        """
        if not self._closed and jpeg:
            self._latest = (jpeg, width, height, True)

    async def start(self) -> None:
        """Begin sending. Returns immediately; the pacing runs in the background."""
        if self._task is not None or self._closed:
            return
        if self._encoder is None and not self._resolved_encoder:
            self._resolved_encoder = True
            self._encoder = jpeg_encoder()
        self._task = asyncio.ensure_future(self._run())
        logger.info(
            "standin: avatar tile relay armed at %d fps, %dx%d",
            self._fps,
            TILE_WIDTH,
            TILE_HEIGHT,
        )

    async def aclose(self) -> None:
        """Stop sending. Safe to call twice, and on every teardown path."""
        self._closed = True
        self._latest = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while not self._closed:
            next_tick += self._period
            delay = next_tick - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Behind schedule: resync rather than sprinting to catch up,
                # which would burst frames at a caller who is already congested.
                next_tick = loop.time()
            with contextlib.suppress(Exception):
                await self._tick()

    async def _tick(self) -> None:
        frame = self._latest
        if frame is None:
            return
        # Consume the slot. Each offered frame is sent at most once, so a source
        # that goes quiet leaves a silent wire rather than a frozen repeat.
        self._latest = None

        if self._over_budget():
            return
        data, width, height, already_jpeg = frame
        if not already_jpeg:
            encoder = self._encoder
            if encoder is None:
                return
            try:
                data = await asyncio.get_running_loop().run_in_executor(
                    None, encoder, data, width, height
                )
            except Exception as err:
                logger.warning("standin: encoding an avatar frame failed: %s", err)
                return
            width, height = TILE_WIDTH, TILE_HEIGHT
            # Re-check after the encode yielded: audio may have filled the
            # socket while we were off the loop, and a video frame must not be
            # what starves the voice.
            if self._over_budget():
                return

        self._seq += 1
        with contextlib.suppress(Exception):
            # The session owns the sequence and the timestamp, the same way it
            # owns them for audio, so the two streams share one clock.
            await self._session.send_tile_frame(data, width, height)

    def _over_budget(self) -> bool:
        try:
            buffered = self._session.buffered_bytes
        except Exception:
            buffered = 0
        if buffered <= self._max_buffered:
            return False
        self._dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log >= 5:
            logger.info(
                "standin: avatar tile is dropping frames to protect the audio (%d so far)",
                self._dropped,
            )
            self._last_drop_log = now
        return True
