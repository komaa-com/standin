# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What an agent can do about what it can see, and what it shows back.

:mod:`standin.vision` is the lane: frames in, images out. This is the layer a
model actually reaches for, and it exists in the core rather than in one plugin
because every provider wants the same five things and none of them wants to
write the guards again:

``look``
    Answer a question about what the caller is showing.
``show``
    Put an image you already have on the bot's tile.
``show_url``
    Put an image from a URL the model chose on the tile.
``show_file``
    Put a local document on the tile. See :mod:`standin.render`.
``walkthrough``
    Talk through several of those in order, pausing for each.

None of these raise at a model. Every one returns a sentence, because the caller
is a tool result being read back to something that will say it out loud, and an
exception there is a silent tool and a confused agent. "I could not do that
because X" is worth more to a model than a traceback.

Two guards travel with them, and both are here rather than in a plugin because
they are the parts that are easy to get wrong:

:class:`VisionBudget`
    A model that can look can look in a loop. The budget is what stops one call
    spending an afternoon of inference.
:class:`KeyframeStore`
    Recent frames, so "what did that slide say?" can be answered about a slide
    that is already gone. Gated on the call being recorded, because keeping a
    history of somebody's screen is a different promise from looking at it once.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from .handler import CallSession
from .log import logger
from .vision import DISPLAY_IMAGE_MIME_TYPES, MAX_IMAGE_BYTES, FrameDescriber, VideoFrame

__all__ = [
    "PageRenderer",
    "PAGE_RENDER_TIMEOUT_S",
    "PAGE_DISPLAY_MS",
    "DISPLAY_MODES",
    "MAX_SLIDESHOW_IMAGES",
    "SLIDESHOW_HOLD_MS",
    "SLIDESHOW_OVERLAP_MS",
    "KeyframeStore",
    "ShowItem",
    "ShownImage",
    "display_image_name",
    "normalize_display_mode",
    "VisionBudget",
    "VisionTools",
    "WalkthroughStep",
]

#: How long an agent-supplied URL has to produce an image.
_FETCH_TIMEOUT_MS = 10_000

#: How long a page has to render before the caller is told it did not.
PAGE_RENDER_TIMEOUT_S = 45.0

#: How long a rendered page stays on the tile. Longer than a chart: a page is
#: read rather than glanced at.
PAGE_DISPLAY_MS = 15_000

#: A URL makes a poor caption at any length, and a long one makes a worse one.
_MAX_PAGE_CAPTION_CHARS = 80

#: Renders one page to bytes plus a mime type. Supplied by a plugin whose host
#: already runs a browser; the core never gains one.
PageRenderer = Callable[[str], Awaitable[tuple[bytes | str, str]]]

#: A caption reaches the caller's screen, and a model's strings are as long as
#: whoever is steering it wants them to be.
_MAX_CAPTION_CHARS = 200

#: How a picture sits on the tile. ``fullscreen`` replaces it; ``overlay`` draws
#: an inset over the live avatar.
#:
#: The model chooses, per picture. An inset is unreadable for a dense screenshot
#: or a page of a document, which is exactly when somebody says "show me", and
#: hardcoding either one is wrong for half of what an agent shows.
DISPLAY_MODES = ("fullscreen", "overlay")

#: How long each picture of a slideshow stays up.
SLIDESHOW_HOLD_MS = 4_000

#: Sent as the duration on every non-final frame, so the next one arrives before
#: the last has expired and the tile never blanks between pictures.
SLIDESHOW_OVERLAP_MS = 500

#: A model asked for "the slides" can mean forty of them.
MAX_SLIDESHOW_IMAGES = 10

#: Bounds on how long one picture may be held.
_MIN_HOLD_MS = 1_000
_MAX_HOLD_MS = 30_000

#: A name that is safe to show. Anything else becomes image.<ext>.
_SAFE_NAME = re.compile(r"^[\w.-]{1,80}\.[A-Za-z0-9]{2,5}$")


def normalize_display_mode(value: object, default: str | None = None) -> str | None:
    """The display mode this value means, or the default.

    One rule, shared by both SDKs and every plugin, because the value comes
    from a model: ``"pip"``, ``"inset"``, ``"full"`` and ``None`` are all things
    a model will say, and none of them is a mode.
    """
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in DISPLAY_MODES:
            return cleaned
    return default


def display_image_name(path_or_url: str, mime: str) -> str:
    """A filename to show beside a picture.

    Taken from the source when it looks like a filename and nothing else. The
    string came from a model steered by whoever is on the call, and it is about
    to be shown to them.
    """
    raw = str(path_or_url or "").split("?")[0].split("#")[0]
    candidate = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if _SAFE_NAME.match(candidate):
        return candidate
    subtype = (mime or "").rsplit("/", 1)[-1] or "bin"
    return f"image.{'jpg' if subtype == 'jpeg' else subtype}"


@dataclass(frozen=True)
class ShownImage:
    """The last picture the caller actually saw."""

    image: bytes | str
    mime: str
    name: str
    at_ms: int

    def as_base64(self) -> str:
        if isinstance(self.image, str):
            return self.image
        return base64.b64encode(self.image).decode("ascii")


@dataclass(frozen=True)
class ShowItem:
    """One picture in a slideshow. Bytes, base64, or an https URL."""

    image: bytes | str
    mime: str = "image/jpeg"
    name: str | None = None


@dataclass
class VisionBudget:
    """A ceiling on how often one call may spend on vision.

    A model that can look can look in a loop, and each look is a paid inference
    over somebody's screen. This is a sliding window rather than a total, so a
    long call is not punished for having been long.

    Spending returns a token and refunding takes that token back. That matters
    more than it looks: two tool calls can overlap, and a refund that simply
    dropped "the most recent charge" would refund the wrong one and let the
    budget drift upward under exactly the load it exists to bound.
    """

    max_per_minute: int = 6
    """Looks allowed in any rolling minute. Zero means no ceiling, which is a
    choice rather than a default."""

    _spent: dict[str, float] = field(default_factory=dict, repr=False)

    def try_consume(self) -> str | None:
        """Take one look's worth of budget, or ``None`` when there is none left.

        The returned token is what :meth:`refund` needs if the look fails.
        """
        if not self.max_per_minute:
            return uuid.uuid4().hex
        now = time.monotonic()
        cutoff = now - 60
        self._spent = {token: at for token, at in self._spent.items() if at > cutoff}
        if len(self._spent) >= self.max_per_minute:
            return None
        token = uuid.uuid4().hex
        self._spent[token] = now
        return token

    @property
    def reserve(self) -> int:
        """How much of the window only an explicit look may spend.

        Ambient vision spends on every scene change, which is exactly the load
        that would leave a caller's own "look at this" with nothing left. The
        reserve is what the ambient lane cannot touch.
        """
        if not self.max_per_minute:
            return 0
        return max(2, self.max_per_minute // 4)

    def try_consume_ambient(self) -> str | None:
        """Take one look's worth, from the ambient lane only.

        Refused once the window is down to the reserve. Refunded through the
        same :meth:`refund`, with the same token, so a failed ambient push and a
        failed explicit look are given back the same way.
        """
        if not self.max_per_minute:
            return self.try_consume()
        if self.spent >= self.max_per_minute - self.reserve:
            return None
        return self.try_consume()

    def refund(self, token: str) -> None:
        """Give back a charge whose look never happened. Idempotent."""
        self._spent.pop(token, None)

    @property
    def spent(self) -> int:
        """Looks charged in the current window."""
        cutoff = time.monotonic() - 60
        return sum(1 for at in self._spent.values() if at > cutoff)


class KeyframeStore:
    """A short history of what the caller showed.

    The call session keeps the LATEST frame per source, which answers "what am I
    looking at now". This answers "what was on that slide a moment ago", which is
    what somebody actually asks after they have moved on.

    Bounded, and **gated on the call being recorded**. Keeping a history of
    somebody's screen is a materially different promise from glancing at it once,
    and the recording is the thing that told them their call is being kept.
    """

    def __init__(self, capacity: int = 16) -> None:
        self._capacity = max(1, capacity)
        self._frames: list[VideoFrame] = []
        self._last: dict[str, str] = {}

    def offer(self, frame: VideoFrame, recording: bool) -> bool:
        """Keep this frame, if the call is being recorded. Returns whether it was kept."""
        if not recording:
            return False
        # Per source, so an alternating camera and screen share each keep their
        # own history. A screen nobody touched would otherwise fill the whole
        # store with one picture.
        from .vision import frame_digest

        digest = frame_digest(frame.data_base64)
        if self._last.get(frame.source) == digest:
            return False
        self._last[frame.source] = digest
        self._frames.append(frame)
        del self._frames[: -self._capacity]
        return True

    def recent(self, source: str | None = None) -> list[VideoFrame]:
        """Frames kept so far, oldest first."""
        if source is None:
            return list(self._frames)
        return [f for f in self._frames if f.source == source]

    def clear(self) -> None:
        """Forget everything. Called on teardown."""
        self._frames.clear()
        self._last.clear()

    def __len__(self) -> int:
        return len(self._frames)


@dataclass(frozen=True)
class WalkthroughStep:
    """One beat of a walkthrough: something to say, optionally something to show."""

    say: str
    """The line spoken before the image appears."""

    image: bytes | str | None = None
    mime: str = "image/jpeg"
    caption: str | None = None


#: Says one line and returns when the caller has heard it. Supplied by the
#: plugin, because "finished speaking" is a thing only the provider knows.
Speaker = Callable[[str], Awaitable[None]]


class VisionTools:
    """The five capabilities, bound to one call.

    Built by a plugin once per call and reached from whatever tool surface that
    provider has::

        tools = VisionTools(session, describer=FrameDescriber.from_env())
        answer = await tools.look("What is on the slide?")

    Every method returns a sentence for a model to read out, including when it
    failed. None of them raise.
    """

    def __init__(
        self,
        session: CallSession,
        describer: FrameDescriber | None = None,
        budget: VisionBudget | None = None,
        keyframes: KeyframeStore | None = None,
        default_display_mode: str | None = None,
    ) -> None:
        self._session = session
        self._describer = describer
        self._budget = budget or VisionBudget()
        self._keyframes = keyframes or KeyframeStore()
        #: What to use when the model says nothing. ``None`` sends no mode at
        #: all, so the service's own default applies rather than one chosen
        #: here.
        self._default_display = normalize_display_mode(default_display_mode)
        self._last_shown: ShownImage | None = None
        self._slideshow: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._generation = 0

    @property
    def keyframes(self) -> KeyframeStore:
        """The frame history. Feed it from ``on_video_frame``."""
        return self._keyframes

    @property
    def budget(self) -> VisionBudget:
        return self._budget

    @property
    def last_shown(self) -> ShownImage | None:
        """The picture the caller can see, if any.

        Recorded only after a send actually returned, so "send me that" attaches
        what they saw rather than what was attempted. One slot, replaced each
        time: a list would be a growing copy of everything shown on the call.
        """
        return self._last_shown

    async def reset(self) -> None:
        """Forget what was shown and stop any slideshow. Call this on teardown."""
        await self._stop_slideshow()
        self._last_shown = None
        self._keyframes.clear()

    # ---- looking ----------------------------------------------------------

    async def look(self, question: str = "", source: str | None = None) -> str:
        """Answer a question about what the caller is showing.

        Uses the newest frame, preferring the screen share, because an agent
        asked to look is nearly always being asked about what is being shown
        rather than who is showing it.
        """
        if self._describer is None:
            return (
                "looking is not available on this deployment: no vision model is configured "
                "(set STANDIN_VISION_API_URL and STANDIN_VISION_MODEL)"
            )
        wanted = source if source in ("camera", "screenshare") else None
        frame = self._session.latest_video_frame(wanted)
        if frame is None:
            return "there is nothing to look at: the caller is not sharing their camera or screen"

        token = self._budget.try_consume()
        if token is None:
            return (
                "this call has reached its limit on looking at the screen; "
                "ask the caller to describe what they are showing"
            )
        try:
            return await self._describer.describe(
                frame, question.strip() or "Describe what is visible."
            )
        except Exception as err:
            # The charge is given back, or a flaky vision endpoint silently
            # burns the budget the caller paid nothing for.
            self._budget.refund(token)
            return f"could not look at the screen: {err}"

    async def look_back(self, question: str = "") -> str:
        """Answer about a frame the caller has already moved past.

        Only possible when the call is being recorded, because that is the only
        time frames are kept at all.
        """
        frames = self._keyframes.recent()
        if not frames:
            if not self._session.recording_active:
                return (
                    "I can only look back at earlier screens while the call is being recorded, "
                    "and it is not"
                )
            return "nothing has been shown on this call yet"
        if self._describer is None:
            return "looking is not available on this deployment: no vision model is configured"

        token = self._budget.try_consume()
        if token is None:
            return "this call has reached its limit on looking at the screen"
        try:
            return await self._describer.describe(
                frames[-1], question.strip() or "Describe what was visible."
            )
        except Exception as err:
            self._budget.refund(token)
            return f"could not look back: {err}"

    # ---- showing ----------------------------------------------------------

    async def show(
        self,
        image: bytes | str,
        mime: str = "image/jpeg",
        caption: str | None = None,
        duration_ms: int | None = None,
        display: str | None = None,
        name: str | None = None,
    ) -> str:
        """Put an image on the bot's video tile.

        ``display`` is the model's choice of ``fullscreen`` or ``overlay``, and
        anything else falls back to the configured default. When neither is set
        the field is OMITTED rather than defaulted here, so the service decides.
        """
        if mime not in DISPLAY_IMAGE_MIME_TYPES:
            return f"that image is {mime}; it must be one of {', '.join(DISPLAY_IMAGE_MIME_TYPES)}"
        try:
            await self._session.display_image(
                image,
                mime,
                duration_ms=duration_ms,
                mode=normalize_display_mode(display, self._default_display),
                caption=caption[:_MAX_CAPTION_CHARS] if caption else None,
            )
        except ValueError as err:
            # The wire has a hard ceiling. A model must be told it in words, not
            # by an exception it cannot see.
            return f"could not show that: {err}"
        except Exception as err:
            return f"could not show that: {err}"
        # Only after it actually went. "Now send me that" must attach what the
        # caller saw, not what was attempted.
        self._last_shown = ShownImage(
            image=image,
            mime=mime,
            name=name or display_image_name("", mime),
            at_ms=int(time.time() * 1000),
        )
        return "the caller can see it"

    async def show_url(
        self, url: str, caption: str | None = None, display: str | None = None
    ) -> str:
        """Fetch an image the model chose, and show it.

        The URL comes from a model steered by whoever is on the call, so it goes
        through the SDK's guard: public hosts only, and the address re-checked at
        connect time.
        """
        if not url.strip():
            return "that needs a public https URL of a jpeg or png"
        from .fetch import fetch_public_image

        try:
            image, mime = await fetch_public_image(url, MAX_IMAGE_BYTES, _FETCH_TIMEOUT_MS)
        except Exception as err:
            return f"could not fetch that image: {err}"
        return await self.show(
            image, mime, caption, display=display, name=display_image_name(url, mime)
        )

    async def show_file(
        self, path: str, page: int = 1, caption: str | None = None, display: str | None = None
    ) -> str:
        """Put a local document on the tile: an image, a PDF page, an Office page.

        Rendering lives in :mod:`standin.render`, which is optional. Without it
        this says so rather than failing quietly.
        """
        from .render import render_file

        try:
            image, mime = await render_file(path, page=page)
        except Exception as err:
            return f"could not show that file: {err}"
        return await self.show(
            image,
            mime,
            caption or _file_caption(path, page),
            display=display,
            name=display_image_name(path, mime),
        )

    async def show_page(
        self,
        url: str,
        caption: str | None = None,
        render: PageRenderer | None = None,
        timeout_s: float = PAGE_RENDER_TIMEOUT_S,
    ) -> str:
        """Put a web page on the tile, as a picture of it.

        The core has no browser and must never gain one. ``render`` is supplied
        by a plugin whose host already runs one, and it returns bytes rather
        than a path: reading a file chosen downstream of whoever is on the call
        is not a primitive this belongs in.

        The guard runs HERE, before the renderer is reached, and it runs even
        when that renderer is a browser advertising private-network protection
        of its own. Such a browser assumes whoever wrote the URL already has a
        shell on the machine. Here the URL was written by a model being steered
        by a stranger, which is exactly the case that relaxation lets through.
        """
        if not url.strip():
            return "that needs a public https URL of a page"
        if render is None:
            return "showing web pages is not available on this deployment"
        from .fetch import assert_public_http_url

        try:
            await assert_public_http_url(url)
        except Exception as err:
            return f"could not open that page: {err}"
        try:
            image, mime = await asyncio.wait_for(render(url), timeout_s)
        except TimeoutError:
            # Never "within 0 seconds", and never "1 seconds": this sentence
            # is read out loud to the person waiting for the page.
            seconds = max(1, round(timeout_s))
            return (
                f"that page did not finish loading within {seconds} "
                f"{'second' if seconds == 1 else 'seconds'}"
            )
        except Exception as err:
            return f"could not open that page: {err}"
        return await self.show(
            image,
            mime,
            caption or url.strip()[:_MAX_PAGE_CAPTION_CHARS],
            duration_ms=PAGE_DISPLAY_MS,
            name=display_image_name(url, mime),
        )

    async def show_many(
        self,
        items: Sequence[ShowItem],
        caption: str | None = None,
        display: str | None = None,
        hold_ms: int = SLIDESHOW_HOLD_MS,
    ) -> str:
        """Show several pictures in turn, without waiting for all of them.

        The FIRST one goes before this returns, so the model can say "here it
        is" and be right. The rest are paced from a detached task: a model that
        waits out a ten-picture slideshow before speaking leaves the caller in
        silence for most of a minute.

        Never raises. The sentence says what is on screen now and what follows.
        """
        if not items:
            return "there was nothing to show"
        await self._stop_slideshow()

        hold = max(_MIN_HOLD_MS, min(_MAX_HOLD_MS, hold_ms))
        shown = list(items[:MAX_SLIDESHOW_IMAGES])
        dropped = len(items) - len(shown)
        mode = normalize_display_mode(display, self._default_display)

        first = await self._show_item(shown[0], caption, mode, len(shown) > 1, hold)
        if first.startswith("could not"):
            return first

        if len(shown) > 1:
            self._generation += 1
            self._slideshow = asyncio.ensure_future(
                self._pace(shown[1:], mode, hold, self._generation)
            )
        of = f"the first of {len(shown)}" if len(shown) > 1 else "it"
        rest = f"; the rest follow every {hold // 1000} seconds" if len(shown) > 1 else ""
        extra = f" (showing the first {len(shown)} of {len(shown) + dropped})" if dropped else ""
        return f"the caller can see {of}{rest}{extra}"

    async def _show_item(
        self, item: ShowItem, caption: str | None, mode: str | None, more: bool, hold: int
    ) -> str:
        """One frame of a slideshow, loaded late and sent."""
        image, mime = item.image, item.mime
        if isinstance(image, str) and image.strip().lower().startswith(("http://", "https://")):
            from .fetch import fetch_public_image

            image, mime = await fetch_public_image(image, MAX_IMAGE_BYTES, _FETCH_TIMEOUT_MS)
        return await self.show(
            image,
            mime,
            caption,
            # Held a little past the pacing gap, so the tile never blanks
            # between pictures. The LAST frame omits it, so the service's own
            # default applies to what stays on screen.
            duration_ms=(hold + SLIDESHOW_OVERLAP_MS) if more else None,
            display=mode,
            name=item.name,
        )

    async def _pace(
        self, rest: Sequence[ShowItem], mode: str | None, hold: int, generation: int
    ) -> None:
        """Send the remaining frames, one hold apart.

        Each is loaded inside this loop rather than up front, so a slideshow of
        ten URLs does not fetch all ten before the first appears.
        """
        for index, item in enumerate(rest):
            await self._hold(hold)
            if generation != self._generation:
                return  # a newer slideshow, or reset()
            try:
                result = await self._show_item(item, None, mode, index < len(rest) - 1, hold)
            except Exception as err:
                logger.debug("standin: a slideshow frame was skipped: %s", err)
                continue
            if result.startswith("could not"):
                # One bad picture skips that picture, never the rest.
                logger.debug("standin: a slideshow frame was skipped: %s", result)

    async def _hold(self, ms: int) -> None:
        """The gap between two pictures, woken early when the slideshow is replaced.

        Without the wake, replacing a slideshow would wait out the old one's
        gap, up to thirty seconds, before the new first picture went up.
        """
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), ms / 1000)

    async def _stop_slideshow(self) -> None:
        self._generation += 1
        task, self._slideshow = self._slideshow, None
        if task is not None:
            # Woken rather than cancelled: the bumped generation is what stops
            # it, and a frame already being sent finishes being sent. Killing
            # one mid-write would leave half a picture on the wire.
            self._wake.set()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ---- pacing -----------------------------------------------------------

    async def walkthrough(
        self,
        steps: Sequence[WalkthroughStep],
        speak: Speaker,
        interrupted: Callable[[], bool] | None = None,
        display: str | None = None,
    ) -> str:
        """Say and show several things in order, pausing for each.

        The pacing is here; the SPEAKING is not. Only the provider knows when a
        line has finished being said, so ``speak`` is supplied by the plugin and
        awaited before the next beat begins. Without that, a walkthrough talks
        over itself.

        ``interrupted`` is checked between beats. A caller who cuts in should
        stop the tour, and the plugin is the only thing that knows they did.
        """
        if not steps:
            return "there was nothing to walk through"
        # One tile. A walkthrough and a slideshow running at once would fight
        # over it, and the caller would see neither properly.
        await self._stop_slideshow()
        shown = 0
        for index, step in enumerate(steps, start=1):
            if interrupted is not None and interrupted():
                return f"stopped after {shown} of {len(steps)}: the caller interrupted"
            try:
                await speak(step.say)
            except Exception as err:
                return f"stopped at step {index}: {err}"
            if step.image is not None:
                result = await self.show(step.image, step.mime, step.caption, display=display)
                if result.startswith("could not"):
                    return f"stopped at step {index}: {result}"
            shown = index
            # A beat between steps, so the caller sees each one rather than a
            # slideshow that outruns them.
            await asyncio.sleep(0)
        return f"walked through all {shown} steps"


def _file_caption(path: str, page: int) -> str:
    name = path.rsplit("/", 1)[-1]
    return f"{name} (page {page})" if page > 1 else name
