# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The Deepgram relay, driven against a fake Voice Agent socket.

No network and no API key. These assert the RELAY: what reaches the caller,
what reaches the agent, how context gets into a prompt that has no context
channel, and what each of the four built-in functions actually does.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from standin.calltools import BUILT_IN_TOOLS as BUILT_IN_CALL_TOOLS
from standin.plugins.deepgram import CustomTool, DeepgramConfig, DeepgramHandler
from standin.plugins.deepgram import handler as handler_module
from standin.protocol import Caller, SessionStart
from standin.vision import VideoFrame

pytestmark = pytest.mark.unit

PCM = b"\x01\x02" * 160
JPEG_BASE64 = "/9j/4AAQ"


def _config(**overrides: Any) -> DeepgramConfig:
    base: dict[str, Any] = {"api_key": "key-never-real"}
    base.update(overrides)
    return DeepgramConfig(**base)


class FakeAgent:
    def __init__(self) -> None:
        self.settings: dict[str, Any] | None = None
        self.prompts: list[str] = []
        self.injected: list[str] = []
        self.audio: list[bytes] = []
        self.results: list[dict[str, str]] = []
        self.closed = False
        self.is_open = True

    def send_settings(self, settings: dict[str, Any]) -> None:
        self.settings = settings

    def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    def update_prompt(self, prompt: str) -> None:
        self.prompts.append(prompt)

    def inject_agent_message(self, text: str) -> None:
        self.injected.append(text)

    def send_function_result(self, call_id: str, name: str, content: str) -> None:
        self.results.append({"id": call_id, "name": name, "content": content})

    async def aclose(self) -> None:
        self.closed = True
        self.is_open = False


class FakeCall:
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
        from standin.avatar import expression

        expression(emotion)
        self.emotions.append(emotion)

    async def display_image(
        self, image, mime="image/jpeg", duration_ms=None, mode=None, caption=None
    ):
        self.images.append({"mime": mime, "caption": caption})

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        if source is not None:
            return self._frames.get(source)
        return self._frames.get("screenshare") or self._frames.get("camera")


async def _started(monkeypatch, call: FakeCall | None = None, **kwargs: Any):
    agent = FakeAgent()

    async def fake_connect(config, on_message, on_audio, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = DeepgramHandler(_config(), **kwargs)
    call = call or FakeCall()
    await handler.on_start(call)
    return handler, call, agent


def _frame(source: str = "screenshare") -> VideoFrame:
    return VideoFrame(
        source=source,
        ts=1,
        width=1280,
        height=720,
        mime="image/jpeg",
        data_base64=JPEG_BASE64,
        participant_name="Dana",
    )


# ------------------------------------------------------------------ the relay


async def test_settings_pin_the_wire_format_and_declare_the_call_capabilities(monkeypatch):
    """linear16 at 16 kHz both ways is what makes the hot path a copy. If this
    ever drifts, every call is resampled or garbled."""
    _, _, agent = await _started(monkeypatch)
    audio = agent.settings["audio"]
    assert audio["input"] == {"encoding": "linear16", "sample_rate": 16_000}
    assert audio["output"]["encoding"] == "linear16"
    assert audio["output"]["sample_rate"] == 16_000

    # Declared from the SDK's list rather than a copy, so a capability added
    # there reaches Deepgram without an edit in this plugin.
    names = {f["name"] for f in agent.settings["agent"]["think"]["functions"]}
    assert names == {spec.name for spec in BUILT_IN_CALL_TOOLS}
    assert "look_back" in names
    assert "Dana" in agent.settings["agent"]["think"]["prompt"]


async def test_audio_flows_both_ways_without_conversion(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler.on_caller_audio(PCM)
    assert agent.audio == [PCM]

    reply = b"\x05\x06" * 80
    await handler._on_agent_audio(reply)
    assert call.audio == [reply]


async def test_context_rides_the_prompt_and_stays_bounded(monkeypatch):
    """This API has no context channel, so context is folded into the prompt -
    which is resent in full every time, so it must not grow forever."""
    handler, _, agent = await _started(monkeypatch)
    for i in range(12):
        await handler.on_context(f"note {i}")
    latest = agent.prompts[-1]
    assert "note 11" in latest
    assert "note 0" not in latest
    assert latest.count("- note") == 8


async def test_the_caller_speaking_stops_the_bot_talking(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_message({"type": "UserStartedSpeaking"})
    assert call.cancels == 1


async def test_the_goodbye_is_spoken_immediately(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler.on_goodbye("Thanks for calling.")
    assert agent.injected == ["Thanks for calling."]


async def test_a_closed_agent_ends_the_call(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_close(1000, "normal")
    assert call.ended == "agent-disconnected"


async def test_an_agent_that_will_not_open_ends_the_call(monkeypatch):
    async def refuse(config, on_message, on_audio, on_close):
        raise RuntimeError("Deepgram is down")

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(refuse))
    handler = DeepgramHandler(_config())
    call = FakeCall()
    await handler.on_start(call)
    assert call.ended == "agent-unavailable"


async def test_context_before_the_socket_opens_is_no_longer_lost(monkeypatch):
    """The "there are N people here, stay quiet" line and the recording change
    both land in this gap. This plugin used to drop them."""
    agent = FakeAgent()

    async def fake_connect(config, on_message, on_audio, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = DeepgramHandler(_config())
    await handler.on_context("There are 3 human participants on this call.")
    assert agent.prompts == []

    await handler.on_start(FakeCall())
    assert any("There are 3" in p for p in agent.prompts)


async def test_audio_before_the_socket_opens_is_buffered(monkeypatch):
    agent = FakeAgent()

    async def fake_connect(config, on_message, on_audio, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = DeepgramHandler(_config())
    await handler.on_caller_audio(PCM)
    assert agent.audio == []
    await handler.on_start(FakeCall())
    assert agent.audio == [PCM]


# --------------------------------------------------------------- functions


async def test_end_call_hangs_up(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    assert await handler._dispatch("end_call", {}) == "the call is ending"
    assert call.ended == "agent-ended-call"


async def test_express_reaches_the_avatar(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._dispatch("express", {"emotion": "surprised"})
    assert call.emotions == ["surprised"]


async def test_show_image_refuses_a_private_address(monkeypatch):
    """The URL comes from the model, which the caller steers. A crafted prompt
    must not be able to make the worker fetch cloud metadata."""
    handler, call, agent = await _started(monkeypatch)
    await handler._run_function(
        {
            "name": "show_image",
            "id": "f1",
            "arguments": json.dumps({"url": "http://169.254.169.254/"}),
        }
    )
    assert call.images == []
    # Now a first-class sentence from the shared tools rather than an exception
    # string, which is what a model can actually act on.
    assert "could not fetch" in agent.results[0]["content"]


async def test_look_needs_a_vision_model_and_says_so(monkeypatch):
    """A Voice Agent hears but does not see. Saying that plainly is better than
    a tool that silently returns nothing."""
    call = FakeCall(frames={"screenshare": _frame()})
    handler, call, _ = await _started(monkeypatch, call, describer=None)
    result = await handler._dispatch("look", {})
    assert "no vision model is configured" in result


async def test_look_describes_the_latest_frame(monkeypatch):
    class FakeDescriber:
        def __init__(self) -> None:
            self.asked: list[tuple[str, str]] = []

        async def describe(self, frame, question):
            self.asked.append((frame.source, question))
            return "a slide about revenue"

    describer = FakeDescriber()
    call = FakeCall(frames={"screenshare": _frame(), "camera": _frame("camera")})
    handler, call, _ = await _started(monkeypatch, call, describer=describer)

    assert await handler._dispatch("look", {"question": "what is this?"}) == "a slide about revenue"
    assert describer.asked == [("screenshare", "what is this?")]

    await handler._dispatch("look", {"source": "camera"})
    assert describer.asked[-1] == ("camera", "Describe what is visible.")


async def test_look_says_so_when_there_is_nothing_to_see(monkeypatch):
    class Describer:
        async def describe(self, frame, question):
            raise AssertionError("must not be asked when there is no frame")

    handler, _, _ = await _started(monkeypatch, describer=Describer())
    assert "not sharing" in await handler._dispatch("look", {})


async def test_a_custom_tool_runs_in_your_worker(monkeypatch):
    seen: list[Any] = []

    async def open_ticket(params, ctx):
        seen.append((params, ctx.recording))
        return f"ticket for {params['summary']}"

    tool = CustomTool(name="open_ticket", description="Open a ticket.", handler=open_ticket)
    call = FakeCall(recording="active")
    handler, call, agent = await _started(monkeypatch, call, tools=[tool])

    assert "open_ticket" in {f["name"] for f in agent.settings["agent"]["think"]["functions"]}
    await handler._run_function(
        {"name": "open_ticket", "id": "f1", "arguments": json.dumps({"summary": "printer"})}
    )
    assert agent.results[0]["content"] == "ticket for printer"
    assert seen == [({"summary": "printer"}, True)]


async def test_a_failing_custom_tool_tells_the_agent_the_truth(monkeypatch):
    def explode(params, ctx):
        raise RuntimeError("the ticket system is down")

    tool = CustomTool(name="open_ticket", description="Open a ticket.", handler=explode)
    handler, _, agent = await _started(monkeypatch, tools=[tool])
    await handler._run_function({"name": "open_ticket", "id": "f1", "arguments": "{}"})
    assert "the ticket system is down" in agent.results[0]["content"]


def test_a_custom_tool_may_not_shadow_a_call_capability():
    """Shadowing end_call would silently remove the agent's ability to hang up."""
    tool = CustomTool(name="end_call", description="nope", handler=lambda p, c: "")
    with pytest.raises(ValueError, match="end_call"):
        DeepgramHandler(_config(), tools=[tool])


async def test_an_unknown_function_is_refused_by_name(monkeypatch):
    handler, _, _ = await _started(monkeypatch)
    assert "launch_rocket" in await handler._dispatch("launch_rocket", {})


async def test_malformed_function_arguments_do_not_break_the_call(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler._run_function({"name": "express", "id": "f1", "arguments": "not json"})
    assert agent.results[0]["content"].startswith("express needs")


# ------------------------------------------------------------------- config


def test_the_config_names_the_variable_that_is_missing(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(Exception, match="DEEPGRAM_API_KEY"):
        DeepgramConfig.from_env()


def test_the_config_refuses_a_host_that_is_not_deepgram(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "key")
    monkeypatch.setenv("DEEPGRAM_AGENT_HOST", "evil.example.com")
    with pytest.raises(Exception, match="deepgram.com"):
        DeepgramConfig.from_env()


def test_think_endpoint_headers_must_be_a_json_object(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "key")
    monkeypatch.delenv("DEEPGRAM_AGENT_HOST", raising=False)
    monkeypatch.setenv("DEEPGRAM_THINK_ENDPOINT_HEADERS", "[1,2]")
    with pytest.raises(Exception, match="JSON object"):
        DeepgramConfig.from_env()
