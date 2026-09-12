# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""LiveKit SDK boundaries exercised locally, without a room server or model."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("standin.plugins.livekit", reason='needs pip install "standin-sdk[livekit]"')

from livekit import api  # noqa: E402
from livekit.agents import Agent, AgentSession, llm  # noqa: E402
from livekit.agents.voice.agent_activity import AgentActivity  # noqa: E402

from standin import Caller, SessionStart  # noqa: E402
from standin.plugins.livekit import CallInfo, TeamsCall, TeamsCallHandler  # noqa: E402
from standin.plugins.livekit import handler as handler_module  # noqa: E402

pytestmark = pytest.mark.unit


class _Room:
    # Mirrors the real room: the avatar relay looks here for the face.
    remote_participants: dict = {}

    def __init__(self):
        self.token = ""
        self.metadata = ""
        self.callbacks = {}
        self.local_participant = SimpleNamespace(publish_track=AsyncMock())

    def on(self, event, callback=None):
        def register(fn):
            self.callbacks[event] = fn
            return fn

        return register(callback) if callback is not None else register

    async def connect(self, url, token, options):
        self.token = token

    async def disconnect(self):
        pass


async def test_automatic_dispatch_carries_context_in_the_signed_room_configuration(monkeypatch):
    """Verify real LiveKit token/protobuf APIs and the pre-connect metadata path."""
    room = _Room()
    source = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(handler_module.rtc, "Room", lambda: room)
    monkeypatch.setattr(handler_module.rtc, "AudioSource", lambda *_: source)
    monkeypatch.setattr(
        handler_module.rtc.LocalAudioTrack, "create_audio_track", lambda *_: object()
    )
    handler = TeamsCallHandler(
        agent_name="",
        livekit_url="wss://livekit.invalid",
        livekit_api_key="test-key",
        livekit_api_secret="s" * 32,
        delete_room_on_end=False,
    )
    call = SimpleNamespace(
        call_id="call-1",
        start=SessionStart(
            "call-1", "thread-1", Caller("aad-1", "Alaa", "caller-tenant"), tenant_id="tenant-1"
        ),
    )
    await handler.on_start(call)
    try:
        claims = api.TokenVerifier("test-key", "s" * 32).verify(room.token)
        metadata = claims.room_config.metadata
        assert json.loads(metadata)["call_id"] == "call-1"
        # The worker receives this room snapshot in the job before ctx.room
        # connects. Automatic dispatch leaves job.metadata empty.
        ctx = SimpleNamespace(job=api.Job(room=api.Room(metadata=metadata)), room=room)
        info = await TeamsCall().start(AgentSession(), ctx=ctx)
        assert info.is_teams_call
        assert info.call_id == "call-1"
        assert info.caller_name == "Alaa"
        assert info.tenant_id == "tenant-1"
        assert info.user_id == "aad-1"
        assert "data_received" in room.callbacks
    finally:
        await handler.aclose("test-ended")
    source.aclose.assert_awaited_once()


def test_explicit_dispatch_metadata_takes_precedence_over_room_metadata():
    ctx = SimpleNamespace(
        job=api.Job(
            metadata=json.dumps({"source": "msteams", "call_id": "explicit-call"}),
            room=api.Room(metadata=json.dumps({"source": "msteams", "call_id": "room-call"})),
        )
    )
    assert CallInfo.from_job(ctx).call_id == "explicit-call"


def test_connected_room_metadata_is_used_when_the_job_snapshot_has_none():
    ctx = SimpleNamespace(
        job=api.Job(),
        room=SimpleNamespace(metadata=json.dumps({"source": "msteams", "call_id": "room-call"})),
    )
    assert CallInfo.from_job(ctx).call_id == "room-call"


async def test_realtime_goodbye_uses_generation_when_the_installed_sdk_cannot_say(monkeypatch):
    model = llm.RealtimeModel(
        capabilities=llm.RealtimeCapabilities(
            message_truncation=True,
            turn_detection=True,
            user_transcription=True,
            auto_tool_reply_generation=False,
            audio_output=True,
            manual_function_calls=True,
        )
    )
    session = AgentSession(llm=model)
    activity = AgentActivity(Agent(instructions="Test"), session)
    activity._scheduling_paused = False
    session._activity = activity
    session.output._audio_sink = object()
    # This exercises the real SDK rejection that the old handler swallowed.
    with pytest.raises(RuntimeError, match="without a TTS model"):
        session.say("Goodbye.")
    interrupt = Mock()
    generate = Mock()
    monkeypatch.setattr(session, "interrupt", interrupt)
    monkeypatch.setattr(session, "generate_reply", generate)
    call = TeamsCall()
    call._session = session
    call._handle_goodbye("Goodbye.")
    interrupt.assert_called_once_with()
    generate.assert_called_once_with(
        instructions="Say this goodbye to the caller, then stop: Goodbye."
    )


@pytest.mark.parametrize("has_tts,supports_say", [(True, False), (False, True)])
def test_goodbye_keeps_exact_say_for_sessions_that_support_it(has_tts, supports_say):
    session = SimpleNamespace(
        tts=object() if has_tts else None,
        llm=SimpleNamespace(capabilities=SimpleNamespace(supports_say=supports_say)),
        interrupt=Mock(),
        say=Mock(),
        generate_reply=Mock(),
    )
    call = TeamsCall()
    call._session = session
    call._handle_goodbye("Exact goodbye.")
    session.say.assert_called_once_with("Exact goodbye.")
    session.generate_reply.assert_not_called()
