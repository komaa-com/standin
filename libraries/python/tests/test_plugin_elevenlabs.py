# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The ElevenLabs relay, driven against a fake agent socket.

No network and no API key: the agent socket is replaced with a recorder, so
these tests assert the RELAY - what reaches the caller, what reaches the agent,
and what happens on a barge-in - rather than re-testing ElevenLabs.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from standin.plugins.elevenlabs import ElevenLabsConfig, ElevenLabsHandler, client_tools
from standin.plugins.elevenlabs import handler as handler_module
from standin.protocol import Caller, SessionStart
from standin.vision import VideoFrame

pytestmark = pytest.mark.unit

PCM = b"\x01\x02" * 160
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0frame").decode()


def _config(**overrides: Any) -> ElevenLabsConfig:
    base = {"api_key": "key-never-real", "agent_id": "agent-1"}
    base.update(overrides)
    return ElevenLabsConfig(**base)


class FakeAgent:
    """Stands in for the ElevenLabs socket, recording what the relay sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.audio: list[str] = []
        self.closed = False
        self.is_open = True
        self.conversation_id = "conv-1"
        self.attached: list[tuple[bytes, str, str]] = []
        self.attach_error: Exception | None = None

    def send_conversation_init(self, init: dict[str, Any]) -> None:
        self.sent.append(init)

    def send_audio_chunk(self, chunk: str) -> None:
        self.audio.append(chunk)

    def send_contextual_update(self, text: str) -> None:
        self.sent.append({"type": "contextual_update", "text": text})

    def send_user_message(self, text: str) -> None:
        self.sent.append({"type": "user_message", "text": text})

    def send_pong(self, event_id: int) -> None:
        self.sent.append({"type": "pong", "event_id": event_id})

    def send_tool_result(self, tool_call_id: str, result: str, is_error: bool = False) -> None:
        self.sent.append(
            {"type": "tool_result", "id": tool_call_id, "result": result, "isError": is_error}
        )

    async def attach_image(self, data: bytes, mime: str, question: str) -> None:
        if self.attach_error is not None:
            raise self.attach_error
        self.attached.append((data, mime, question))

    async def aclose(self) -> None:
        self.closed = True
        self.is_open = False

    def results(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["type"] == "tool_result"]


class FakeCall:
    """The CallSession surface, recording what reaches the caller."""

    def __init__(
        self, recording: str | None = None, frames: dict[str, VideoFrame] | None = None
    ) -> None:
        self.call_id = "call-1"
        self.start = SessionStart(
            call_id="call-1",
            thread_id="19:meeting@thread.v2",
            caller=Caller(aad_id="aad-1", display_name="Dana", tenant_id="tenant-1"),
            direction="inbound",
            recording_status=recording,
        )
        self.audio: list[bytes] = []
        self.cancels = 0
        self.ended: str | None = None
        self.emotions: list[str] = []
        self.images: list[dict[str, Any]] = []
        self._frames = frames or {}
        # Mirrors the real session: the server keeps this flag current.
        self.recording_active = recording == "active"

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def cancel_playback(self) -> None:
        self.cancels += 1

    async def end(self, reason: str) -> None:
        self.ended = self.ended or reason

    async def express(self, emotion: str) -> None:
        # Built for real, so the fake enforces exactly what the wire enforces.
        # A fake that is more permissive than the session hides the bug where a
        # plugin forgets a bound.
        from standin.avatar import expression

        expression(emotion)
        self.emotions.append(emotion)

    async def display_image(
        self, image, mime="image/jpeg", duration_ms=None, mode=None, caption=None
    ):
        self.images.append(
            {
                "image": image,
                "mime": mime,
                "durationMs": duration_ms,
                "mode": mode,
                "caption": caption,
            }
        )

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        if source is not None:
            return self._frames.get(source)
        return self._frames.get("screenshare") or self._frames.get("camera")


async def _started(monkeypatch, call: FakeCall | None = None, **config: Any):
    """Start a handler with the agent socket faked out."""
    agent = FakeAgent()

    async def fake_connect(config_arg, on_message, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = ElevenLabsHandler(_config(**config))
    call = call or FakeCall()
    await handler.on_start(call)
    return handler, call, agent


def _frame(source: str = "screenshare", **overrides: Any) -> VideoFrame:
    fields = {
        "source": source,
        "ts": 1,
        "width": 1280,
        "height": 720,
        "mime": "image/jpeg",
        "data_base64": JPEG,
        "participant_id": "aad-1",
        "participant_name": "Dana",
    }
    fields.update(overrides)
    return VideoFrame(**fields)


# ------------------------------------------------------------------ the relay


async def test_the_conversation_opens_personalised_with_the_caller(monkeypatch):
    _, _, agent = await _started(monkeypatch, first_message="Hello from Microsoft Teams")
    init = agent.sent[0]
    assert init["type"] == "conversation_initiation_client_data"
    assert init["dynamic_variables"] == {
        "caller_name": "Dana",
        "tenant_id": "tenant-1",
        "call_direction": "inbound",
    }
    # Per-person memory keyed on a REAL identity.
    assert init["user_id"] == "aad-1"
    assert (
        init["conversation_config_override"]["agent"]["first_message"]
        == "Hello from Microsoft Teams"
    )


async def test_an_anonymous_caller_gets_no_shared_identity(monkeypatch):
    """Two anonymous callers sharing a user_id would share conversation memory,
    so an absent identity must mean NO identity rather than a default."""
    call = FakeCall()
    call.start = SessionStart(call_id="call-1", thread_id="", caller=Caller(), direction="inbound")
    _, _, agent = await _started(monkeypatch, call)
    assert "user_id" not in agent.sent[0]
    assert agent.sent[0]["dynamic_variables"]["caller_name"] == "caller"


async def test_audio_flows_both_ways(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler.on_caller_audio(PCM)
    assert agent.audio == [base64.b64encode(PCM).decode()]

    reply = b"\x03\x04" * 80
    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 1, "audio_base_64": base64.b64encode(reply).decode()},
        }
    )
    assert call.audio == [reply]


async def test_audio_before_the_socket_opens_is_buffered_then_flushed(monkeypatch):
    """The caller can start talking before the conversation is up. Dropping that
    audio loses the first thing they said."""
    agent = FakeAgent()

    async def fake_connect(config_arg, on_message, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = ElevenLabsHandler(_config())
    await handler.on_caller_audio(PCM)
    await handler.on_context("There are 3 human participants on this call.")
    assert agent.audio == []

    await handler.on_start(FakeCall())
    assert agent.audio == [base64.b64encode(PCM).decode()]
    assert any(m.get("text", "").startswith("There are 3") for m in agent.sent)


async def test_a_barge_in_stops_the_bot_talking_and_drops_the_tail(monkeypatch):
    """The interruption must both flush what StandIn has buffered and suppress
    the audio the model had already produced, or the bot talks over the caller."""
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 5, "audio_base_64": base64.b64encode(b"12").decode()},
        }
    )
    assert call.audio == [b"12"]

    await handler._on_agent_message({"type": "interruption", "interruption_event": {"event_id": 7}})
    assert call.cancels == 1

    # Audio generated before the interruption but delivered after it.
    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 6, "audio_base_64": base64.b64encode(b"34").decode()},
        }
    )
    assert call.audio == [b"12"], "ghost audio from the interrupted turn was played"

    # Audio from the NEW turn still plays.
    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 8, "audio_base_64": base64.b64encode(b"56").decode()},
        }
    )
    assert call.audio == [b"12", b"56"]


async def test_malformed_agent_frames_are_dropped_not_fatal(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    for message in (
        {"type": "audio"},
        {"type": "audio", "audio_event": {"event_id": "x", "audio_base_64": "AA=="}},
        {"type": "audio", "audio_event": {"event_id": 1, "audio_base_64": "not base64!"}},
        {"type": "interruption"},
        {"type": "ping"},
        {"type": "client_tool_call", "client_tool_call": {"tool_name": 5}},
        {"type": "something_new"},
    ):
        await handler._on_agent_message(message)
    assert call.audio == []
    assert call.ended is None


async def test_a_ping_is_answered(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler._on_agent_message({"type": "ping", "ping_event": {"event_id": 9}})
    assert {"type": "pong", "event_id": 9} in agent.sent


async def test_the_goodbye_interrupts_and_suppresses_the_interrupted_turn(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 3, "audio_base_64": base64.b64encode(b"ab").decode()},
        }
    )
    await handler.on_goodbye("Thanks for calling.")
    assert any("Thanks for calling." in m.get("text", "") for m in agent.sent)

    await handler._on_agent_message(
        {
            "type": "audio",
            "audio_event": {"event_id": 2, "audio_base_64": base64.b64encode(b"cd").decode()},
        }
    )
    assert call.audio == [b"ab"]


async def test_a_closed_conversation_ends_the_call(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_close(1000, "normal")
    assert call.ended == "agent-disconnected"


async def test_teardown_closes_the_conversation(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler.aclose("caller-hung-up")
    assert agent.closed


async def test_a_conversation_that_will_not_open_ends_the_call_rather_than_hanging(monkeypatch):
    async def refuse(config_arg, on_message, on_close):
        raise RuntimeError("ElevenLabs is down")

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(refuse))
    handler = ElevenLabsHandler(_config())
    call = FakeCall()
    await handler.on_start(call)
    assert call.ended == "agent-unavailable"


# ------------------------------------------------------------- client tools


async def test_end_call_hangs_up(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_tool_call({"tool_name": "end_call", "tool_call_id": "t1"})
    assert call.ended == "agent-ended-call"
    assert agent.results()[0]["isError"] is False


async def test_express_reaches_the_avatar(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_tool_call(
        {"tool_name": "express", "tool_call_id": "t1", "parameters": {"emotion": "happy"}}
    )
    assert call.emotions == ["happy"]


@pytest.mark.parametrize("emotion", ["", "   ", "x" * 41])
async def test_express_refuses_an_unusable_emotion(monkeypatch, emotion):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_tool_call(
        {"tool_name": "express", "tool_call_id": "t1", "parameters": {"emotion": emotion}}
    )
    assert call.emotions == []
    assert agent.results()[0]["isError"] is True


async def test_show_image_puts_inline_bytes_on_the_tile(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_show_image(
        "t1",
        {"dataBase64": JPEG, "mime": "image/jpeg", "caption": "Q3", "durationMs": 3000},
    )
    assert call.images[0]["caption"] == "Q3"
    assert call.images[0]["durationMs"] == 3000
    assert agent.results()[0]["isError"] is False


async def test_show_image_refuses_a_type_the_service_will_not_draw(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler._on_show_image("t1", {"dataBase64": JPEG, "mime": "image/gif"})
    assert call.images == []
    assert agent.results()[0]["isError"] is True


async def test_show_image_tells_the_agent_the_truth_when_a_url_is_refused(monkeypatch):
    """The URL comes from the model, which is steered by the caller. A private
    address must be refused, and the agent must not be told it worked."""
    handler, call, agent = await _started(monkeypatch)
    await handler._on_show_image("t1", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert call.images == []
    result = agent.results()[0]
    assert result["isError"] is True
    assert "show_image failed" in result["result"]


async def test_look_uses_the_latest_frame_when_the_call_is_recorded(monkeypatch):
    call = FakeCall(recording="active", frames={"screenshare": _frame()})
    handler, call, agent = await _started(monkeypatch, call)
    await handler._on_look("t1", {"question": "What is on the slide?"})
    data, mime, question = agent.attached[0]
    assert data == base64.b64decode(JPEG)
    assert mime == "image/jpeg"
    assert "What is on the slide?" in question
    assert "screen shared by Dana" in question
    assert agent.results()[0]["isError"] is False


async def test_look_refuses_when_the_call_is_not_recorded(monkeypatch):
    """Looking uploads the caller's screen to a third party. Without a recording
    the caller has not been told anything is being kept."""
    call = FakeCall(recording=None, frames={"screenshare": _frame()})
    handler, call, agent = await _started(monkeypatch, call)
    await handler._on_look("t1", {})
    assert agent.attached == []
    assert agent.results()[0]["isError"] is True


async def test_look_says_so_when_there_is_nothing_to_look_at(monkeypatch):
    call = FakeCall(recording="active")
    handler, call, agent = await _started(monkeypatch, call)
    await handler._on_look("t1", {})
    assert agent.results()[0]["isError"] is True
    assert "not sharing" in agent.results()[0]["result"]


async def test_look_prefers_the_requested_source(monkeypatch):
    call = FakeCall(
        recording="active",
        frames={"screenshare": _frame(), "camera": _frame("camera", participant_name="Ali")},
    )
    handler, call, agent = await _started(monkeypatch, call)
    await handler._on_look("t1", {"source": "camera"})
    assert "camera of Ali" in agent.attached[0][2]


async def test_looking_back_answers_about_a_frame_already_gone(monkeypatch):
    """The call session keeps only the newest frame per source. Without the
    keyframe store, a question about a slide already moved past has nothing to
    look at."""
    call = FakeCall(recording="active")
    handler, call, agent = await _started(monkeypatch, call)
    await handler.on_video_frame(_frame())
    await handler._on_look_back("t1", {"question": "what did that say?"})
    assert "what did that say?" in agent.attached[0][2]
    assert agent.results()[0]["isError"] is False


async def test_looking_back_needs_a_recorded_call(monkeypatch):
    call = FakeCall(recording=None)
    handler, call, agent = await _started(monkeypatch, call)
    await handler.on_video_frame(_frame())
    await handler._on_look_back("t1", {})
    assert agent.attached == []
    assert "while the call is being recorded" in agent.results()[0]["result"]


async def test_the_client_tools_are_the_sdk_capabilities(monkeypatch):
    """A tool declared on the agent but unanswered here is an agent that stalls
    mid-call, so the declarations and the dispatch have to agree."""
    handler, _, agent = await _started(monkeypatch)
    for tool in client_tools():
        await handler._on_tool_call(
            {"tool_name": tool["name"], "tool_call_id": "t", "parameters": {}}
        )
    answered = [r["result"] for r in agent.results()]
    assert not any("is not a tool this plugin answers" in text for text in answered)


def test_show_image_is_widened_for_the_one_provider_that_can_take_bytes():
    show = next(tool for tool in client_tools() if tool["name"] == "show_image")
    assert set(show["parameters"]["properties"]) >= {"url", "dataBase64", "mime"}
    # Either form will do, so neither can be required.
    assert show["parameters"]["required"] == []


async def test_an_unknown_tool_is_refused_by_name(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler._on_tool_call({"tool_name": "launch_rocket", "tool_call_id": "t1"})
    result = agent.results()[0]
    assert result["isError"] is True
    assert "launch_rocket" in result["result"]


# ------------------------------------------------------------------- config


def test_the_config_names_the_variable_that_is_missing(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent-1")
    with pytest.raises(Exception, match="ELEVENLABS_API_KEY"):
        ElevenLabsConfig.from_env()


def test_the_config_refuses_a_host_that_is_not_elevenlabs(monkeypatch):
    """The API key travels to this host, so a wrong one is credential leakage
    rather than a failed call."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "key")
    monkeypatch.setenv("ELEVENLABS_AGENT_ID", "agent")
    monkeypatch.setenv("ELEVENLABS_HOST", "evil.example.com")
    with pytest.raises(Exception, match="elevenlabs.io"):
        ElevenLabsConfig.from_env()
