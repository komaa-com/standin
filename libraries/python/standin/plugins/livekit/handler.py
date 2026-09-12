# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""One Microsoft Teams call, bridged into one LiveKit room.

This is the whole LiveKit-specific half of the plugin. Everything that is
the same for every framework - the socket StandIn dials, the HMAC handshake and
its replay guard, capacity and draining, the wire protocol, the outbound audio
timeline, the watchdogs, teardown ordering - belongs to
:class:`standin.CallServer` and is not repeated here.

What is left is genuinely LiveKit's:

* create one room per call, and dispatch the worker's own agent into it by name
* publish the caller's audio into the room as an ordinary participant track, so
  ``session.start(room=ctx.room)`` picks it up with no special wiring
* relay the agent's audio back out through :meth:`CallSession.send_audio`
* carry Microsoft Teams call context to the agent on two data topics
* delete the room at teardown so the agent job ends at once instead of idling out

By the time the worker's entrypoint runs, the call is an ordinary LiveKit room.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

from standin import NUM_CHANNELS, SAMPLE_RATE_HZ, CallSession, StandInError, TileStream
from standin.plugins._lazy import lazy_module
from standin.startup import StartupBuffer

from .call import TOPIC_CONTEXT, TOPIC_GOODBYE
from .log import logger

#: LiveKit tags an avatar worker with the identity it publishes for, which
#: is how the face is found when it is a different participant from the voice.
_PUBLISH_ON_BEHALF = "lk.publish_on_behalf"


def _int_env(name: str, fallback: int) -> int:
    """A whole positive number, or an error naming the variable.

    Fails loud rather than substituting the default. An operator who typed
    ``LIVEKIT_TILE_VIDEO_FPS=twelve`` is looking at a setting that is not the
    one in force, and silence is what makes that take an afternoon to find.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        raise StandInError(f"{name} must be a whole number above zero, not {raw!r}") from None
    if value <= 0:
        raise StandInError(f"{name} must be a whole number above zero, not {raw!r}")
    return value


# LiveKit is bound lazily, never imported at module load time: one SDK ships
# every plugin, so this file is on disk for readers who never installed
# livekit-agents. The type checker sees the real modules, the interpreter sees a
# stand-in that imports on first touch. See standin/plugins/_lazy.py.
if TYPE_CHECKING:
    from livekit import api, rtc
else:
    api = lazy_module("livekit.api", plugin="livekit", extra="livekit")
    rtc = lazy_module("livekit.rtc", plugin="livekit", extra="livekit")

__all__ = ["TeamsCallHandler", "room_name_for"]

#: callId reaches the SDK as a decoded URL segment, so it can contain anything a
#: %-escape can smuggle. Room names get a conservative charset.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

#: Log-safe rendering of an attacker-influenceable id.
_CTRL = re.compile(r"[^\x20-\x7e]")

_BRIDGE_IDENTITY = "standin-bridge"

#: Context published before the agent is bound would reach nobody, and a caller
#: can generate DTMF faster than an agent joins. Bound the queue.
_MAX_PENDING_CONTEXT = 16


def _safe(value: str) -> str:
    return _CTRL.sub("?", value)[:80]


def _http_url(url: str) -> str:
    """LiveKitAPI speaks HTTP; accept the ws(s):// form people configure."""
    parts = urlparse(url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return urlunparse(parts._replace(scheme=scheme))


def room_name_for(prefix: str, call_id: str) -> str:
    """The room this call gets.

    A separate function because it is a CONTRACT, not a detail: the Python and
    TypeScript SDKs derive the same name for the same call, so a room created by
    either is the same room. Same 100-char budget, same conservative charset - callId
    reaches us as a decoded URL segment, so it can contain anything a %-escape
    can smuggle.
    """
    return f"{prefix}{_UNSAFE.sub('-', call_id)}"[:100]


def _is_agent(participant: rtc.RemoteParticipant) -> bool:
    kind = getattr(participant, "kind", None)
    expected = getattr(rtc.ParticipantKind, "PARTICIPANT_KIND_AGENT", None)
    # Assume agent when the SDK reports no kind, so an older rtc build degrades
    # to first-audio-wins rather than never binding.
    return kind is None or expected is None or bool(kind == expected)


class TeamsCallHandler:
    """The :class:`standin.CallHandler` for LiveKit. One instance per call.

    Built by :func:`~.service.handler_factory`; construct it directly only when
    embedding without an ``AgentServer``.

    Args:
        agent_name: the name this worker registered with, used for explicit
            dispatch. Empty means the project uses automatic dispatch, where
            creating the room is itself what assigns the job.
        livekit_url / livekit_api_key / livekit_api_secret: your LiveKit
            project, defaulting to the standard ``LIVEKIT_*`` env variables the
            worker already has.
        room_prefix: room names are ``{room_prefix}{callId}``.
        delete_room_on_end: delete the room at teardown so the agent job ends
            immediately rather than idling out.
    """

    def __init__(
        self,
        *,
        agent_name: str = "",
        livekit_url: str | None = None,
        livekit_api_key: str | None = None,
        livekit_api_secret: str | None = None,
        room_prefix: str = "msteams-",
        delete_room_on_end: bool = True,
        tile_video: bool | str | None = None,
        tile_video_fps: int | None = None,
    ) -> None:
        self.agent_name = agent_name
        #: Relay an agent's own avatar video onto the bot tile. On unless
        #: ``LIVEKIT_TILE_VIDEO=off``, because an agent that publishes video
        #: almost always means it for the caller to see. Needs the ``tile``
        #: extra to encode frames; without it the relay stays off with one log
        #: line and the audio is unaffected.
        #:
        #: Any other value is a participant IDENTITY to pin the relay to, which
        #: matters when a separate worker publishes the avatar and
        #: publish-on-behalf is NOT set: without a name the relay takes whichever
        #: participant published first, which on a busy room is the wrong one.
        choice = (
            tile_video
            if tile_video is not None
            else os.environ.get("LIVEKIT_TILE_VIDEO", "").strip() or "auto"
        )
        if isinstance(choice, bool):
            self.tile_video = choice
            self.tile_video_identity = ""
        else:
            self.tile_video = choice != "off"
            self.tile_video_identity = "" if choice in ("auto", "off") else choice
        self.tile_video_fps = tile_video_fps or _int_env("LIVEKIT_TILE_VIDEO_FPS", 12)
        self._livekit_url = livekit_url or os.environ.get("LIVEKIT_URL", "")
        self._api_key = livekit_api_key or os.environ.get("LIVEKIT_API_KEY", "")
        self._api_secret = livekit_api_secret or os.environ.get("LIVEKIT_API_SECRET", "")
        missing = [
            name
            for name, value in (
                ("LIVEKIT_URL", self._livekit_url),
                ("LIVEKIT_API_KEY", self._api_key),
                ("LIVEKIT_API_SECRET", self._api_secret),
            )
            if not value
        ]
        if missing:
            raise StandInError(f"LiveKit project not configured: missing {', '.join(missing)}")

        self.room_prefix = room_prefix
        self.delete_room_on_end = delete_room_on_end

        self._call: CallSession | None = None
        self._room: rtc.Room | None = None
        self._room_name = ""
        self._source: rtc.AudioSource | None = None
        self._audio_stream: rtc.AudioStream | None = None
        self._tile: TileStream | None = None
        self._tile_task: asyncio.Task[None] | None = None
        self._tile_sid: str | None = None
        # The caller starts talking the instant the call connects, and joining a
        # room takes a moment. Their first words are often the reason they rang.
        self._pending_audio = StartupBuffer()
        self._agent_identity: str | None = None
        self._pump_sid: str | None = None
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending_context: list[tuple[str, str]] = []

    # ---- the CallHandler surface ------------------------------------------

    async def on_start(self, session: CallSession) -> None:
        """Create the room, dispatch the agent, and start publishing the caller."""
        self._call = session
        self._room_name = room_name_for(self.room_prefix, session.call_id)

        token = (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(_BRIDGE_IDENTITY)
            .with_name("Microsoft Teams")
            .with_ttl(timedelta(hours=6))
            # Automatic jobs do not carry dispatch metadata. Put the same
            # context on the room as it is created, before a job is assigned.
            .with_room_config(api.RoomConfiguration(metadata=json.dumps(self._call_metadata())))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self._room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

        room = rtc.Room()
        self._room = room
        self._wire_room_events(room)
        await room.connect(self._livekit_url, token, rtc.RoomOptions(auto_subscribe=True))
        logger.info('standin: call %s joined room "%s"', _safe(session.call_id), self._room_name)

        # Dispatch AFTER connect, because connect is what creates the room.
        await self._dispatch_agent()

        source = rtc.AudioSource(SAMPLE_RATE_HZ, NUM_CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("teams-caller", source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await room.local_participant.publish_track(track, options)
        self._source = source

        # Whatever the caller said while the room was being joined.
        await self._pending_audio.release(send_audio=self._publish_pcm)
        if self._pending_audio.dropped[0]:
            logger.info("standin: the caller outran the room join; some early audio was dropped")

        # The agent's own face on the tile, when it publishes one. On by
        # default: an agent that publishes video almost always means it for the
        # caller to see.
        if self.tile_video:
            self._tile = TileStream(session, fps=self.tile_video_fps)
            await self._tile.start()
            self._start_tile_relay_from_existing(room)

    async def on_caller_audio(self, pcm: bytes) -> None:
        """Publish the caller's voice into the room."""
        if self._closed:
            return
        source = self._source
        if source is None:
            # Held, not dropped. The room takes a moment to join, and what the
            # caller says in that moment is usually why they called.
            self._pending_audio.audio(pcm)
            return
        await self._publish_pcm(pcm)

    async def _publish_pcm(self, pcm: bytes) -> None:
        """Put one frame of caller audio into the room."""
        source = self._source
        if source is None:
            return
        await source.capture_frame(
            rtc.AudioFrame(
                data=pcm,
                sample_rate=SAMPLE_RATE_HZ,
                num_channels=NUM_CHANNELS,
                samples_per_channel=len(pcm) // 2,
            )
        )

    async def on_context(self, text: str) -> None:
        """Queue until the agent is bound, then publish on ``msteams.context``.

        The SDK delivers context the moment it arrives and does not queue, which
        is right: what "ready" means is a framework's own business. For LiveKit
        it means something specific - a data packet reaches only participants
        connected at that instant, and the first ``participants`` message
        arrives seconds before the dispatched agent joins. So the queue lives
        here.
        """
        if self._agent_identity is None:
            self._pending_context.append((TOPIC_CONTEXT, text))
            del self._pending_context[:-_MAX_PENDING_CONTEXT]
            return
        await self._publish_text(TOPIC_CONTEXT, text)

    async def on_goodbye(self, text: str) -> None:
        """Publish on ``msteams.goodbye``.

        Not queued: the SDK has already told StandIn to drop the agent's
        buffered audio, and teardown follows within seconds. A goodbye with no
        agent to hear it is a goodbye that was never going to be spoken.
        """
        await self._publish_text(TOPIC_GOODBYE, text)

    async def aclose(self, reason: str) -> None:
        """Release the room and every rtc primitive it owns."""
        self._closed = True

        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # rtc primitives own FFI subscriptions and internal tasks that only
        # aclose() releases: cancelling the tasks above frees neither, so every
        # finished call would leave one of each behind for the life of the
        # worker. Closed before the room teardown, so a hang there cannot skip
        # them.
        stream, self._audio_stream = self._audio_stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.aclose()
        source, self._source = self._source, None
        if source is not None:
            with contextlib.suppress(Exception):
                await source.aclose()

        self._stop_video_drain()
        tile, self._tile = self._tile, None
        if tile is not None:
            with contextlib.suppress(Exception):
                await tile.aclose()
        room, self._room = self._room, None
        if room is not None:
            with contextlib.suppress(Exception):
                await room.disconnect()
            await self._delete_room()

    # ---- LiveKit plumbing --------------------------------------------------

    def _call_metadata(self) -> dict[str, str]:
        call = self._call
        if call is None:
            return {}
        start = call.start
        # This shape is the contract CallInfo reads, and both SDKs write it
        # identically, so an agent moved between them needs no change.
        metadata: dict[str, str] = {
            "source": "msteams",
            "caller_name": start.caller.display_name or "caller",
            "tenant_id": start.tenant_id or start.caller.tenant_id or "unknown-tenant",
            "call_direction": start.direction,
            "call_id": call.call_id,
            "thread_id": start.thread_id,
        }
        if start.caller.aad_id:
            metadata["user_id"] = start.caller.aad_id
        return metadata

    async def _dispatch_agent(self) -> None:
        if not self.agent_name or self._call is None:
            return  # automatic dispatch reads the room metadata instead
        lkapi = api.LiveKitAPI(_http_url(self._livekit_url), self._api_key, self._api_secret)
        try:
            await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self.agent_name,
                    room=self._room_name,
                    metadata=json.dumps(self._call_metadata()),
                )
            )
            logger.info('standin: dispatched "%s" into %s', self.agent_name, self._room_name)
        finally:
            with contextlib.suppress(Exception):
                await lkapi.aclose()

    async def _delete_room(self) -> None:
        """Delete the room so the agent job ends at once instead of idling out."""
        if not self.delete_room_on_end or not self._room_name:
            return
        lkapi = api.LiveKitAPI(_http_url(self._livekit_url), self._api_key, self._api_secret)
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=self._room_name))
        except Exception as err:
            logger.warning("standin: delete_room failed, room will idle out: %s", err)
        finally:
            with contextlib.suppress(Exception):
                await lkapi.aclose()

    # ---- the avatar tile ----

    def _pick_avatar_participant(self, room: rtc.Room) -> Any | None:
        """Whose video goes on the tile.

        LiveKit's avatar framework runs the voice and the face on DIFFERENT
        participants and tags the second with publish-on-behalf, so that
        attribute is checked before the agent's own identity.
        """
        # Read defensively: this runs inside a room event callback, where an
        # AttributeError would be swallowed and the relay would simply never
        # start, with nothing in the log to say why.
        participants = getattr(room, "remote_participants", None) or {}
        remotes = list(participants.values())
        # A pinned identity wins outright: it is set precisely when guessing
        # would relay the wrong participant.
        if self.tile_video_identity:
            for participant in remotes:
                if participant.identity == self.tile_video_identity:
                    return participant
            return None
        for participant in remotes:
            attributes = getattr(participant, "attributes", None) or {}
            if attributes.get(_PUBLISH_ON_BEHALF) == self._agent_identity:
                return participant
        for participant in remotes:
            if participant.identity == self._agent_identity:
                return participant
        return None

    def _start_tile_relay_from_existing(self, room: rtc.Room) -> None:
        """Pick up a video track that subscribed before the identity bound."""
        if self._tile is None or self._tile_sid is not None:
            return
        chosen = self._pick_avatar_participant(room)
        if chosen is None:
            return
        for publication in (getattr(chosen, "track_publications", None) or {}).values():
            # Matched by KIND, never by source: an avatar worker publishes its
            # video untagged, so a source filter picks the right participant and
            # then streams nothing.
            if publication.kind == rtc.TrackKind.KIND_VIDEO and publication.track is not None:
                self._drain_video(publication.track, chosen.identity)
                return

    def _drain_video(self, track: rtc.Track, identity: str) -> None:
        """Feed one video track into the tile stream, latest-wins."""
        self._stop_video_drain()
        self._tile_sid = track.sid or "unknown"
        logger.info('standin: relaying avatar video from "%s"', identity)

        async def pump() -> None:
            stream = rtc.VideoStream.from_track(
                track=track, format=rtc.VideoBufferType.RGB24, capacity=1
            )
            try:
                async for event in stream:
                    if self._closed or self._tile is None:
                        break
                    frame = event.frame
                    self._tile.offer_rgb(bytes(frame.data), frame.width, frame.height)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._closed:
                    logger.warning("standin: the avatar video stream ended: %s", err)
            finally:
                with contextlib.suppress(Exception):
                    await stream.aclose()

        self._tile_task = asyncio.ensure_future(pump())

    def _stop_video_drain(self) -> None:
        self._tile_sid = None
        task, self._tile_task = self._tile_task, None
        if task is not None:
            task.cancel()

    def _wire_room_events(self, room: rtc.Room) -> None:
        @room.on("track_subscribed")
        def _on_track(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            # Bind the agent by participant KIND, not by whoever publishes audio
            # first: in a room with a recorder or a monitor, first-audio-wins
            # binds the wrong identity and then blocks the real agent behind the
            # single-pump gate.
            if self._agent_identity is None:
                if self.agent_name and not _is_agent(participant):
                    logger.debug('standin: ignoring audio from "%s"', participant.identity)
                    return
                self._agent_identity = participant.identity
                # The agent's own audio is what "answered" means. Without this
                # the core reaper would end a call an agent HAS taken but that
                # is still listening.
                if self._call is not None:
                    self._call.mark_answered()
                # The agent can hear us now: deliver the context that arrived
                # while the room had nobody to deliver it to.
                self._flush_pending_context()
            elif participant.identity != self._agent_identity:
                return
            self._start_pump(track)
            # The audio subscribe is what binds the agent identity. The avatar's
            # video may already be subscribed, and that event will not fire
            # again, so scan for it here or the relay never starts.
            self._start_tile_relay_from_existing(room)

        @room.on("track_subscribed")
        def _on_video(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.kind != rtc.TrackKind.KIND_VIDEO or self._tile is None:
                return
            if self._tile_sid is not None:
                return
            chosen = self._pick_avatar_participant(room)
            if chosen is None or chosen.identity != participant.identity:
                return
            self._drain_video(track, participant.identity)

        @room.on("track_unsubscribed")
        def _on_unsubscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.sid and track.sid == self._pump_sid:
                self._pump_sid = None  # a re-published track may take over

        @room.on("track_unsubscribed")
        def _on_video_unsubscribed(
            track: rtc.Track,
            publication: rtc.RemoteTrackPublication,
            participant: rtc.RemoteParticipant,
        ) -> None:
            if track.sid and track.sid == self._tile_sid:
                self._stop_video_drain()

        @room.on("participant_disconnected")
        def _on_left(participant: rtc.RemoteParticipant) -> None:
            if self._agent_identity and participant.identity == self._agent_identity:
                self._end_soon("agent-disconnected")

        @room.on("disconnected")
        def _on_disconnected(*_: Any) -> None:
            # Final by the time it fires: the SDK retries transient drops first.
            self._end_soon("room-disconnected")

    def _start_pump(self, track: rtc.Track) -> None:
        """Relay the agent's audio back to Microsoft Teams. One voice at a time.

        The outbound sequence number and timeline belong to the SDK now, so a
        re-published track (avatar swap, mute cycle) cannot make ``timestampMs``
        jump backwards - that used to be this method's job to get right.
        """
        if self._pump_sid:
            return
        sid = track.sid or "unknown"
        self._pump_sid = sid

        async def pump() -> None:
            stream: rtc.AudioStream | None = None
            try:
                # Ask the SDK for 16 kHz mono so our side stays copy-only.
                stream = rtc.AudioStream.from_track(
                    track=track, sample_rate=SAMPLE_RATE_HZ, num_channels=NUM_CHANNELS
                )
                self._audio_stream = stream
                async for event in stream:
                    call = self._call
                    if self._closed or call is None:
                        break
                    await call.send_audio(event.frame.data.tobytes())
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._closed:
                    logger.exception("standin: agent audio pump failed")
            finally:
                # Release the claim only if it is still OURS: a stale pump
                # draining its stream after a takeover must not clear the new
                # pump's claim and let a third pump start alongside it.
                if self._pump_sid == sid:
                    self._pump_sid = None
                if stream is not None:
                    if self._audio_stream is stream:
                        self._audio_stream = None
                    # The stream owns an FFI subscription and an internal task
                    # that only aclose() releases; ending the pump does not.
                    with contextlib.suppress(Exception):
                        await stream.aclose()

        self._spawn(pump())

    def _flush_pending_context(self) -> None:
        pending, self._pending_context = self._pending_context, []
        for topic, text in pending:
            self._spawn(self._publish_text(topic, text))

    async def _publish_text(self, topic: str, text: str) -> None:
        """The topic contract: both topics carry ``{"text": ...}``."""
        room = self._room
        if room is None or self._closed:
            return
        with contextlib.suppress(Exception):
            await room.local_participant.publish_data(
                json.dumps({"text": text}).encode("utf-8"), reliable=True, topic=topic
            )

    def _end_soon(self, reason: str) -> None:
        """End the call from a synchronous room callback.

        The SDK owns teardown and makes it idempotent with first-reason-wins, so
        this just asks; it does not need to guard against a second caller.
        """
        call = self._call
        if call is None or self._closed:
            return
        self._spawn(call.end(reason))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
