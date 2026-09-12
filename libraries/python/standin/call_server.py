# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The call listener every StandIn plugin shares.

StandIn dials ``wss://<your-host>/msteams/calling/{callId}`` once per call. This
server answers that dial, authenticates it, speaks the call wire protocol, and hands each call to a :class:`~.handler.CallHandler` supplied by
a plugin. Everything here is the same whichever agent framework is on the
other side, which is exactly why it lives at the top of the package and not
under standin/plugins/:

* the HMAC handshake, its freshness window and its single-use replay guard
* capacity, draining, and the one-live-session-per-callId rule
* the frame loop: ``session.start``, ``audio.frame``, ``video.frame``,
  ``ping``, ``participants``, ``dtmf``, ``recording.status``,
  ``assistant.say``, ``session.end``
* outbound sequence numbers and the audio timeline
* the pre-start watchdog and the caller-audio idle watchdog
* idempotent, shielded teardown that always frees the slot

Defaults match the StandIn worker layout: port **9442**, path
``/msteams/calling``. Expose the port (for example
``tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling``)
and register the public ``wss://`` URL as the identity's agent voice URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import inspect
import json
import os
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from aiohttp import WSMsgType, web

from ._exceptions import StandInError
from ._hmac import (
    REPLAY_WINDOW_MS,
    SIGNATURE_HEADER,
    SIGNATURE_V2_HEADER,
    TIMESTAMP_HEADER,
    now_ms,
    sign_request,
    verify_handshake,
)
from .avatar import SpeechMark
from .avatar import expression as build_expression
from .avatar import speech_marks as build_speech_marks
from .handler import HandlerFactory
from .log import logger
from .protocol import (
    SAMPLE_RATE_HZ,
    SessionStart,
    assistant_cancel,
    audio_frame,
    decode_pcm,
    parse_message,
    parse_session_start,
    pong,
    session_end,
)
from .vision import VideoFrame, parse_video_frame
from .vision import display_frame as build_display_frame
from .vision import display_image as build_display_image

__all__ = ["CallServer"]

#: Log-safe rendering of an attacker-influenceable id: control characters
#: (CR/LF forge log lines) replaced, length bounded.
_CTRL = re.compile(r"[^\x20-\x7e]")

#: How often the single-use handshake cache is swept for expired entries.
_PRUNE_INTERVAL_MS = 1_000

#: Teardown must never be held hostage by a peer that stopped reading.
#: ``WebSocketResponse.close()`` waits for the peer's close acknowledgement, and
#: until it returns the callId is still occupied - so every retry for that call
#: 409s, and at max_connections the whole listener stops accepting. aiohttp's own
#: ceiling here is 10 s, which is far too long to hold a slot for a peer that has
#: already gone away. The advisory ``session.end`` is written before we wait, so
#: a peer that is still listening has what it needs either way.
_CLOSE_TIMEOUT_S = 2.0

#: An outcome report is a handful of JSON fields. The cap matters because the
#: body must be read before the signature over its hash can be checked, so this
#: is the bound on what an unauthenticated peer can make the worker read.
_MAX_OUTCOME_BYTES = 8 * 1024


def _fresh(timestamp: str) -> bool:
    """Whether a control-request timestamp is inside the replay window."""
    try:
        sent = int(timestamp)
    except (TypeError, ValueError):
        return False
    return abs(now_ms() - sent) <= REPLAY_WINDOW_MS


def _safe(value: str) -> str:
    return _CTRL.sub("?", value)[:80]


async def _dispatch(handler: Any, method: str, *args: Any) -> None:
    """Call one optional handler method.

    A :class:`~.handler.CallHandler` is a Protocol, not a base class, so a
    plugin implements only the methods it cares about and a missing one is a
    no-op. Sync implementations are accepted too: a handler that has nothing to
    await should not be forced to declare ``async``.
    """
    fn = getattr(handler, method, None)
    if fn is None:
        return
    result = fn(*args)
    if inspect.isawaitable(result):
        await result


#: Outbound bytes past which agent audio is dropped rather than queued.
#:
#: A slow or wedged peer turns "await every send" into an unbounded queue, and
#: those awaits are what stall the provider receive loop feeding them. Shedding
#: keeps the loop moving: the caller hears a gap rather than the call wedging.
#:
#: Audio only. Control frames are what END a call, and a call that cannot be
#: ended is the failure this exists to prevent.
MAX_AUDIO_BUFFER_BYTES = 1024 * 1024

#: How long a call may go without an agent ever answering it.
#:
#: The one gap none of the other watchdogs cover. ``pre_start_timeout`` watches
#: for session.start, which arrived. ``on_start_timeout`` bounds on_start, which
#: succeeded. ``audio_idle_timeout`` is satisfied because the CALLER is still
#: talking. So StandIn is on the call, the caller hears nothing, and nothing
#: ends it.
STALE_CALL_REAPER_S = 120.0

#: How often to look. Coarse on purpose: this is a grace period, not something
#: anybody measures to the second.
REAPER_CHECK_INTERVAL_S = 15.0
REAPER_MIN_INTERVAL_S = 0.05


class _Call:
    """One live call: the StandIn socket on one side, a plugin's handler on the
    other."""

    def __init__(self, server: CallServer, call_id: str, ws: web.WebSocketResponse) -> None:
        self._server = server
        self._call_id = call_id
        self._ws = ws
        self._handler: Any = None
        self._start: SessionStart | None = None
        self._seq = 0
        self._sent_ms = 0
        # The tile stream has its own sequence: it is a separate stream from the
        # audio, and a receiver drops out-of-order frames per stream.
        self._tile_seq = 0
        self._last_audio: float | None = None
        # One recording flag, kept current here so no plugin has to re-derive
        # it from the context sentence it happened to see.
        self._recording = False
        # Whether a recording.status frame has actually said so. The
        # session.start snapshot OMITS the field when the state was unknown at
        # answer time, and an omitted field is not "not recording": a
        # recording.status that lands first would otherwise be overwritten by
        # the absent snapshot, and every recording-gated capability would stay
        # shut for the rest of the call with nothing said.
        self._recording_reported = False
        # The active speaker, when StandIn sends unmixed audio. None on the
        # mixed path, which is most calls.
        self._speaker: str | None = None
        self._participants = 0
        self._audio_dropped = 0
        self._last_drop_log = 0.0
        # Monotonic, not wall clock: a clock step forward larger than the grace
        # period would otherwise reap every live unanswered call at once.
        self._started_at = time.monotonic()
        self._answered_at: float | None = None
        # Latest frame per source, and only the latest: frames arrive sparsely
        # and a held history would be an unbounded buffer of the caller's
        # screen. Never written to disk.
        self._frames: dict[str, VideoFrame] = {}
        self._closed = False
        self._in_start = False
        self._closing_reason = "call-ended"
        self._close_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # ---- the CallSession surface handed to the plugin ----

    @property
    def call_id(self) -> str:
        return self._call_id

    @property
    def start(self) -> SessionStart:
        if self._start is None:
            raise StandInError("the call has not started yet")
        return self._start

    @property
    def recording_active(self) -> bool:
        """Whether this call is being recorded, right now."""
        return self._recording

    @property
    def answered(self) -> bool:
        """Whether anything has actually taken this call yet."""
        return self._answered_at is not None

    def mark_answered(self) -> None:
        """Say that an agent has taken the call. Stamped once, never re-stamped.

        A plugin that joins a room calls this when the agent's own audio track
        appears, not when a participant connects: monitors, recorders and avatar
        workers all connect, and none of them is an agent answering.

        A plugin that never calls it is still covered, because sending audio
        counts. The explicit call exists for an agent that joins and listens
        before it says anything.
        """
        if self._answered_at is None:
            self._answered_at = time.monotonic()

    @property
    def speaker(self) -> str | None:
        """Who is speaking, when StandIn sends unmixed audio."""
        return self._speaker

    @property
    def participant_count(self) -> int:
        """How many people are on the call. Zero until StandIn says."""
        return self._participants

    @property
    def buffered_bytes(self) -> int:
        """Outbound bytes the socket has not flushed yet.

        Read off the transport's own write buffer. Wrapped because this reaches
        past aiohttp's public surface: a shape change there must cost a
        conservative zero, not an exception on the hot path.
        """
        try:
            transport = self._ws._writer.transport  # type: ignore[attr-defined]
            return int(transport.get_write_buffer_size())
        except Exception:
            return 0

    @property
    def media_time_ms(self) -> int:
        """The outbound audio timeline this call stamps its audio with."""
        return self._sent_ms

    async def send_audio(self, pcm: bytes) -> None:
        """Send agent audio to the caller. The server owns ``seq`` and the
        timeline, so a handler that swaps or re-publishes its audio source
        cannot make ``timestampMs`` jump backwards while ``seq`` keeps climbing.
        """
        # Gated on the SOCKET, not on _closed: _begin_close sets _closed BEFORE
        # teardown dispatches the handler's aclose, so gating on the flag would
        # silently break the guarantee below that a handler can still speak on
        # the way out. A documented promise that does not hold is worse than no
        # promise at all.
        if self._ws.closed or not pcm:
            return
        # Sending audio IS answering, so every plugin is covered by the reaper
        # without doing anything.
        self.mark_answered()
        timestamp_ms = self._sent_ms
        # The timeline advances whether or not this frame goes out. It is the
        # CALLER's clock: a dropped frame is a gap in what they hear, not a
        # rewind, and stalling the clock would make every later frame claim a
        # time that has already passed.
        self._seq += 1
        self._sent_ms += (len(pcm) // 2) * 1000 // SAMPLE_RATE_HZ
        if self._over_audio_budget():
            return
        await self._send(audio_frame(self._seq, timestamp_ms, pcm))

    def _over_audio_budget(self) -> bool:
        """Whether the socket is too far behind to take another audio frame."""
        if self.buffered_bytes <= MAX_AUDIO_BUFFER_BYTES:
            return False
        self._audio_dropped += 1
        now = time.monotonic()
        if now - self._last_drop_log >= 5.0:
            logger.warning(
                "standin: call %s is shedding agent audio to keep the loop moving "
                "(%d frames so far); the peer is not reading",
                _safe(self._call_id),
                self._audio_dropped,
            )
            self._last_drop_log = now
        return True

    async def cancel_playback(self) -> None:
        """Drop whatever agent audio StandIn still has buffered.

        The only lever that un-sends audio already handed to the service: it
        flushes the platform player, so the caller stops hearing the turn they
        just interrupted. Without it a barge-in stops the MODEL but the bot
        keeps talking for the length of the buffered PCM.

        Call it the moment your provider reports the caller started speaking,
        before you cancel the response upstream.
        """
        if self._ws.closed:
            return
        await self._send(assistant_cancel(self._seq))

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        """The most recent frame the caller showed, or None.

        Screen share wins over camera when no source is named: an agent asked
        to look is nearly always being asked about what is being shown.
        """
        if source is not None:
            return self._frames.get(source)
        return self._frames.get("screenshare") or self._frames.get("camera")

    async def send_tile_frame(
        self, jpeg: bytes, width: int | None = None, height: int | None = None
    ) -> None:
        """Send one frame of continuous avatar video.

        Gated on the socket, like send_audio: a handler may still be showing
        something on its way out.
        """
        if self._ws.closed or not jpeg:
            return
        self._tile_seq += 1
        await self._send(
            build_display_frame(self._tile_seq, self._sent_ms, jpeg, "image/jpeg", width, height)
        )

    async def display_image(
        self,
        image: bytes | str,
        mime: str = "image/jpeg",
        duration_ms: int | None = None,
        mode: str | None = None,
        caption: str | None = None,
    ) -> None:
        """Draw an image on the bot's video tile.

        Gated on the socket for the same reason send_audio is: a handler is
        allowed to show something on its way out.
        """
        if self._ws.closed:
            return
        await self._send(build_display_image(image, mime, duration_ms, mode, caption))

    async def express(self, emotion: str) -> None:
        """Hint the avatar's emotion. Video only, and never fatal."""
        if self._ws.closed:
            return
        await self._send(build_expression(emotion))

    async def send_speech_marks(self, marks: Iterable[SpeechMark]) -> None:
        """Send one utterance's viseme timeline for avatar lip-sync."""
        if self._ws.closed:
            return
        await self._send(build_speech_marks(marks))

    async def end(self, reason: str) -> None:
        """Ask for the call to end.

        Awaiting teardown from INSIDE on_start would deadlock: teardown waits
        for on_start to return before dispatching the handler's aclose, and
        on_start would be waiting for teardown. Refusing a call is a normal
        thing to do from on_start - an allowlist rejection, a busy runtime, a
        provider that will not connect - so it is made safe here: ask for the
        close, return immediately, and let it run once on_start unwinds.
        """
        if self._in_start:
            self._begin_close(reason)
            return
        await self.aclose(reason)

    # ---- lifecycle ----

    async def run(self) -> None:
        """Drive the socket until it ends. Never raises to the caller: a single
        bad call must not take the worker with it."""
        try:
            await self._pump_worker()
        except asyncio.CancelledError:
            self._begin_close("server-shutdown")
            raise
        except Exception:
            logger.exception("standin: call %s failed", _safe(self._call_id))
            self._begin_close("transport-failure")
        finally:
            await self.aclose()

    async def _pump_worker(self) -> None:
        started = False
        async for msg in self._ws:
            if msg.type is WSMsgType.ERROR:
                raise StandInError(f"call socket error: {self._ws.exception()}")
            if msg.type is not WSMsgType.TEXT:
                continue
            frame = parse_message(msg.data)
            if frame is None:
                continue
            kind = frame["type"]
            if kind == "session.start":
                if started:
                    continue  # a second start is a sender bug, not a new call
                started = True
                await self._on_session_start(parse_session_start(frame))
            elif kind == "audio.frame":
                self._last_audio = time.monotonic()
                await self._on_caller_audio(frame)
            elif kind == "video.frame":
                await self._on_video_frame(frame)
            elif kind == "ping":
                await self._send(pong(frame.get("ts")))
            elif kind == "participants":
                # Call context is rendered as finished English sentences, not
                # as structured fields, so a handler can hand it straight to a
                # model. Both SDKs render the same text for the same frame.
                count = frame.get("count")
                if isinstance(count, int):
                    self._participants = max(0, count)
                    if count <= 1:
                        sentence = "This is a 1:1 call with a single human caller."
                    else:
                        sentence = (
                            f"There are {count} human participants on this call. "
                            "Stay quiet unless directly addressed."
                        )
                    await self._on_context(sentence)
            elif kind == "dtmf":
                digit = frame.get("digit")
                if isinstance(digit, str) and digit:
                    await self._on_context(f'The caller pressed the "{digit}" key on their keypad.')
            elif kind == "recording.status":
                status = frame.get("status")
                if isinstance(status, str):
                    was_recording = self._recording
                    self._recording = status == "active"
                    self._recording_reported = True
                    if self._recording and not was_recording:
                        # A call that has just connected has sent no audio yet.
                        # Restart the idle clock from here or the watchdog
                        # judges the new call by how long the ringing took.
                        self._last_audio = time.monotonic()
                    await self._on_context(
                        "The Microsoft Teams call recording is now ACTIVE."
                        if status == "active"
                        else "The Microsoft Teams call recording is not active."
                    )
            elif kind == "assistant.say":
                text = frame.get("text")
                if isinstance(text, str) and text.strip():
                    # Flush the worker's queued agent playback FIRST: without the
                    # cancel, the goodbye publishes behind seconds of already-
                    # buffered audio and the call is torn down before it plays.
                    await self._send(assistant_cancel(self._seq))
                    await self._guard("on_goodbye", text)
            elif kind == "session.end":
                self._closing_reason = str(frame.get("reason") or "call-ended")
                return
            # Anything else (the avatar surface included) is ignored by
            # contract, so an older plugin and a newer StandIn interoperate.

    async def _on_session_start(self, start: SessionStart) -> None:
        if start.call_id != self._call_id:
            # The URL path is what the HMAC signed. A body that disagrees is
            # either a bug or an attempt to ride one call's signature into
            # another's session.
            raise StandInError(
                f"session.start callId {start.call_id!r} does not match the authenticated path"
            )
        self._start = start
        # Only when nothing has reported the real state yet. recording.status
        # can land before session.start, and the snapshot is omitted when the
        # state was unknown at answer time, so seeding unconditionally turns a
        # live ACTIVE into False for the whole call.
        if not self._recording_reported:
            self._recording = start.recording_status == "active"
        self._handler = self._server._build_handler()
        self._last_audio = time.monotonic()

        # Arm the idle watchdog BEFORE on_start, not after. on_start does real
        # network work (LiveKit joins a room and dispatches an agent; a realtime
        # plugin opens a provider socket), and while it is awaited the frame loop
        # is suspended, so session.end is never read. Armed after, a hung on_start
        # has NO watchdog at all: _watch_pre_start already sees _start, nothing
        # else is running, and the callId 409s forever - one leaked slot per
        # inbound call, up to max_connections.
        self._spawn(self._watch_audio_idle())
        self._spawn(self._watch_call_duration())

        # Bounded, and routed through _guard so a third-party outage inside a
        # plugin closes the call as 'handler-start-failure' rather than
        # 'transport-failure' - which would report a plugin's problem as
        # StandIn's own socket failing, and send both sides debugging the wrong
        # system.
        self._in_start = True
        try:
            await self._guard_start()
        finally:
            self._in_start = False
        logger.info(
            "standin: call %s started (%s, caller %s)",
            _safe(self._call_id),
            start.direction,
            _safe(start.caller.display_name or "unknown"),
        )

    async def _guard_start(self) -> None:
        """on_start with its own timeout and its own close reason."""
        timeout = self._server.on_start_timeout
        try:
            if timeout > 0:
                await asyncio.wait_for(_dispatch(self._handler, "on_start", self), timeout)
            else:
                await _dispatch(self._handler, "on_start", self)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.error(
                "standin: handler.on_start exceeded %.0fs on call %s",
                timeout,
                _safe(self._call_id),
            )
            self._begin_close("handler-start-timeout")
        except Exception:
            logger.exception("standin: handler.on_start failed on call %s", _safe(self._call_id))
            self._begin_close("handler-start-failure")

    async def _on_caller_audio(self, frame: dict[str, Any]) -> None:
        if self._handler is None or self._closed:
            return  # audio before session.start, or after teardown
        try:
            pcm = decode_pcm(frame.get("payloadBase64"))
        except ValueError as err:
            logger.warning("standin: dropping caller frame: %s", err)
            return
        await self._note_speaker(frame.get("speakerName"))
        await self._guard("on_caller_audio", pcm)

    async def _note_speaker(self, name: Any) -> None:
        """Remember who is talking, and say so once when it changes.

        Absent on the mixed path, which is most calls, so this is additive: a
        handler that never looks at it behaves exactly as before. The callback
        fires on CHANGE only. It rides every audio frame, and a model told
        forty times a second who is speaking would hear nothing else.
        """
        if not isinstance(name, str):
            return
        speaker = name.strip()
        if not speaker or speaker == self._speaker:
            return
        self._speaker = speaker
        # _dispatch already no-ops on a handler that does not implement it.
        await self._guard("on_speaker_change", speaker)

    async def _on_video_frame(self, frame: dict[str, Any]) -> None:
        """Store the latest frame per source, then offer it to the handler.

        Stored even when the handler implements no callback, because
        latest_video_frame is the way most plugins use this lane: the
        model asks to look long after the frame arrived.
        """
        if self._handler is None or self._closed:
            return
        parsed = parse_video_frame(frame)
        if parsed is None:
            # Sparse and best-effort by contract. One unusable frame is not
            # worth a log line per frame, let alone ending the call.
            return
        self._frames[parsed.source] = parsed
        await self._guard("on_video_frame", parsed)

    async def _on_context(self, text: str) -> None:
        if self._handler is None:
            # Context can arrive before session.start on a fast dial. Dropping
            # it is correct: there is no handler to receive it, and the server
            # does not queue on a plugin's behalf.
            return
        await self._guard("on_context", text)

    async def _guard(self, method: str, *args: Any) -> None:
        """Run one handler callback. A plugin raising must end its own call, not
        the worker, and not the frame loop mid-utterance."""
        if self._handler is None:
            return
        try:
            await _dispatch(self._handler, method, *args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: handler.%s failed on call %s", method, _safe(self._call_id))
            self._begin_close("handler-failure")

    def _ringing(self) -> bool:
        """Whether this is an outbound call nobody has answered yet."""
        return (
            self._start is not None
            and self._start.direction == "outbound"
            and not self._recording_reported
        )

    async def _watch_call_duration(self) -> None:
        """End a call that has run past its ceiling, still going.

        The audio-idle watchdog ends a call that went QUIET. This one ends a
        call that has not: a caller who will not hang up, a model looping at
        itself, an automated system that dialled and never stopped talking. Each
        of those bills a provider by the minute for as long as the socket lives,
        and none of them trips a silence check.

        The goodbye goes through the handler's ``on_goodbye``, the same callback
        StandIn's own closing line uses, so no plugin needs new code for this.
        Playback is flushed FIRST, or the line queues behind however many
        seconds of agent audio the service still holds and the call ends before
        anyone hears it.
        """
        limit = self._server.max_call_seconds
        if limit <= 0:
            return
        await asyncio.sleep(limit)
        if self._closed or self._ws.closed:
            return
        logger.info(
            "standin: call %s reached its %.0fs limit; saying goodbye",
            _safe(self._call_id),
            limit,
        )
        with contextlib.suppress(Exception):
            await self.cancel_playback()
        text = self._server.goodbye_text.strip()
        if text:
            await self._guard("on_goodbye", text)
            # Bounded whatever the handler does with it. A plugin that hangs in
            # on_goodbye must not turn a time-limited call into an endless one,
            # which is the exact failure this watchdog exists to prevent.
            await asyncio.sleep(max(0.0, self._server.goodbye_grace))
        self._begin_close("call-duration-limit")

    async def _watch_audio_idle(self) -> None:
        """End the call when the caller's audio stops arriving.

        A live Microsoft Teams call delivers PCM continuously - silence is still frames -
        so audio going quiet for this long means the call is gone on the far
        side and nobody told us. That happens: the peer keeps the socket open
        (and even keeps pinging) while its own teardown is wedged, and without
        this backstop the handler's session burns until someone notices."""
        idle = self._server.audio_idle_timeout
        if idle <= 0:
            return
        while not self._closed:
            await asyncio.sleep(min(idle / 4, 10.0))
            if self._ringing():
                # An outbound call that nobody has picked up yet carries no
                # caller audio BY DEFINITION, so to this watchdog it looks
                # exactly like a dead one. Ringing is bounded by the answer
                # window instead.
                continue
            last = self._last_audio
            if last is not None and time.monotonic() - last > idle:
                logger.warning(
                    "standin: call %s got no caller audio for %.0fs; ending it",
                    _safe(self._call_id),
                    idle,
                )
                await self.aclose("caller-idle-timeout")
                return

    def _begin_close(self, reason: str) -> asyncio.Task[None]:
        """Start (or return the in-flight) teardown task. Idempotent, and the
        FIRST reason wins - a cascade of close causes must not overwrite the
        one that actually ended the call."""
        if self._close_task is None:
            self._closing_reason = reason
            self._closed = True
            self._close_task = asyncio.ensure_future(self._teardown())
        return self._close_task

    async def aclose(self, reason: str | None = None) -> None:
        """Every caller waits for the SAME teardown, shielded: a caller being
        cancelled (the pre-start watchdog, a task in _tasks that teardown
        itself cancels, worker shutdown) must never abort teardown mid-flight -
        that is how a slot leaks and a callId 409s forever."""
        task = self._begin_close(reason or self._closing_reason)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            # Only the CALLER was cancelled; teardown continues on its own.
            raise
        except Exception:
            pass  # teardown already logged; never raise into callers

    async def _teardown(self) -> None:
        try:
            # Teardown runs in its OWN task, never in self._tasks, so cancelling
            # the whole set is safe here.
            for task in list(self._tasks):
                task.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

            # A handler may refuse the call from inside on_start by awaiting
            # session.end(...). Dispatching aclose while on_start is still on the
            # stack tears down half-built state, and then on_start RESUMES and
            # finishes building a provider session that nothing will ever close -
            # one leaked socket per refusal. Wait for it, bounded.
            if self._in_start:
                for _ in range(200):  # 2s at 10ms
                    if not self._in_start:
                        break
                    await asyncio.sleep(0.01)

            # The plugin releases its side BEFORE the socket closes, so a
            # handler that wants to say something on the way out still can.
            if self._handler is not None:
                with contextlib.suppress(Exception):
                    await _dispatch(self._handler, "aclose", self._closing_reason)
                self._handler = None

            with contextlib.suppress(Exception):
                if not self._ws.closed:
                    await self._send(session_end(self._closing_reason))
                    # Bounded: see _CLOSE_TIMEOUT_S. Releasing the slot matters
                    # more than a clean close handshake with an absent peer.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._ws.close(), _CLOSE_TIMEOUT_S)
        finally:
            # Unconditional: whatever failed or was cancelled above, the slot is
            # released and the callId becomes usable again.
            self._server._release(self._call_id)
            logger.info("standin: call %s ended (%s)", _safe(self._call_id), self._closing_reason)

    # ---- plumbing ----

    async def _send(self, text: str) -> None:
        if self._ws.closed:
            return
        with contextlib.suppress(Exception):
            await self._ws.send_str(text)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _unanswered(call: _Call, stale_s: float, now: float) -> bool:
    """Whether this call has run past its grace with nothing answering it.

    Pure, with the clock passed in, so the boundary is testable without
    sleeping. Strictly greater, so a tick landing exactly on the grace does not
    reap a call one instant early.
    """
    return call._answered_at is None and stale_s > 0 and (now - call._started_at) > stale_s


class CallServer:
    """Answers the socket StandIn dials, and hands each call to a plugin.

    Args:
        handler_factory: builds one :class:`~.handler.CallHandler` per call,
            called with no arguments. This is the plugin.
        secret: the connection secret from the StandIn portal. Must byte-match,
            or the handshake is rejected with 401. Defaults to ``STANDIN_SECRET``.
        host / port / ws_path: where the listener binds. Defaults to the StandIn
            plugin layout, ``0.0.0.0:9442`` at ``/msteams/calling``. ``0.0.0.0``
            because the worker usually runs in a container behind an ingress; bind
            ``127.0.0.1`` when only a local tunnel should reach the listener (the
            upgrade is HMAC-authenticated either way).
        max_connections: concurrent live calls, checked before any crypto runs.
        pre_start_timeout: seconds a socket may stay silent after authenticating
            before it is dropped for never sending ``session.start``.
        audio_idle_timeout: seconds without caller audio before a live call is
            declared dead and torn down (0 disables). A live Microsoft Teams call streams
            PCM continuously, silence included, so this fires only when the far
            side is gone but its socket was never closed.
        on_start_timeout: seconds a handler's ``on_start`` may take before the
            call is given up (0 disables). It does real network work - joining a
            room, opening a provider socket - and the frame loop is suspended
            while it runs, so an unbounded one holds a slot for the life of the
            worker.
        stale_call_reaper_seconds: seconds a call may run with nothing having
            answered it (0 disables). The gap none of the other watchdogs
            cover: an agent dispatch that never lands leaves StandIn on the
            call, the caller hearing nothing, and no timer that fires.
        max_call_seconds: a hard ceiling on ONE call, measured from
            ``session.start`` (0 disables). Different from
            ``audio_idle_timeout``, which ends a call that went quiet: this one
            ends a call that is still going. A caller who will not hang up, a
            model looping at itself, an automated system that dialled and never
            stopped talking, all bill a provider by the minute for as long as
            the socket lives.
        goodbye_text: what to say before hanging up on the limit. Delivered
            through the handler's ``on_goodbye``, the same callback StandIn's
            own closing line uses, so a plugin needs no new code to honour it.
        goodbye_grace: seconds to let that line finish before the call ends.
    """

    def __init__(
        self,
        *,
        handler_factory: HandlerFactory,
        secret: str | None = None,
        host: str | None = None,
        port: int | None = None,
        ws_path: str | None = None,
        max_connections: int = 64,
        pre_start_timeout: float = 10.0,
        audio_idle_timeout: float = 45.0,
        on_start_timeout: float = 15.0,
        max_call_seconds: float = 0.0,
        stale_call_reaper_seconds: float = STALE_CALL_REAPER_S,
        goodbye_text: str = "We are out of time on this call, so I have to stop here. Goodbye.",
        goodbye_grace: float = 6.0,
        on_call_outcome: Callable[[str, str], Any] | None = None,
    ) -> None:
        if not callable(handler_factory):
            raise StandInError("handler_factory must be callable and build one handler per call")
        self._handler_factory = handler_factory
        #: Called with (call_id, outcome) when StandIn reports how an outbound
        #: call ended without anyone answering. Set it and the route exists;
        #: leave it and a POST there is a 404, so a worker that never places a
        #: call opens no extra surface.
        self._on_call_outcome = on_call_outcome

        self._secret = secret or os.environ.get("STANDIN_SECRET", "")
        if not self._secret:
            raise StandInError(
                "a StandIn connection secret is required: pass secret=... or set STANDIN_SECRET"
            )

        self._host = host if host is not None else os.environ.get("STANDIN_HOST", "0.0.0.0")
        self._port = port if port is not None else int(os.environ.get("STANDIN_PORT", "9442"))
        path = (
            ws_path
            if ws_path is not None
            else os.environ.get("STANDIN_WS_PATH", "/msteams/calling")
        )
        self._ws_path = "/" + path.strip().strip("/")
        if self._ws_path == "/":
            raise StandInError("ws_path must be a real path such as /msteams/calling")
        self._max_connections = max_connections
        self._pre_start_timeout = pre_start_timeout
        #: Seconds without caller audio before a live call is declared dead
        #: (0 disables). A live Microsoft Teams call streams PCM continuously, silence
        #: included, so this only fires when the far side is gone or wedged.
        self.audio_idle_timeout = audio_idle_timeout
        #: Seconds a handler's on_start may take before the call is given up
        #: (0 disables). on_start does real network work and the frame loop is
        #: suspended while it runs, so an unbounded one holds a slot forever.
        self.on_start_timeout = on_start_timeout
        #: A hard ceiling on one call, from session.start (0 disables). The
        #: audio-idle watchdog ends a call that went quiet; this one ends a call
        #: that is still going and has no reason to stop.
        self.max_call_seconds = max_call_seconds
        #: Said through the handler's on_goodbye before the ceiling ends the
        #: call, then the call ends whether or not it was said.
        self.goodbye_text = goodbye_text
        self.goodbye_grace = goodbye_grace
        #: Seconds a call may run with nothing having answered it (0 disables).
        #: On by default: it spends nothing and only frees what is already lost.
        self.stale_call_reaper_seconds = stale_call_reaper_seconds

        self._calls: dict[str, _Call] = {}
        self._reaper: asyncio.Task[None] | None = None
        #: fingerprint -> signing timestamp (ms). Pruned by AGE, never wholesale:
        #: clearing the set would reopen the replay window for every handshake
        #: still inside it.
        self._used_signatures: dict[str, int] = {}
        self._last_prune = now_ms()
        self.draining = False
        self._runner: web.AppRunner | None = None

    @property
    def ws_path(self) -> str:
        return self._ws_path

    @property
    def active_calls(self) -> int:
        return len(self._calls)

    @property
    def running(self) -> bool:
        """Whether the listener is actually bound.

        A host that calls connect twice today binds a second listener and leaks
        the first, and has no way to ask whether the bind succeeded, so it
        reports a dead platform as connected.
        """
        return self._runner is not None

    @property
    def host(self) -> str:
        """The interface the listener is bound to, or will be."""
        return self._host

    @property
    def port(self) -> int:
        """The port actually bound, or 0 before :meth:`start`.

        Asked for after starting on port 0, which is how an ephemeral listener
        avoids racing another process for a port picked in advance.
        """
        return self._port

    def _build_handler(self) -> Any:
        return self._handler_factory()

    async def start(self) -> None:
        """Bind the listener. Transactional: either it is listening when this
        returns, or nothing of it survives."""
        if self._runner is not None:
            raise StandInError("this listener is already running")
        app = web.Application()
        app.router.add_get("/healthz", self._healthz)
        app.router.add_get(f"{self._ws_path}/{{call_id}}", self._upgrade)
        if self._on_call_outcome is not None:
            app.router.add_post(f"{self._ws_path}/outcome/{{call_id}}", self._outcome)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, self._host, self._port).start()
        except BaseException:
            with contextlib.suppress(Exception):
                await runner.cleanup()
            raise
        self._runner = runner
        # Port 0 means "any free one", and the caller has to be told which one
        # it got: the alternative is picking a free port with a throwaway
        # socket and racing whatever binds it in between.
        bound = runner.addresses
        if bound:
            self._port = int(bound[0][1])
        if self.stale_call_reaper_seconds > 0:
            self._reaper = asyncio.ensure_future(self._reap_unanswered())
        logger.info(
            "standin: answering Microsoft Teams calls on %s:%s%s",
            self._host,
            self._port,
            self._ws_path,
        )

    async def _reap_unanswered(self) -> None:
        """End calls that nothing ever answered.

        An agent dispatch that never lands is the commonest misconfiguration
        there is, and it is invisible to every other watchdog: session.start
        arrived, on_start succeeded, and the caller keeps sending audio the
        whole time. Without this the caller sits on a live call hearing nothing
        and the worker holds the slot until somebody notices.
        """
        stale = self.stale_call_reaper_seconds
        interval = max(REAPER_MIN_INTERVAL_S, min(REAPER_CHECK_INTERVAL_S, stale))
        reaped: set[str] = set()
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            # A SNAPSHOT: ending a call removes it from the registry, and
            # iterating the live one raises and kills this task for the life of
            # the worker.
            live = list(self._calls.items())
            # Bounded by what is actually running, so a long-lived worker does
            # not accumulate ids for ever.
            reaped &= {call_id for call_id, _ in live}
            for call_id, call in live:
                if call_id in reaped or not _unanswered(call, stale, now):
                    continue
                reaped.add(call_id)
                logger.warning(
                    "standin: nothing answered call %s within %.0fs; ending it",
                    _safe(call_id),
                    stale,
                )
                call._begin_close("no-agent-answered")

    async def aclose(self) -> None:
        """Drain live calls, then stop listening. Awaits the calls' REAL
        teardown tasks - an aclose that early-returns on an in-flight closer
        would let the loop stop with teardown still pending, leaking sessions."""
        reaper, self._reaper = self._reaper, None
        if reaper is not None:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        calls = list(self._calls.values())
        if calls:
            await asyncio.gather(
                *(c._begin_close("server-shutdown") for c in calls), return_exceptions=True
            )
        self._calls.clear()
        runner, self._runner = self._runner, None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()

    # ---- request handling ----

    async def _outcome(self, request: web.Request) -> web.Response:
        """How an outbound call ended, reported by StandIn.

        Only reached when a plugin asked for it. The route carries the only
        signal that nobody answered, and without it an unanswered call waits
        out the ring timeout before anything can be said about it.

        The body is read and capped BEFORE the signature is checked, because v2
        signs a hash of the body: there is nothing to verify until the bytes are
        in hand. The cap is what stops that being a way to make the worker read
        an unbounded request from an unauthenticated peer.
        """
        call_id = request.match_info.get("call_id", "")
        if request.content_length is not None and request.content_length > _MAX_OUTCOME_BYTES:
            return web.Response(status=413, text="outcome body too large")
        raw = await request.content.read(_MAX_OUTCOME_BYTES + 1)
        if len(raw) > _MAX_OUTCOME_BYTES:
            return web.Response(status=413, text="outcome body too large")

        timestamp = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(SIGNATURE_V2_HEADER, "")
        expected = sign_request(self._secret, timestamp, "POST", request.path, raw)
        if not signature or not hmac.compare_digest(signature.strip().lower(), expected):
            logger.warning("standin: refused an unsigned call outcome for %s", _safe(call_id))
            return web.Response(status=401, text="bad signature")
        if not _fresh(timestamp):
            return web.Response(status=401, text="stale signature")

        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            payload = {}
        outcome = ""
        if isinstance(payload, dict):
            outcome = str(payload.get("outcome") or payload.get("reason") or "")

        try:
            result = self._on_call_outcome(call_id, outcome)  # type: ignore[misc]
            if inspect.isawaitable(result):
                await result
        except Exception:
            # A plugin failing to handle an outcome must not make StandIn retry
            # forever. Log it and acknowledge.
            logger.exception("standin: handling the outcome for %s failed", _safe(call_id))
        return web.Response(status=204)

    async def _healthz(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "calls": len(self._calls)})

    async def _upgrade(self, request: web.Request) -> web.StreamResponse:
        call_id = request.match_info.get("call_id", "")
        if not call_id:
            return web.Response(status=400, text="missing callId")

        # Draining: live calls continue, new ones are refused so a worker that
        # is winding down does not accept calls it will never serve.
        if self.draining:
            return web.Response(status=503, text="draining")

        # Capacity is checked BEFORE any crypto, so a flood cannot make us spend
        # CPU on signatures for calls we were never going to accept.
        if len(self._calls) >= self._max_connections:
            logger.warning("standin: refusing %s, at capacity", _safe(call_id))
            return web.Response(status=503, text="at capacity")

        timestamp = request.headers.get(TIMESTAMP_HEADER)
        signature = request.headers.get(SIGNATURE_HEADER)
        if not verify_handshake(self._secret, timestamp, call_id, signature):
            return web.Response(status=401, text="unauthorized")

        # Single-use handshake: a correctly signed upgrade replayed inside the
        # freshness window must not open a second socket. The fingerprint uses
        # the NORMALIZED signature - verify accepts case/whitespace variants, so
        # keying on the raw header would let the same capture replay once per
        # casing. Entries are pruned by age, matching the freshness window.
        sig_norm = (signature or "").strip().lower()
        fingerprint = f"{timestamp}.{sig_norm}"
        now = now_ms()
        if fingerprint in self._used_signatures:
            return web.Response(status=401, text="handshake already used")
        # Key on the SIGNING timestamp, never the arrival time: verification
        # accepts a timestamp up to REPLAY_WINDOW_MS in the FUTURE, so an entry
        # aged from arrival can be pruned while its signature is still valid -
        # reopening the exact replay this guard exists to close. Aged from the
        # signing time, an entry lives precisely as long as the signature does.
        self._used_signatures[fingerprint] = int(timestamp) if timestamp else now
        # Prune on a time throttle, not on a size threshold: rebuilding once the
        # map passes a watermark makes every later request O(n). Only correctly
        # signed, not-yet-seen handshakes reach this line (a bad signature 401s
        # earlier, a replay returns above), so the map tracks StandIn's real
        # call rate rather than attacker traffic.
        if now - self._last_prune >= _PRUNE_INTERVAL_MS:
            self._last_prune = now
            cutoff = now - REPLAY_WINDOW_MS
            self._used_signatures = {
                fp: ts for fp, ts in self._used_signatures.items() if ts >= cutoff
            }

        if call_id in self._calls:
            return web.Response(status=409, text="call already has a live session")

        # 2 MB bounds a single inbound message, matching the sibling providers:
        # audio is ~856 B base64 per frame, and the protocol caps video.frame
        # JPEGs to fit this envelope (sent sparsely, dropped when busy).
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)

        call = _Call(self, call_id, ws)
        self._calls[call_id] = call
        logger.info("standin: call %s connected", _safe(call_id))

        watchdog = asyncio.ensure_future(self._watch_pre_start(call))
        try:
            await call.run()
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
        return ws

    async def _watch_pre_start(self, call: _Call) -> None:
        """Drop a socket that authenticates and then never starts a call: it
        holds a connection slot and a callId that nothing will ever free."""
        await asyncio.sleep(self._pre_start_timeout)
        # This timer bounds arrival of session.start, not handler startup.
        # Once the frame is received, on_start_timeout owns the startup budget.
        if call._start is None and not call._closed:
            logger.warning("standin: call %s never sent session.start", _safe(call._call_id))
            await call.aclose("pre-start-timeout")

    def _release(self, call_id: str) -> None:
        self._calls.pop(call_id, None)
