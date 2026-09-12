# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The provider-neutral tool surface.

Two things are being protected here. One, the same five capabilities reach every
provider with the same wording, so a caller gets the same agent whichever one
answers. Two, dispatch NEVER raises: its result is read out loud, and an
exception there is a silent tool and a caller left waiting.

The TypeScript twin is ``src/callTools.test.ts``.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from standin.avatar import expression
from standin.calltools import BUILT_IN_TOOLS, CallTools, ToolSpec, tool_schemas
from standin.protocol import Caller, SessionStart
from standin.vision import VideoFrame
from standin.vision_tools import VisionTools

pytestmark = pytest.mark.unit

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0frame").decode()


def _frame(source: str = "screenshare") -> VideoFrame:
    return VideoFrame(
        source=source,
        ts=1,
        width=1280,
        height=720,
        mime="image/jpeg",
        data_base64=JPEG,
        participant_name="Dana",
    )


class FakeCall:
    def __init__(self, recording: bool = False, frames: dict[str, VideoFrame] | None = None):
        self.call_id = "call-1"
        self.start = SessionStart(
            call_id="call-1", thread_id="", caller=Caller(display_name="Dana"), direction="inbound"
        )
        self.recording_active = recording
        self.ended: list[str] = []
        self.emotions: list[str] = []
        self._frames = frames or {}

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        if source is not None:
            return self._frames.get(source)
        return self._frames.get("screenshare") or self._frames.get("camera")

    async def end(self, reason: str) -> None:
        self.ended.append(reason)

    async def express(self, emotion: str) -> None:
        # Build the real wire message, so this fake refuses exactly what the wire
        # refuses rather than something more forgiving.
        expression(emotion)
        self.emotions.append(emotion)

    async def display_image(self, *args: Any, **kwargs: Any) -> None:
        pass


class FakeDescriber:
    def __init__(self, answer: str = "a slide about revenue") -> None:
        self.answer = answer

    async def describe(self, frame: VideoFrame, question: str) -> str:
        return self.answer


def _tools(call: FakeCall, describer: FakeDescriber | None = None) -> CallTools:
    return CallTools(call, vision=VisionTools(call, describer=describer))


# ------------------------------------------------------------- declarations


def test_the_same_five_things_reach_every_provider():
    flat = [spec["name"] for spec in tool_schemas("flat")]
    assert flat == ["end_call", "express", "show_image", "look", "look_back"]
    assert [spec["name"] for spec in tool_schemas("openai")] == flat
    assert [spec["name"] for spec in tool_schemas("anthropic")] == flat


def test_each_provider_gets_its_own_shape():
    assert tool_schemas("flat")[0]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
    }
    assert tool_schemas("openai")[0]["type"] == "function"
    assert "input_schema" in tool_schemas("anthropic")[0]
    assert "parameters" not in tool_schemas("anthropic")[0]


def test_an_unknown_dialect_falls_back_rather_than_failing_at_connect_time():
    """A tool a model never sees is a worse outcome than a shape one provider
    happens to also accept."""
    assert tool_schemas("something-new") == tool_schemas("flat")


@pytest.mark.parametrize("dialect", ["flat", "openai", "anthropic"])
def test_a_required_argument_stays_required_in_every_dialect(dialect: str):
    spec = next(s for s in tool_schemas(dialect) if s["name"] == "express")
    schema = spec.get("parameters") or spec["input_schema"]
    assert schema["required"] == ["emotion"]


def test_every_tool_is_described_for_a_model_not_a_maintainer():
    # Every description has to say WHEN to reach for it; that sentence is the
    # only thing the model reads before deciding.
    for spec in BUILT_IN_TOOLS:
        assert "use" in spec.description.lower()
        assert len(spec.description) > 40


def test_the_two_sdks_declare_the_same_tools():
    """Parity is the point: a caller must get the same agent from either SDK."""
    from pathlib import Path

    ts = Path(__file__).resolve().parents[2] / "typescript" / "src" / "callTools.ts"
    text = ts.read_text()
    for spec in BUILT_IN_TOOLS:
        assert f'name: "{spec.name}"' in text
        # The parameters too, not just the names. A parameter added on one side
        # and missed on the other is two SDKs handing the model different tools,
        # which is exactly the drift nobody notices until a call goes wrong.
        for param in spec.parameters:
            assert f"{param}: {{" in text, f"{spec.name}.{param} is missing from the TypeScript"


# -------------------------------------------------------------- your tools


async def test_your_own_tool_joins_the_same_list():
    call = FakeCall()
    tools = _tools(call)
    tools.register(
        ToolSpec("open_ticket", "Use this when the caller reports a fault."),
        lambda params: "ticket 42 is open",
    )
    assert "open_ticket" in [spec["name"] for spec in tools.schemas("flat")]
    assert await tools.dispatch("open_ticket", {}) == "ticket 42 is open"


async def test_an_async_tool_of_your_own_is_awaited():
    async def handler(params: dict[str, Any]) -> str:
        return "looked it up"

    tools = _tools(FakeCall())
    tools.register(ToolSpec("lookup", "Use this to look something up."), handler)
    assert await tools.dispatch("lookup", {}) == "looked it up"


def test_a_built_in_cannot_be_taken_over():
    """A shadowed end_call is an agent that has quietly lost the ability to hang
    up, which is not something to discover mid-conversation."""
    with pytest.raises(ValueError, match="built-in"):
        _tools(FakeCall()).register(ToolSpec("end_call", "x"), lambda params: "")


async def test_a_failing_tool_of_your_own_does_not_take_the_call_down():
    def handler(params: dict[str, Any]) -> str:
        raise RuntimeError("the ticket system is down")

    tools = _tools(FakeCall())
    tools.register(ToolSpec("flaky", "x"), handler)
    assert await tools.dispatch("flaky", {}) == "flaky failed: the ticket system is down"


# ---------------------------------------------------------------- dispatch


async def test_the_agent_can_hang_up():
    call = FakeCall()
    assert await _tools(call).dispatch("end_call") == "the call is ending"
    assert call.ended == ["agent-ended-call"]


async def test_an_over_long_emotion_is_read_back_not_raised():
    """The bound lives where the wire message is built, so a plugin cannot forget
    it. What reaches the model is the reason, in words."""
    call = FakeCall()
    result = await _tools(call).dispatch("express", {"emotion": "x" * 41})
    assert "at most 40 characters" in result
    assert call.emotions == []


async def test_a_missing_argument_is_named():
    assert "needs an 'emotion'" in await _tools(FakeCall()).dispatch("express", {})
    assert "needs an 'emotion'" in await _tools(FakeCall()).dispatch("express", {"emotion": "  "})


async def test_an_emotion_reaches_the_avatar():
    call = FakeCall()
    assert await _tools(call).dispatch("express", {"emotion": "happy"}) == "expressing happy"
    assert call.emotions == ["happy"]


async def test_looking_answers_about_the_screen():
    call = FakeCall(frames={"screenshare": _frame()})
    result = await _tools(call, FakeDescriber()).dispatch("look", {"question": "What is on it?"})
    assert result == "a slide about revenue"


async def test_looking_at_nothing_says_so():
    assert "not sharing" in await _tools(FakeCall(), FakeDescriber()).dispatch("look", {})


async def test_a_private_address_is_refused():
    result = await _tools(FakeCall()).dispatch(
        "show_image", {"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert "could not fetch" in result


async def test_arguments_of_the_wrong_type_are_survivable():
    """Tool arguments arrive as whatever the model emitted, which is not always
    the type the schema asked for."""
    tools = _tools(FakeCall(), FakeDescriber())
    assert "needs an 'emotion'" in await tools.dispatch("express", {"emotion": 7})
    assert "needs a public" in await tools.dispatch("show_image", {"url": None})
    assert "not sharing" in await tools.dispatch("look", {"source": 12})


async def test_an_unknown_tool_is_named_rather_than_ignored():
    result = await _tools(FakeCall()).dispatch("teleport", {})
    assert result == '"teleport" is not a tool this agent has'


async def test_no_arguments_at_all_is_fine():
    call = FakeCall()
    assert await _tools(call).dispatch("end_call", None) == "the call is ending"


# -------------------------------------------------------------- the ok bit


async def test_ok_is_false_only_when_the_sdk_knows_the_tool_did_not_run():
    """Providers whose tool-result frame carries an error flag read this. It has
    to mean something precise or it is worse than not having it."""
    tools = _tools(FakeCall(), FakeDescriber())
    assert (await tools.run("end_call")).ok is True
    assert (await tools.run("express", {"emotion": "happy"})).ok is True
    assert (await tools.run("express", {})).ok is False
    assert (await tools.run("express", {"emotion": "x" * 41})).ok is False
    assert (await tools.run("teleport", {})).ok is False


async def test_a_vision_refusal_is_an_answer_not_an_error():
    """The vision tools answer in sentences by design, and the reason is in the
    text where the model will read it."""
    result = await _tools(FakeCall(), FakeDescriber()).run("look", {})
    assert result.ok is True
    assert "not sharing" in result.text


async def test_dispatch_is_the_text_of_run():
    tools = _tools(FakeCall(), FakeDescriber())
    assert await tools.dispatch("look", {}) == (await tools.run("look", {})).text
