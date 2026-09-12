# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""A real Microsoft Teams call against a real CallServer, with no StandIn service.

Everything here drives the actual socket: it signs a handshake the way StandIn
does, opens the WebSocket, and speaks the wire protocol. That makes these tests
the executable spec of what a plugin can rely on, and the guard on the security
properties the server owes every plugin - the replay window, the single-use
handshake, and the callId binding between the signed path and session.start.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import socket

import aiohttp
import pytest

from standin import CallServer, CallSession
from standin._exceptions import StandInError
from standin._hmac import SIGNATURE_HEADER, TIMESTAMP_HEADER, now_ms, sign_handshake
from standin.call_server import MAX_AUDIO_BUFFER_BYTES

pytestmark = pytest.mark.unit

SECRET = "test-secret-never-a-real-one"
PCM = b"\x01\x02" * 160  # 320 bytes = 10 ms at 16 kHz mono


class RecordingHandler:
    """Records every callback, and echoes audio so the round trip is observable."""

    instances: list[RecordingHandler] = []

    def __init__(self) -> None:
        self.session: CallSession | None = None
        self.audio: list[bytes] = []
        self.context: list[str] = []
        self.goodbyes: list[str] = []
        self.frames: list = []
        self.speakers: list[str] = []
        self.closed_with: str | None = None
        RecordingHandler.instances.append(self)

    async def on_speaker_change(self, name: str) -> None:
        self.speakers.append(name)

    async def on_start(self, session: CallSession) -> None:
        self.session = session

    async def on_caller_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)
        assert self.session is not None
        await self.session.send_audio(pcm)  # echo

    async def on_video_frame(self, frame) -> None:
        self.frames.append(frame)

    async def on_context(self, text: str) -> None:
        self.context.append(text)

    async def on_goodbye(self, text: str) -> None:
        self.goodbyes.append(text)

    async def aclose(self, reason: str) -> None:
        self.closed_with = reason


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def server():
    RecordingHandler.instances.clear()
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        pre_start_timeout=5.0,
        audio_idle_timeout=0.0,  # off; exercised separately
    )
    await srv.start()
    try:
        yield srv
    finally:
        await srv.aclose()


def _headers(call_id: str, *, secret: str = SECRET, ts: int | None = None) -> dict[str, str]:
    stamp = now_ms() if ts is None else ts
    return {
        TIMESTAMP_HEADER: str(stamp),
        SIGNATURE_HEADER: sign_handshake(secret, stamp, call_id),
    }


def _url(server: CallServer, call_id: str) -> str:
    return f"http://127.0.0.1:{server._port}{server.ws_path}/{call_id}"


def _session_start(call_id: str) -> str:
    return json.dumps(
        {
            "type": "session.start",
            "callId": call_id,
            "threadId": "19:meeting@thread.v2",
            "direction": "inbound",
            "caller": {"displayName": "Alaa", "aadId": "aad-1", "tenantId": "tenant-1"},
        }
    )


async def _recv(ws: aiohttp.ClientWebSocketResponse, kind: str, timeout: float = 3.0) -> dict:
    """Next frame of the given type, ignoring others."""

    async def pump() -> dict:
        async for msg in ws:
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            if frame.get("type") == kind:
                return frame
        raise AssertionError(f"socket closed before a {kind!r} arrived")

    return await asyncio.wait_for(pump(), timeout)


@contextlib.contextmanager
def _wedged(session):
    """Pretend the socket is hopelessly behind, then put it back exactly.

    Replaces the real property rather than deleting it: deleting leaves the
    class without the attribute for every test that follows.
    """
    cls = type(session)
    original = cls.buffered_bytes
    cls.buffered_bytes = property(lambda self: MAX_AUDIO_BUFFER_BYTES + 1)
    try:
        yield
    finally:
        cls.buffered_bytes = original


def _audio_frame(pcm: bytes, speaker: str | None = None, seq: int = 1) -> str:
    frame = {
        "type": "audio.frame",
        "seq": seq,
        "timestampMs": 0,
        "payloadBase64": base64.b64encode(pcm).decode(),
    }
    if speaker is not None:
        frame["speakerName"] = speaker
    return json.dumps(frame)


async def _eventually(predicate, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait for a property that becomes true on the SERVER's timeline.

    A client leaving its `async with` does not wait for the server to finish
    tearing the call down - teardown still has to release the handler, send
    session.end and close the socket. Polling asserts the property that actually
    matters ("the slot is always freed") instead of guessing a sleep long enough
    to hide the race on a slow machine.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------- happy path


async def test_the_listener_says_whether_it_is_bound_and_refuses_to_bind_twice():
    """A host that connects twice binds a second listener and leaks the first,
    then reports a dead platform as connected."""
    server = CallServer(secret=SECRET, host="127.0.0.1", port=0, handler_factory=RecordingHandler)
    assert server.running is False
    assert server.port == 0
    await server.start()
    try:
        assert server.running is True
        # Port 0 asked for any free one; the caller has to be told which.
        assert server.port > 0
        with pytest.raises(StandInError, match="already running"):
            await server.start()
    finally:
        await server.aclose()
    assert server.running is False
    # Closing twice is not an error: teardown runs from more than one place.
    await server.aclose()


async def test_full_call_round_trip(server):
    """A whole call: authenticate, start, speak, get audio back, get context,
    take the goodbye, and end cleanly."""
    call_id = "call-happy"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))

            # the handler saw the call, with the caller identity intact
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            assert handler.session.call_id == call_id
            assert handler.session.start.caller.display_name == "Alaa"
            assert handler.session.start.direction == "inbound"

            # caller audio reaches the handler and the echo comes back framed
            await ws.send_str(
                json.dumps(
                    {
                        "type": "audio.frame",
                        "seq": 1,
                        "timestampMs": 0,
                        "payloadBase64": base64.b64encode(PCM).decode(),
                    }
                )
            )
            echoed = await _recv(ws, "audio.frame")
            assert base64.b64decode(echoed["payloadBase64"]) == PCM
            assert echoed["seq"] == 1
            assert echoed["timestampMs"] == 0
            assert handler.audio == [PCM]

            # the server owns the timeline: 320 bytes = 160 samples = 10 ms
            await ws.send_str(
                json.dumps(
                    {
                        "type": "audio.frame",
                        "seq": 2,
                        "timestampMs": 10,
                        "payloadBase64": base64.b64encode(PCM).decode(),
                    }
                )
            )
            second = await _recv(ws, "audio.frame")
            assert second["seq"] == 2
            assert second["timestampMs"] == 10

            # ping / pong echoes the timestamp verbatim
            await ws.send_str(json.dumps({"type": "ping", "ts": 12345}))
            assert (await _recv(ws, "pong"))["ts"] == 12345

            # context arrives as a ready-to-prompt sentence
            await ws.send_str(json.dumps({"type": "participants", "count": 3}))
            await ws.send_str(json.dumps({"type": "dtmf", "digit": "7"}))
            await ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))
            await asyncio.sleep(0.1)
            assert "3 human participants" in handler.context[0]
            assert '"7" key' in handler.context[1]
            assert "recording is now ACTIVE" in handler.context[2]

            # assistant.say flushes buffered playback FIRST, then delivers the line
            await ws.send_str(json.dumps({"type": "assistant.say", "text": "Goodbye now."}))
            cancel = await _recv(ws, "assistant.cancel")
            assert cancel["turnId"] == 2
            await asyncio.sleep(0.1)
            assert handler.goodbyes == ["Goodbye now."]

            # session.end tears down and the handler is released with the reason
            await ws.send_str(json.dumps({"type": "session.end", "reason": "caller-hung-up"}))
            await asyncio.sleep(0.2)

    await _eventually(lambda: server.active_calls == 0, what="the call slot to be freed")
    assert handler.closed_with == "caller-hung-up"


async def test_unknown_message_types_are_ignored(server):
    """Forward compatibility: a newer StandIn and an older plugin interoperate."""
    call_id = "call-unknown"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await ws.send_str(json.dumps({"type": "video.frame", "payloadBase64": "AAAA"}))
            await ws.send_str(json.dumps({"type": "expression", "name": "smile"}))
            await ws.send_str(json.dumps({"type": "a.type.from.the.future"}))
            await ws.send_str("not json at all")
            # still alive and serving
            await ws.send_str(json.dumps({"type": "ping", "ts": 9}))
            assert (await _recv(ws, "pong"))["ts"] == 9


async def test_malformed_audio_is_dropped_not_fatal(server):
    """An odd byte count would shift every later sample; drop the frame, keep
    the call."""
    call_id = "call-badpcm"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await ws.send_str(
                json.dumps(
                    {
                        "type": "audio.frame",
                        "payloadBase64": base64.b64encode(b"\x01\x02\x03").decode(),  # odd
                    }
                )
            )
            await ws.send_str(json.dumps({"type": "ping", "ts": 1}))
            assert (await _recv(ws, "pong"))["ts"] == 1
            await asyncio.sleep(0.05)
            assert RecordingHandler.instances[0].audio == []


# ------------------------------------------------------------------ security


async def test_bad_signature_is_rejected(server):
    call_id = "call-badsig"
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await http.ws_connect(
                _url(server, call_id), headers=_headers(call_id, secret="wrong-secret")
            )
        assert err.value.status == 401


async def test_missing_headers_fail_closed(server):
    call_id = "call-noauth"
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await http.ws_connect(_url(server, call_id))
        assert err.value.status == 401


async def test_stale_handshake_outside_replay_window(server):
    """Older than the freshness window is a replay, not a slow network."""
    call_id = "call-stale"
    stale = now_ms() - 120_000
    async with aiohttp.ClientSession() as http:
        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await http.ws_connect(_url(server, call_id), headers=_headers(call_id, ts=stale))
        assert err.value.status == 401


async def test_handshake_is_single_use(server):
    """A correctly signed upgrade replayed inside the freshness window must not
    open a second socket."""
    call_id = "call-replay"
    headers = _headers(call_id)
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=headers) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.05)
            # exact same timestamp + signature
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(_url(server, call_id), headers=headers)
            assert err.value.status == 401


async def test_replay_guard_normalizes_signature_casing(server):
    """verify() accepts case variants, so the replay cache must key on the
    normalized form or the same capture replays once per casing."""
    call_id = "call-casing"
    headers = _headers(call_id)
    upper = dict(headers)
    upper[SIGNATURE_HEADER] = headers[SIGNATURE_HEADER].upper()
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=headers) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.05)
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(_url(server, call_id), headers=upper)
            assert err.value.status == 401


async def test_session_start_callid_must_match_signed_path(server):
    """The path is what the HMAC signed. A body that disagrees is an attempt to
    ride one call's signature into another's session."""
    call_id = "call-bound"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start("a-different-call"))
            await _eventually(
                lambda: server.active_calls == 0, what="the mismatched call to be dropped"
            )
    # the handler is built only AFTER the callId check, so none exists here
    assert RecordingHandler.instances == []


async def test_second_session_for_same_call_id_is_refused(server):
    call_id = "call-dup"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.05)
            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(_url(server, call_id), headers=_headers(call_id))
            assert err.value.status == 409


# ------------------------------------------------------------------ capacity


async def test_capacity_refuses_before_spending_crypto():
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        max_connections=1,
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "c1"), headers=_headers("c1")) as ws:
                await ws.send_str(_session_start("c1"))
                await asyncio.sleep(0.05)
                # a VALID handshake, refused purely on capacity
                with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                    await http.ws_connect(_url(srv, "c2"), headers=_headers("c2"))
                assert err.value.status == 503
    finally:
        await srv.aclose()


async def test_draining_refuses_new_calls_but_keeps_live_ones(server):
    call_id = "call-drain"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.05)
            server.draining = True

            with pytest.raises(aiohttp.WSServerHandshakeError) as err:
                await http.ws_connect(_url(server, "call-new"), headers=_headers("call-new"))
            assert err.value.status == 503

            # the live one still works
            await ws.send_str(json.dumps({"type": "ping", "ts": 42}))
            assert (await _recv(ws, "pong"))["ts"] == 42


async def test_pre_start_timeout_frees_the_slot():
    """A socket that authenticates and never starts holds a callId nothing would
    otherwise free."""
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        pre_start_timeout=0.2,
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "silent"), headers=_headers("silent")):
                assert srv.active_calls == 1
                await _eventually(
                    lambda: srv.active_calls == 0, what="the silent socket to be dropped"
                )
    finally:
        await srv.aclose()


async def test_healthz_reports_live_calls(server):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"http://127.0.0.1:{server._port}/healthz") as resp:
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "calls": 0}


# ------------------------------------------------------------------- handler


async def test_handler_exception_ends_only_that_call(server):
    """A plugin raising must end its own call, not the worker."""

    class Exploding:
        async def on_caller_audio(self, pcm: bytes) -> None:
            raise RuntimeError("plugin bug")

    srv = CallServer(
        handler_factory=Exploding,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "boom"), headers=_headers("boom")) as ws:
                await ws.send_str(_session_start("boom"))
                await ws.send_str(
                    json.dumps(
                        {"type": "audio.frame", "payloadBase64": base64.b64encode(PCM).decode()}
                    )
                )
                await _eventually(lambda: srv.active_calls == 0, what="the exploding call to end")
        # the server itself is still listening and serving
        async with aiohttp.ClientSession() as http:
            async with http.get(f"http://127.0.0.1:{srv._port}/healthz") as resp:
                assert resp.status == 200
    finally:
        await srv.aclose()


async def test_partial_handler_is_valid(server):
    """Every callback is optional: a handler that only wants audio implements
    only on_caller_audio."""

    got: list[bytes] = []

    class AudioOnly:
        async def on_caller_audio(self, pcm: bytes) -> None:
            got.append(pcm)

    srv = CallServer(
        handler_factory=AudioOnly,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "partial"), headers=_headers("partial")) as ws:
                await ws.send_str(_session_start("partial"))
                await ws.send_str(
                    json.dumps(
                        {"type": "audio.frame", "payloadBase64": base64.b64encode(PCM).decode()}
                    )
                )
                await ws.send_str(json.dumps({"type": "participants", "count": 1}))
                await ws.send_str(json.dumps({"type": "assistant.say", "text": "bye"}))
                await asyncio.sleep(0.2)
                await ws.send_str(json.dumps({"type": "ping", "ts": 5}))
                assert (await _recv(ws, "pong"))["ts"] == 5
        assert got == [PCM]
    finally:
        await srv.aclose()


async def test_sync_handler_methods_are_accepted(server):
    """A handler with nothing to await should not be forced to declare async."""

    seen: list[str] = []

    class Sync:
        def on_start(self, session: CallSession) -> None:
            seen.append(session.call_id)

        def on_context(self, text: str) -> None:
            seen.append(text)

    srv = CallServer(
        handler_factory=Sync,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "sync"), headers=_headers("sync")) as ws:
                await ws.send_str(_session_start("sync"))
                await ws.send_str(json.dumps({"type": "participants", "count": 1}))
                await asyncio.sleep(0.15)
        assert seen[0] == "sync"
        assert "1:1 call" in seen[1]
    finally:
        await srv.aclose()


async def test_audio_idle_watchdog_ends_a_wedged_call():
    """A live Microsoft Teams call streams PCM continuously. Silence means the far side is
    gone and nobody told us."""
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.4,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "wedged"), headers=_headers("wedged")) as ws:
                await ws.send_str(_session_start("wedged"))
                await asyncio.sleep(0.1)
                assert srv.active_calls == 1
                # keep pinging, send no audio - the wedged-peer case exactly.
                # The socket goes away underneath us when the watchdog fires,
                # which is the point of the test, so stop writing then.
                for _ in range(10):
                    if ws.closed or srv.active_calls == 0:
                        break
                    try:
                        await ws.send_str(json.dumps({"type": "ping", "ts": 1}))
                    except Exception:
                        break
                    await asyncio.sleep(0.15)
                await _eventually(
                    lambda: srv.active_calls == 0, what="the idle watchdog to end the call"
                )
        assert RecordingHandler.instances[-1].closed_with == "caller-idle-timeout"
    finally:
        await srv.aclose()


# --------------------------------------------------------- barge-in + on_start
# Regression tests for defects found by an adversarial review of this SDK.


async def test_cancel_playback_emits_the_frame(server):
    """The only lever that un-sends audio already handed to StandIn. Without it a
    barge-in stops the model but the bot keeps talking for the length of the
    buffered PCM."""
    call_id = "call-cancel"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None

            await handler.session.send_audio(PCM)
            await _recv(ws, "audio.frame")

            await handler.session.cancel_playback()
            cancel = await _recv(ws, "assistant.cancel")
            assert cancel["turnId"] == 1


async def test_send_audio_still_works_during_aclose():
    """The seam documents that a handler can speak on the way out. Gating
    send_audio on the closed FLAG rather than the socket silently broke it."""
    spoken: list[bytes] = []

    class FarewellHandler:
        async def on_start(self, session):
            self.session = session

        async def aclose(self, reason: str) -> None:
            await self.session.send_audio(PCM)
            spoken.append(PCM)

    srv = CallServer(
        handler_factory=FarewellHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "bye"), headers=_headers("bye")) as ws:
                await ws.send_str(_session_start("bye"))
                await asyncio.sleep(0.1)
                await ws.send_str(json.dumps({"type": "session.end", "reason": "caller-hung-up"}))
                # the farewell must reach the wire before the socket closes
                frame = await _recv(ws, "audio.frame")
                assert base64.b64decode(frame["payloadBase64"]) == PCM
        assert spoken == [PCM]
    finally:
        await srv.aclose()


async def test_a_hung_on_start_does_not_leak_the_slot():
    """on_start does real network work and the frame loop is suspended while it
    runs. Unbounded, a hung one holds the callId forever and every retry 409s -
    one leaked slot per inbound call, up to max_connections."""

    class Hangs:
        async def on_start(self, session):
            await asyncio.sleep(60)

    srv = CallServer(
        handler_factory=Hangs,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        on_start_timeout=0.3,
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "hung"), headers=_headers("hung")) as ws:
                await ws.send_str(_session_start("hung"))
                await _eventually(
                    lambda: srv.active_calls == 0, what="the hung on_start to be given up"
                )
    finally:
        await srv.aclose()


async def test_on_start_failure_is_not_reported_as_a_transport_failure():
    """A third-party outage inside a plugin must not be reported to StandIn as
    StandIn's own socket failing - that sends both sides debugging the wrong
    system."""
    reasons: list[str] = []

    class Explodes:
        async def on_start(self, session):
            raise RuntimeError("the provider is down")

        async def aclose(self, reason: str) -> None:
            reasons.append(reason)

    srv = CallServer(
        handler_factory=Explodes,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "boom2"), headers=_headers("boom2")) as ws:
                await ws.send_str(_session_start("boom2"))
                await _eventually(lambda: srv.active_calls == 0, what="the failed start to end")
        assert reasons == ["handler-start-failure"]
    finally:
        await srv.aclose()


async def test_refusing_the_call_from_on_start_does_not_tear_down_early():
    """A handler that refuses with session.end() from inside on_start used to get
    its aclose run while on_start was still on the stack: half-built state torn
    down, then on_start resumes and finishes building a session nothing will ever
    close - one leaked provider socket per refusal."""
    order: list[str] = []

    class Refuses:
        async def on_start(self, session):
            order.append("start-begin")
            await session.end("not-allowed")
            await asyncio.sleep(0.05)
            order.append("start-end")

        async def aclose(self, reason: str) -> None:
            order.append(f"aclose:{reason}")

    srv = CallServer(
        handler_factory=Refuses,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
    )
    await srv.start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, "refuse"), headers=_headers("refuse")) as ws:
                await ws.send_str(_session_start("refuse"))
                await _eventually(lambda: srv.active_calls == 0, what="the refused call to end")
        assert order == ["start-begin", "start-end", "aclose:not-allowed"], order
    finally:
        await srv.aclose()


# --------------------------------------------------------------- vision lane


def _video_frame(source: str = "screenshare", **overrides) -> str:
    frame = {
        "type": "video.frame",
        "source": source,
        "ts": 1_738_000_000_000,
        "width": 1280,
        "height": 720,
        "mime": "image/jpeg",
        "dataBase64": base64.b64encode(b"\xff\xd8\xff\xe0jpeg").decode(),
        "participantId": "aad-1",
        "participantName": "Alaa",
    }
    frame.update(overrides)
    return json.dumps(frame)


async def test_video_frames_reach_the_handler_and_the_latest_is_kept(server):
    """The whole vision lane over a real socket: a frame arrives, the callback
    sees it, and the session keeps the latest per source for a model that asks
    to look long afterwards."""
    call_id = "call-vision"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None

            await ws.send_str(_video_frame("camera", width=320, height=240))
            await ws.send_str(_video_frame("screenshare"))
            await _eventually(lambda: len(handler.frames) == 2, what="two video frames")

            camera, share = handler.frames
            assert camera.source == "camera"
            assert (camera.width, camera.height) == (320, 240)
            assert share.participant_name == "Alaa"
            assert share.data.startswith(b"\xff\xd8\xff")

            # No source: the screen share wins, because an agent asked to look
            # is nearly always being asked about what is being SHOWN.
            assert handler.session.latest_video_frame().source == "screenshare"
            assert handler.session.latest_video_frame("camera").width == 320
            assert handler.session.latest_video_frame("screenshare").width == 1280

            # A second frame replaces the first rather than accumulating: only
            # the latest matters, and a history would be an unbounded buffer of
            # the caller's screen.
            await ws.send_str(_video_frame("screenshare", width=1920, height=1080))
            await _eventually(
                lambda: handler.session.latest_video_frame("screenshare").width == 1920,
                what="the newer share frame",
            )


async def test_malformed_video_is_dropped_not_fatal(server):
    """Frames are sparse and best-effort. An unusable one is dropped, the call
    stays up, and the caller keeps talking."""
    call_id = "call-bad-video"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]

            await ws.send_str(_video_frame(dataBase64="not base64!"))
            await ws.send_str(_video_frame("whiteboard"))
            await ws.send_str(_video_frame(width=0))

            # The call is still live and still carrying audio.
            await ws.send_str(
                json.dumps(
                    {
                        "type": "audio.frame",
                        "seq": 1,
                        "timestampMs": 0,
                        "payloadBase64": base64.b64encode(PCM).decode(),
                    }
                )
            )
            await _recv(ws, "audio.frame")
            assert handler.frames == []
            assert handler.session.latest_video_frame() is None


async def test_display_image_reaches_the_service(server):
    """The agent's half of the lane: what the bot shows on its own tile."""
    call_id = "call-display"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None

            await handler.session.display_image(
                b"\xff\xd8\xff\xe0chart", caption="Q3 revenue", duration_ms=4000
            )
            shown = await _recv(ws, "display.image")
            assert shown["caption"] == "Q3 revenue"
            assert shown["durationMs"] == 4000
            assert shown["mime"] == "image/jpeg"
            assert base64.b64decode(shown["dataBase64"]) == b"\xff\xd8\xff\xe0chart"


# ------------------------------------------------- the session's own readings


async def test_recording_flag_follows_the_call(server):
    """One flag, kept current by the server. Every plugin used to re-derive this
    by string-matching the context sentence, which is how five copies drifted."""
    call_id = "call-recording"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            assert handler.session.recording_active is False

            await ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))
            await _eventually(
                lambda: handler.session.recording_active is True, what="recording to turn on"
            )

            await ws.send_str(json.dumps({"type": "recording.status", "status": "inactive"}))
            await _eventually(
                lambda: handler.session.recording_active is False, what="recording to turn off"
            )


async def test_a_reported_recording_status_is_not_undone_by_session_start(server):
    """recording.status can land BEFORE session.start, and session.start omits
    the field when the state was unknown at answer time. Seeding from the
    snapshot unconditionally turns a live ACTIVE into False, and every
    recording-gated capability stays shut for the whole call with nothing said."""
    call_id = "call-recording-race"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))
            await asyncio.sleep(0.05)
            await ws.send_str(_session_start(call_id))  # carries no recordingStatus
            await asyncio.sleep(0.1)

            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            assert handler.session.recording_active is True


async def test_a_later_report_still_wins_over_the_latch(server):
    """The latch stops the snapshot downgrading a report. It must not stop a
    real later report from turning recording off."""
    call_id = "call-recording-latch"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))
            await asyncio.sleep(0.05)
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session.recording_active is True

            await ws.send_str(json.dumps({"type": "recording.status", "status": "inactive"}))
            await _eventually(
                lambda: handler.session.recording_active is False, what="recording to turn off"
            )


async def test_the_active_speaker_reaches_the_handler(server):
    """The wire carries speakerName on every frame of unmixed audio. The server
    used to read it and throw it away, so nothing could attribute a transcript."""
    call_id = "call-speaker"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]

            for name in ("Dana", "Dana", "Dana", "Ali", "Ali", "Dana"):
                await ws.send_str(_audio_frame(b"\x00\x00" * 160, speaker=name))
            await _eventually(lambda: len(handler.speakers) == 3, what="three speaker changes")

            # On CHANGE only. The name rides every frame, and a model told forty
            # times a second who is speaking would hear nothing else.
            assert handler.speakers == ["Dana", "Ali", "Dana"]
            assert handler.session.speaker == "Dana"


async def test_a_mixed_call_never_reports_a_speaker(server):
    """speakerName is absent on the mixed path, which is most calls."""
    call_id = "call-speaker-mixed"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            await ws.send_str(_audio_frame(b"\x00\x00" * 160))
            await asyncio.sleep(0.1)
            assert handler.speakers == []
            assert handler.session.speaker is None


async def test_the_participant_count_reaches_the_handler(server):
    """The sentence is for a model. The number is for a plugin that has to
    branch on it, and re-parsing the sentence to get it back is how five copies
    of the same regex appeared."""
    call_id = "call-participants"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session.participant_count == 0

            await ws.send_str(json.dumps({"type": "participants", "count": 4}))
            await _eventually(
                lambda: handler.session.participant_count == 4, what="the count to arrive"
            )


async def test_agent_audio_is_shed_when_the_peer_stops_reading(server):
    """A peer that stops reading turns every send into a queue, and those awaits
    stall the provider loop that feeds them. The caller hears a gap; the call
    does not wedge."""
    call_id = "call-shedding"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session

            with _wedged(session):
                before = session._seq
                for _ in range(5):
                    await session.send_audio(b"\x01\x02" * 160)
                assert session._audio_dropped == 5
                # The sequence still climbs: the frames were real, they were
                # just not sent.
                assert session._seq == before + 5


async def test_a_shed_frame_leaves_a_gap_not_a_rewind(server):
    """The timeline is the caller's clock. Stalling it while frames are dropped
    would make every later frame claim a time that has already passed."""
    call_id = "call-shedding-clock"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session

            await session.send_audio(b"\x01\x02" * 160)
            after_first = session.media_time_ms

            with _wedged(session):
                await session.send_audio(b"\x01\x02" * 160)
            assert session.media_time_ms > after_first

            await session.send_audio(b"\x03\x04" * 160)
            sent = await _recv(ws, "audio.frame")
            while base64.b64decode(sent["payloadBase64"]) != b"\x03\x04" * 160:
                sent = await _recv(ws, "audio.frame")
            # The frame after the gap is stamped where it really belongs.
            assert sent["timestampMs"] >= after_first + 10


async def test_a_call_that_will_not_end_is_ended():
    """The idle watchdog ends a call that went QUIET. This ends one that has
    not: a caller who will not hang up, a model looping at itself, an automated
    system that dialled and never stopped talking. None of those trips a
    silence check, and every one bills a provider by the minute."""
    RecordingHandler.instances.clear()
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
        max_call_seconds=0.2,
        goodbye_text="Out of time, goodbye.",
        goodbye_grace=0.05,
    )
    await srv.start()
    try:
        call_id = "call-ceiling"
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, call_id), headers=_headers(call_id)) as ws:
                await ws.send_str(_session_start(call_id))
                await _eventually(
                    lambda: bool(RecordingHandler.instances), what="the handler to be built"
                )
                handler = RecordingHandler.instances[0]

                # The closing line reaches the handler through on_goodbye, the
                # same callback StandIn's own closing line uses, so a plugin
                # needs no new code for this.
                await _eventually(
                    lambda: handler.goodbyes == ["Out of time, goodbye."],
                    what="the goodbye to be delivered",
                )
                await _eventually(
                    lambda: handler.closed_with == "call-duration-limit",
                    what="the call to end on its limit",
                )
    finally:
        await srv.aclose()


async def test_the_ceiling_flushes_playback_before_it_speaks():
    """Otherwise the line queues behind however many seconds of agent audio the
    service still holds, and the call ends before anyone hears it."""
    RecordingHandler.instances.clear()
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
        max_call_seconds=0.2,
        goodbye_grace=0.05,
    )
    await srv.start()
    try:
        call_id = "call-ceiling-flush"
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, call_id), headers=_headers(call_id)) as ws:
                await ws.send_str(_session_start(call_id))
                cancel = await _recv(ws, "assistant.cancel", timeout=3.0)
                assert cancel["type"] == "assistant.cancel"
    finally:
        await srv.aclose()


async def test_no_ceiling_by_default(server):
    """A hard cap on a live call is an operator's decision, not a default."""
    assert server.max_call_seconds == 0.0


def test_the_reaper_boundary_is_exact():
    """Pure, with the clock passed in, so the boundary is testable without
    sleeping. Strictly greater, so a tick landing exactly on the grace does not
    reap a call one instant early."""
    from standin.call_server import _unanswered

    class Fake:
        _answered_at = None
        _started_at = 100.0

    call = Fake()
    assert _unanswered(call, 10.0, 109.9) is False
    assert _unanswered(call, 10.0, 110.0) is False  # exactly the grace: not yet
    assert _unanswered(call, 10.0, 110.1) is True
    # Disabled means never.
    assert _unanswered(call, 0.0, 1_000.0) is False
    # And an answered call is never stale.
    call._answered_at = 101.0
    assert _unanswered(call, 10.0, 1_000.0) is False


async def test_a_call_nothing_answers_is_ended():
    """An agent dispatch that never lands is invisible to every other watchdog:
    session.start arrived, on_start succeeded, and the caller keeps talking."""
    RecordingHandler.instances.clear()
    srv = CallServer(
        handler_factory=RecordingHandler,
        secret=SECRET,
        host="127.0.0.1",
        port=_free_port(),
        audio_idle_timeout=0.0,
        stale_call_reaper_seconds=0.15,
    )
    await srv.start()
    try:
        call_id = "call-unanswered"
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(_url(srv, call_id), headers=_headers(call_id)) as ws:
                await ws.send_str(_session_start(call_id))
                await _eventually(
                    lambda: bool(RecordingHandler.instances), what="the handler to be built"
                )
                handler = RecordingHandler.instances[0]
                await _eventually(
                    lambda: handler.closed_with == "no-agent-answered",
                    what="the call to be reaped",
                )
    finally:
        await srv.aclose()


async def test_sending_audio_counts_as_answering(server):
    """Every plugin is covered without doing anything: a provider that connects
    and then produces no audio at all is reaped exactly like an agent that
    never joined."""
    call_id = "call-answered-by-audio"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session.answered is False
            await session.send_audio(b"\x01\x02" * 160)
            assert session.answered is True


async def test_an_agent_that_listens_first_can_say_so(server):
    """A listen-first agent joins and stays quiet, and must not be reaped for
    it."""
    call_id = "call-listen-first"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            session.mark_answered()
            assert session.answered is True
            # Stamped once, never re-stamped.
            first = session._answered_at
            session.mark_answered()
            assert session._answered_at == first


async def test_recording_flag_starts_from_session_start(server):
    call_id = "call-recording-start"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            start = json.loads(_session_start(call_id))
            start["recordingStatus"] = "active"
            await ws.send_str(json.dumps(start))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            assert handler.session.recording_active is True


async def test_the_media_clock_is_the_outbound_audio_timeline(server):
    """Video frames must be stamped with this, not a wall clock: a wall clock
    keeps ticking through listening silence while this one does not."""
    call_id = "call-clock"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            assert handler.session.media_time_ms == 0

            # 320 bytes is 160 samples is 10 ms at 16 kHz.
            await handler.session.send_audio(PCM)
            await _recv(ws, "audio.frame")
            assert handler.session.media_time_ms == 10

            await handler.session.send_audio(PCM)
            await _recv(ws, "audio.frame")
            assert handler.session.media_time_ms == 20

            # Silence does not advance it. That is the whole point.
            await asyncio.sleep(0.05)
            assert handler.session.media_time_ms == 20


async def test_buffered_bytes_is_readable_and_never_raises(server):
    """Zero means no evidence of backpressure, not proof of an idle socket."""
    call_id = "call-buffered"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            assert handler.session is not None
            value = handler.session.buffered_bytes
            assert isinstance(value, int)
            assert value >= 0


# --------------------------------------------------------------- the tile lane


async def test_tile_frames_carry_the_audio_clock_and_their_own_sequence(server):
    """The tile is a separate stream from the audio: its own sequence, but the
    SAME clock, so the two cannot disagree about what time it is."""
    call_id = "call-tile"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            handler = RecordingHandler.instances[0]
            session = handler.session
            assert session is not None

            await session.send_tile_frame(b"\xff\xd8\xff\xe0one", 640, 360)
            first = await _recv(ws, "display.frame")
            assert first["seq"] == 1
            assert first["ts"] == 0
            assert first["width"] == 640

            # Ten milliseconds of audio moves the clock the video rides.
            await session.send_audio(PCM)
            await _recv(ws, "audio.frame")
            await session.send_tile_frame(b"\xff\xd8\xff\xe0two", 640, 360)
            second = await _recv(ws, "display.frame")
            assert second["seq"] == 2
            assert second["ts"] == 10


async def test_the_tile_stream_paces_and_keeps_only_the_newest_frame(server):
    """Offered faster than the wire, the middle frames are dropped rather than
    the stream falling behind."""
    from standin import TileStream

    call_id = "call-tile-pace"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session is not None

            tile = TileStream(session, fps=20)
            await tile.start()
            try:
                for i in range(30):
                    tile.offer_jpeg(f"frame-{i}".encode())
                    await asyncio.sleep(0.005)
                await _eventually(lambda: tile.frames_sent >= 2, what="paced frames")
            finally:
                await tile.aclose()

            # Paced, so far fewer than the 30 offered went out.
            assert tile.frames_sent < 30


async def test_a_source_that_stops_leaves_a_silent_wire(server):
    """Each offered frame is sent at most once. A stalled source means silence,
    not one stale frame repeated forever."""
    from standin import TileStream

    call_id = "call-tile-quiet"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session is not None

            tile = TileStream(session, fps=20)
            await tile.start()
            try:
                tile.offer_jpeg(b"the only frame")
                await _eventually(lambda: tile.frames_sent == 1, what="the one frame")
                await asyncio.sleep(0.2)
                assert tile.frames_sent == 1
            finally:
                await tile.aclose()


async def test_the_tile_stream_needs_no_encoder_for_ready_made_jpeg(server):
    """A source that already produces JPEG costs no optional dependency."""
    from standin import TileStream

    call_id = "call-tile-jpeg"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session is not None

            tile = TileStream(session, fps=20, encoder=None)
            await tile.start()
            try:
                tile.offer_jpeg(b"\xff\xd8\xff\xe0ready")
                frame = await _recv(ws, "display.frame")
                assert base64.b64decode(frame["dataBase64"]) == b"\xff\xd8\xff\xe0ready"
            finally:
                await tile.aclose()


async def test_the_tile_uses_the_encoder_it_is_given(server):
    from standin import TileStream

    call_id = "call-tile-encode"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session is not None

            seen: list[tuple[int, int]] = []

            def encode(rgb: bytes, width: int, height: int) -> bytes:
                seen.append((width, height))
                return b"encoded:" + rgb[:4]

            tile = TileStream(session, fps=20, encoder=encode)
            await tile.start()
            try:
                tile.offer_rgb(b"\x01\x02\x03" * 8, 4, 2)
                frame = await _recv(ws, "display.frame")
                assert base64.b64decode(frame["dataBase64"]).startswith(b"encoded:")
                assert seen == [(4, 2)]
                # Encoded frames are reported at the tile size they were resized to.
                assert (frame["width"], frame["height"]) == (640, 360)
            finally:
                await tile.aclose()


async def test_the_tile_drops_frames_to_protect_the_audio(server):
    """Both streams share a socket. A caller forgives a dropped frame far more
    readily than a break in the voice."""
    from standin import TileStream

    call_id = "call-tile-budget"
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(_url(server, call_id), headers=_headers(call_id)) as ws:
            await ws.send_str(_session_start(call_id))
            await asyncio.sleep(0.1)
            session = RecordingHandler.instances[0].session
            assert session is not None

            # A budget of zero means every frame is over it.
            tile = TileStream(session, fps=20, max_buffered_bytes=-1)
            await tile.start()
            try:
                for _ in range(5):
                    tile.offer_jpeg(b"dropped")
                    await asyncio.sleep(0.02)
                await _eventually(lambda: tile.frames_dropped >= 1, what="a dropped frame")
                assert tile.frames_sent == 0
            finally:
                await tile.aclose()
