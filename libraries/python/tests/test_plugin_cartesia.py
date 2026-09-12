# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The Cartesia relay, driven against a fake Line stream."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from standin.plugins.cartesia import CartesiaConfig, CartesiaHandler
from standin.plugins.cartesia import handler as handler_module
from standin.protocol import Caller, SessionStart

pytestmark = pytest.mark.unit

PCM = b"\x01\x02" * 160


def _config(**overrides: Any) -> CartesiaConfig:
    base: dict[str, Any] = {"api_key": "key-never-real", "agent_id": "agent-1"}
    base.update(overrides)
    return CartesiaConfig(**base)


class FakeAgent:
    def __init__(self) -> None:
        self.stream_id = "stream-1"
        self.start: dict[str, Any] | None = None
        self.audio: list[str] = []
        self.custom: list[dict[str, Any]] = []
        self.dtmf: list[str] = []
        self.closed = False
        self.is_open = True

    def send_start(self, start: dict[str, Any]) -> None:
        self.start = start

    def send_audio_chunk(self, chunk: str) -> None:
        self.audio.append(chunk)

    def send_custom(self, metadata: dict[str, Any]) -> None:
        self.custom.append(metadata)

    def send_dtmf(self, digit: str) -> None:
        self.dtmf.append(digit)

    async def aclose(self) -> None:
        self.closed = True
        self.is_open = False


class FakeCall:
    def __init__(self) -> None:
        self.call_id = "call-1"
        self.start = SessionStart(
            call_id="call-1",
            thread_id="19:meeting@thread.v2",
            caller=Caller(aad_id="aad-1", display_name="Dana", tenant_id="tenant-1"),
            direction="inbound",
        )
        self.audio: list[bytes] = []
        self.cancels = 0
        self.ended: str | None = None

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def cancel_playback(self) -> None:
        self.cancels += 1

    async def end(self, reason: str) -> None:
        self.ended = self.ended or reason


async def _started(monkeypatch, **config: Any):
    agent = FakeAgent()

    async def fake_connect(cfg, on_message, on_audio, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = CartesiaHandler(_config(**config))
    call = FakeCall()
    await handler.on_start(call)
    return handler, call, agent


async def test_the_stream_starts_pinned_to_the_wire_format_with_caller_metadata(monkeypatch):
    _, _, agent = await _started(monkeypatch)
    assert agent.start["config"]["input_format"] == "pcm_16000"
    assert agent.start["metadata"] == {
        "from": "msteams",
        "callId": "call-1",
        "callerName": "Dana",
        "tenantId": "tenant-1",
        "direction": "inbound",
    }


async def test_a_prompt_you_did_not_write_is_never_replaced(monkeypatch):
    """Left unset, the agent keeps the prompt written on Cartesia's platform.
    Silently overwriting it would break a deployed agent."""
    _, _, agent = await _started(monkeypatch)
    assert "agent" not in agent.start or "system_prompt" not in agent.start.get("agent", {})

    _, _, with_prompt = await _started(monkeypatch, system_prompt="Be brief.")
    assert "Be brief." in with_prompt.start["agent"]["system_prompt"]
    assert "Dana" in with_prompt.start["agent"]["system_prompt"]


async def test_audio_flows_both_ways(monkeypatch):
    handler, call, agent = await _started(monkeypatch)
    await handler.on_caller_audio(PCM)
    assert agent.audio == [base64.b64encode(PCM).decode()]

    reply = b"\x07\x08" * 80
    await handler._on_agent_audio(base64.b64encode(reply).decode())
    assert call.audio == [reply]


async def test_unusable_agent_audio_is_dropped_not_fatal(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_audio("not base64!")
    assert call.audio == []
    assert call.ended is None


async def test_a_clear_event_stops_the_bot_talking(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_message({"event": "clear"})
    assert call.cancels == 1


async def test_context_before_the_stream_opens_is_no_longer_lost(monkeypatch):
    """This plugin used to drop anything that arrived before the stream was up."""
    agent = FakeAgent()

    async def fake_connect(cfg, on_message, on_audio, on_close):
        return agent

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(fake_connect))
    handler = CartesiaHandler(_config())
    await handler.on_context("There are 3 human participants on this call.")
    assert agent.custom == []

    await handler.on_start(FakeCall())
    assert any("There are 3" in str(m.get("context", "")) for m in agent.custom)


async def test_context_reaches_the_agent_code(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler.on_context("There are 3 human participants on this call.")
    assert agent.custom[0]["context"].startswith("There are 3")


async def test_a_key_press_arrives_as_a_real_dtmf_event(monkeypatch):
    """The SDK renders key presses as prose for a model. Line has a real dtmf
    event, so the digit is read back out of the SDK's own sentence."""
    handler, _, agent = await _started(monkeypatch)
    await handler.on_context('The caller pressed the "5" key on their keypad.')
    assert agent.dtmf == ["5"]
    assert agent.custom == []


async def test_the_goodbye_is_handed_to_the_agent_code(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler.on_goodbye("Thanks for calling.")
    assert agent.custom[-1]["goodbye"] == "Thanks for calling."


async def test_a_closed_stream_ends_the_call(monkeypatch):
    handler, call, _ = await _started(monkeypatch)
    await handler._on_agent_close(1000, "normal")
    assert call.ended == "agent-disconnected"


async def test_a_stream_that_will_not_open_ends_the_call(monkeypatch):
    async def refuse(cfg, on_message, on_audio, on_close):
        raise RuntimeError("Cartesia is down")

    monkeypatch.setattr(handler_module.AgentSocket, "connect", staticmethod(refuse))
    handler = CartesiaHandler(_config())
    call = FakeCall()
    await handler.on_start(call)
    assert call.ended == "agent-unavailable"


async def test_teardown_closes_the_stream(monkeypatch):
    handler, _, agent = await _started(monkeypatch)
    await handler.aclose("caller-hung-up")
    assert agent.closed


def test_the_config_names_the_variable_that_is_missing(monkeypatch):
    monkeypatch.delenv("CARTESIA_API_KEY", raising=False)
    with pytest.raises(Exception, match="CARTESIA_API_KEY"):
        CartesiaConfig.from_env()


def test_the_config_refuses_a_host_that_is_not_cartesia(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "key")
    monkeypatch.setenv("CARTESIA_AGENT_ID", "agent")
    monkeypatch.setenv("CARTESIA_API_HOST", "evil.example.com")
    with pytest.raises(Exception, match="cartesia.ai"):
        CartesiaConfig.from_env()
