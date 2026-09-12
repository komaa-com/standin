# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The seam every StandIn plugin implements.

This is the whole contract between the SDK and an agent framework, and it is
deliberately five methods wide. :class:`~.call_server.CallServer` owns
everything that is the same for every framework - the socket StandIn dials, the
HMAC handshake and its replay guard, capacity and draining, the frame loop,
sequence numbers and the outbound audio timeline, and the two watchdogs that end
a call nobody closed. A plugin owns only the part that differs: what to do with
a caller's voice, and where the reply comes from.

That split is why a plugin is small. :mod:`standin.plugins.echo` is
under 100 lines and answers a real Microsoft Teams call.

Writing one:

    from standin import CallHandler, CallServer, CallSession

    class EchoHandler:
        async def on_start(self, session: CallSession) -> None:
            self._session = session

        async def on_caller_audio(self, pcm: bytes) -> None:
            await self._session.send_audio(pcm)      # echo it straight back

        async def on_context(self, text: str) -> None: ...
        async def on_goodbye(self, text: str) -> None: ...
        async def aclose(self, reason: str) -> None: ...

    server = CallServer(handler_factory=EchoHandler)
    await server.start()

Every method is optional in practice: :class:`CallHandler` is a
:class:`typing.Protocol`, and the server treats a missing method as a no-op, so
a handler that only wants audio implements only ``on_caller_audio``. Nothing
inherits from anything.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable

from .avatar import SpeechMark
from .protocol import SessionStart
from .vision import VideoFrame

__all__ = ["CallHandler", "CallSession", "HandlerFactory", "SpeakerHandler", "VideoHandler"]


class CallSession(Protocol):
    """One live Microsoft Teams call, as a handler sees it.

    Handed to :meth:`CallHandler.on_start` and valid until the call ends. The
    server owns the socket; this is the handler's only way to reach it.
    """

    @property
    def call_id(self) -> str:
        """StandIn's id for this call. Authenticated - it is the value the
        handshake HMAC signed, not something a caller supplied."""
        ...

    @property
    def start(self) -> SessionStart:
        """The ``session.start`` that opened the call: caller identity,
        direction, thread, recording status."""
        ...

    @property
    def recording_active(self) -> bool:
        """Whether the Microsoft Teams call is being recorded, right now.

        One flag, kept current by the server: it starts from
        ``session.start.recording_status`` and follows every later
        ``recording.status`` change, so a plugin never has to re-derive it from
        the context sentence it happens to have seen.

        A reported status WINS over the start snapshot, whichever arrives
        first. ``session.start`` omits the field when the state was unknown at
        answer time, and an omitted field is not "not recording": letting the
        snapshot overwrite a ``recording.status`` that landed first would shut
        every recording-gated capability for the whole call, silently.

        Gate on it before anything that STORES what the caller said or showed
        with a third party. A recorded call is one the caller was told is being
        kept; an unrecorded one is not.
        """
        ...

    @property
    def speaker(self) -> str | None:
        """Who is speaking now, when StandIn sends unmixed audio.

        ``None`` on the mixed path, which is most calls, so treat it as a
        hint rather than as something to depend on. Use it to attribute a
        transcript, or to tell a model who it is answering in a meeting.
        """
        ...

    @property
    def participant_count(self) -> int:
        """How many people are on the call.

        Zero until StandIn first says, and it says again whenever somebody
        joins or leaves. The number behind the "this is a 1:1 call" and "there
        are N human participants" context sentences, for a plugin that wants to
        branch on it rather than hand a sentence to a model.
        """
        ...

    @property
    def answered(self) -> bool:
        """Whether anything has actually taken this call yet."""
        ...

    def mark_answered(self) -> None:
        """Say that an agent has taken the call. Stamped once, never re-stamped.

        A plugin that joins a room calls this when the agent's own audio track
        appears, not when a participant connects: monitors, recorders and avatar
        workers all connect, and none of them is an agent answering. A plugin
        that never calls it is still covered, because sending audio counts.
        """
        ...

    @property
    def buffered_bytes(self) -> int:
        """How much outbound data the socket has not yet flushed.

        The number a continuous sender watches to decide whether to drop a
        frame. Audio and the avatar tile both push on a timer, and a peer that
        stops reading turns "send everything" into an unbounded buffer.

        Zero when the transport cannot report it, so treat a zero as "no
        evidence of backpressure" rather than as proof of an idle socket.
        """
        ...

    @property
    def media_time_ms(self) -> int:
        """The outbound audio timeline, in milliseconds.

        The same clock this call's ``audio.frame`` messages are stamped with,
        which is what a video frame must be stamped with too. A wall clock
        keeps ticking through listening silence while this one does not, so
        stamping video from a wall clock makes the audio and video drift apart
        on paper even when they are in step.
        """
        ...

    async def send_audio(self, pcm: bytes) -> None:
        """Send the agent's voice to the caller.

        ``pcm`` is raw PCM16, 16 kHz, mono, little-endian - the same format
        :meth:`CallHandler.on_caller_audio` receives. The server owns the
        sequence number and the outbound timeline, so a handler never tracks
        either, and a re-published or swapped audio source cannot make
        timestamps jump backwards.
        """
        ...

    async def cancel_playback(self) -> None:
        """Drop whatever agent audio StandIn still has buffered.

        The only lever that un-sends audio already handed to the service. Call
        it the moment your provider reports the caller started speaking, before
        you cancel the response upstream - otherwise a barge-in stops the model
        but the bot keeps talking for the length of the buffered PCM.
        """
        ...

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        """The most recent frame the caller showed, or ``None`` if they have
        shown nothing.

        Synchronous, because it reads a value the frame loop already stored:
        there is no waiting for a frame here, and a plugin that wants to
        know the moment one arrives implements
        :meth:`CallHandler.on_video_frame` instead.

        With no ``source`` the screen share wins over the camera, because an
        agent asked to look is nearly always being asked about what is being
        shown rather than who is showing it. Pass ``"camera"`` or
        ``"screenshare"`` to be explicit.
        """
        ...

    async def send_tile_frame(
        self, jpeg: bytes, width: int | None = None, height: int | None = None
    ) -> None:
        """Put one frame of continuous video on the bot's tile.

        The server owns the sequence number and stamps the frame with the
        outbound AUDIO timeline, exactly as it does for
        :meth:`send_audio`, so the two streams cannot disagree about what time
        it is.

        Latest wins and there is no handshake: the first frames start the
        stream and silence ends it. Pace it and drop under backpressure rather
        than queueing, which is what :class:`standin.tile.TileStream` is for.
        """
        ...

    async def display_image(
        self,
        image: bytes | str,
        mime: str = "image/jpeg",
        duration_ms: int | None = None,
        mode: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Draw an image on the bot's video tile for a few seconds.

        The agent's half of the vision lane: a chart it just computed, a page
        it is quoting, a photo it was asked for. Best-effort and additive, so a
        service that does not implement it ignores the message rather than
        failing the call.
        """
        ...

    async def express(self, emotion: str) -> None:
        """Hint the emotion the avatar should wear on the bot's tile.

        Best-effort and video only: an unknown emotion renders as neutral, and
        nothing here changes a sample of what the caller hears.
        """
        ...

    async def send_speech_marks(self, marks: Iterable[SpeechMark]) -> None:
        """Send the viseme timeline for one utterance, which is what drives
        lip-sync on the avatar.

        Real timings from your provider are best. Where there are none, which
        is every realtime speech-to-speech model, :mod:`standin.lipsync`
        estimates a timeline from the text and spreads it over the audio that
        turn actually sent: a mouth on a measured clock beats a still one. What
        is worse than none is a timeline whose DURATION was guessed, from text
        length or a words-per-minute rate, because that one drifts further out
        of step with the voice the longer it runs.
        """
        ...

    async def end(self, reason: str) -> None:
        """End the call. Idempotent, and the first reason wins - a cascade of
        close causes must not overwrite the one that actually ended it."""
        ...


@runtime_checkable
class CallHandler(Protocol):
    """What a plugin implements. One instance per call, built by a
    :data:`HandlerFactory`.

    Every method is awaited by the server and every one is optional: a missing
    method is a no-op. An exception raised from any of them is logged and ends
    that call alone - one bad call must never take the worker with it.
    """

    async def on_start(self, session: CallSession) -> None:
        """The call is live. Join a room, open a realtime socket, build an
        agent - whatever this framework needs. Audio does not flow until this
        returns, so a slow start delays the caller rather than dropping frames."""
        ...

    async def on_caller_audio(self, pcm: bytes) -> None:
        """One frame of the caller's voice: PCM16, 16 kHz, mono, little-endian.

        Called on the receive path of a live call, so it must not block. The
        frame has already been validated - a truncated or malformed payload is
        dropped by the server and never reaches here.
        """
        ...

    async def on_context(self, text: str) -> None:
        """Non-interrupting context about the call, as a plain sentence ready to
        put in front of a model: participant counts and group-call etiquette,
        DTMF key presses, and recording status changes.

        Delivered as it arrives. A framework that cannot accept context before
        its agent is ready should queue it here - the server does not, because
        what "ready" means is a framework's own business.
        """
        ...

    async def on_goodbye(self, text: str) -> None:
        """StandIn is ending the call and wants this line spoken first.

        The server has already told StandIn to drop whatever agent audio it had
        buffered, so this line plays immediately. Interrupt the current turn and
        say it: the teardown follows shortly, and a goodbye queued behind a long
        answer is a goodbye the caller never hears.
        """
        ...

    async def aclose(self, reason: str) -> None:
        """Release everything this call holds. Always called exactly once, on
        every path including cancellation, before the slot is freed."""
        ...


#: Builds one :class:`CallHandler` per call. Called with no arguments, so a
#: a plugin closes over its own configuration rather than threading it
#: through the
#: server.
HandlerFactory = Callable[[], Any]


@runtime_checkable
class SpeakerHandler(Protocol):
    """The optional callback: who is speaking changed.

    Kept off :class:`CallHandler` for the same reason as
    :class:`VideoHandler`: that protocol is
    :func:`~typing.runtime_checkable`, and a runtime check demands every member
    it declares. The server duck-types this one, so implementing it is enough
    and inheriting from it is never required.

    Only ever called when StandIn sends unmixed audio. Most calls carry mixed
    audio and never call it at all.
    """

    async def on_speaker_change(self, name: str) -> None:
        """A different person started speaking.

        Called on CHANGE only, never per frame. The name rides every inbound
        audio frame, so a model told forty times a second who is speaking would
        hear nothing else.

        Called on the receive path of a live call, so anything slow belongs off
        the frame loop, exactly as in :meth:`CallHandler.on_caller_audio`.
        """
        ...


@runtime_checkable
class VideoHandler(Protocol):
    """The optional sixth method: every frame of what the caller is showing.

    Kept off :class:`CallHandler` deliberately. That protocol is
    :func:`~typing.runtime_checkable`, and a runtime check demands every member
    it declares, so a sixth method there would mean an
    ``isinstance(handler, CallHandler)`` that fails for every handler written
    before the vision lane existed - while the documented rule says each method
    is optional. The server duck-types this one, so implementing it is enough
    and inheriting from it is never required.

    Most plugins want :meth:`CallSession.latest_video_frame` instead: the
    model asks to look long after the frame arrived, and the server keeps the
    latest frame per source whether or not anything implements this.
    """

    async def on_video_frame(self, frame: VideoFrame) -> None:
        """One sampled frame of the caller's camera or screen share.

        Frames arrive sparsely and best-effort: StandIn drops a frame rather
        than queueing it when the socket is busy, so this is not a video stream.

        Called on the receive path of a live call, so a slow model call belongs
        off the frame loop, exactly as it does in
        :meth:`CallHandler.on_caller_audio`.
        """
        ...
