# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Showing the model what the caller is showing, without being asked.

Three things keep this from being expensive or creepy: the recording gate, so
nobody's screen is streamed to a model without them being told; change
detection, so a screen nobody touched costs nothing; and a reserve, so ambient
spending cannot starve the caller's own request to look.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from standin.ambient import AmbientImage, AmbientVision
from standin.vision import VideoFrame
from standin.vision_tools import VisionBudget

pytestmark = pytest.mark.unit


def _frame(body: str = "one", source: str = "screenshare", name: str = "Dana") -> VideoFrame:
    return VideoFrame(
        source=source,
        ts=1,
        width=1280,
        height=720,
        mime="image/jpeg",
        data_base64=base64.b64encode(body.encode()).decode(),
        participant_name=name,
    )


class FakeCall:
    def __init__(self, recording: bool = True) -> None:
        self.recording_active = recording


class Sink:
    def __init__(self, fail_times: int = 0) -> None:
        self.images: list[AmbientImage] = []
        self.fail_times = fail_times

    async def __call__(self, image: AmbientImage) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("the provider refused it")
        self.images.append(image)


async def _settle() -> None:
    """Let the spawned pass run."""
    for _ in range(6):
        await asyncio.sleep(0)


# ------------------------------------------------------------------- the gate


async def test_it_is_off_unless_a_plugin_turns_it_on():
    """It spends money on every scene change, and not every deployment wants
    that."""
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink)
    ambient.offer(_frame())
    await _settle()
    assert sink.images == []
    assert ambient.queued == 0


async def test_nothing_is_stored_while_the_call_is_not_recorded():
    """Streaming somebody's screen to a model is a different promise from
    glancing at it once, and the recording is what told them."""
    sink = Sink()
    ambient = AmbientVision(FakeCall(recording=False), sink, enabled=True)
    ambient.offer(_frame())
    await _settle()
    assert sink.images == []


async def test_opening_the_gate_does_not_surface_an_earlier_frame():
    """Otherwise turning the recording on reaches back to before the caller was
    told anything was being kept."""
    call = FakeCall(recording=False)
    sink = Sink()
    ambient = AmbientVision(call, sink, enabled=True)
    ambient.offer(_frame("before the recording"))
    await _settle()

    call.recording_active = True
    ambient.flush()
    await _settle()
    assert sink.images == []


async def test_the_gate_can_be_turned_off_for_a_deployment_that_does_not_need_it():
    sink = Sink()
    ambient = AmbientVision(FakeCall(recording=False), sink, enabled=True, require_recording=False)
    ambient.offer(_frame())
    await _settle()
    assert len(sink.images) == 1


# --------------------------------------------------------------- the changes


async def test_a_changed_screen_reaches_the_model():
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True)
    ambient.offer(_frame("the first slide"))
    await _settle()
    assert len(sink.images) == 1
    assert sink.images[0].owner == "Dana"
    assert "Dana" in sink.images[0].caption
    assert sink.images[0].data_url.startswith("data:image/jpeg;base64,")


async def test_a_screen_nobody_touched_costs_nothing():
    sink = Sink()
    budget = VisionBudget(max_per_minute=10)
    ambient = AmbientVision(FakeCall(), sink, enabled=True, budget=budget)
    for _ in range(5):
        ambient.offer(_frame("the same slide"))
        await _settle()
    assert len(sink.images) == 1
    assert budget.spent == 1


async def test_a_delivery_that_failed_is_tried_again():
    """The latch is the last frame DELIVERED, not the last one seen. Latching a
    frame that never arrived means the model never sees that screen."""
    sink = Sink(fail_times=1)
    budget = VisionBudget(max_per_minute=10)
    ambient = AmbientVision(FakeCall(), sink, enabled=True, budget=budget)

    ambient.offer(_frame("the slide"))
    await _settle()
    assert sink.images == []
    # The charge was given back, because nothing was delivered.
    assert budget.spent == 0

    ambient.flush()
    await _settle()
    assert len(sink.images) == 1


async def test_each_source_is_latched_separately():
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True)
    ambient.offer(_frame("shared screen", source="screenshare"))
    ambient.offer(_frame("their face", source="camera"))
    await _settle()
    assert {image.source for image in sink.images} == {"screenshare", "camera"}


async def test_the_screen_share_goes_first():
    """Somebody presenting is nearly always talking about the screen rather
    than about their face."""
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True)
    ambient.offer(_frame("their face", source="camera"))
    ambient.offer(_frame("shared screen", source="screenshare"))
    await _settle()
    assert [image.source for image in sink.images] == ["screenshare", "camera"]


# --------------------------------------------------------------- the budget


def test_the_reserve_is_what_ambient_cannot_spend():
    budget = VisionBudget(max_per_minute=12)
    assert budget.reserve == 3
    assert VisionBudget(max_per_minute=4).reserve == 2  # never less than two
    assert VisionBudget(max_per_minute=0).reserve == 0  # uncapped means uncapped


def test_ambient_stops_at_the_reserve_and_an_explicit_look_does_not():
    """Ambient spends on every scene change, which is exactly the load that
    would leave the caller's own request with nothing left."""
    budget = VisionBudget(max_per_minute=8)
    ambient_taken = 0
    while budget.try_consume_ambient() is not None:
        ambient_taken += 1
    assert ambient_taken == 6  # eight minus a reserve of two
    # And the caller can still look.
    assert budget.try_consume() is not None


async def test_an_exhausted_budget_stops_the_pass_rather_than_moving_on():
    sink = Sink()
    budget = VisionBudget(max_per_minute=3)
    for _ in range(budget.max_per_minute - budget.reserve):
        budget.try_consume()
    ambient = AmbientVision(FakeCall(), sink, enabled=True, budget=budget)
    ambient.offer(_frame("a", source="screenshare"))
    ambient.offer(_frame("b", source="camera"))
    await _settle()
    assert sink.images == []


# ---------------------------------------------------------------- the queue


async def test_frames_are_held_until_the_provider_is_ready():
    ready = False
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True, sink_ready=lambda: ready)
    ambient.offer(_frame("the first slide"))
    await _settle()
    assert sink.images == []
    assert ambient.queued == 1

    ready = True
    ambient.flush()
    await _settle()
    assert len(sink.images) == 1
    assert ambient.queued == 0


async def test_the_held_queue_is_bounded():
    """A sink that never comes up would otherwise hold the whole call's video."""
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True, sink_ready=lambda: False, queue_max=2)
    for index in range(6):
        ambient.offer(_frame(f"slide {index}"))
        await _settle()
    assert ambient.queued == 2


async def test_what_was_held_goes_out_in_order():
    ready = False
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True, sink_ready=lambda: ready)
    for index in range(3):
        ambient.offer(_frame(f"slide {index}"))
        await _settle()

    ready = True
    ambient.flush()
    await _settle()
    assert len(sink.images) == 3


# ------------------------------------------------------------ the bookkeeping


async def test_what_was_shown_can_be_recorded():
    """The hook a meeting recap uses, so the minutes say what was on screen."""
    seen: list[str] = []
    ambient = AmbientVision(
        FakeCall(), Sink(), enabled=True, on_delivered=lambda image: seen.append(image.owner)
    )
    ambient.offer(_frame("the slide"))
    await _settle()
    assert seen == ["Dana"]


async def test_attribution_degrades_rather_than_vanishing():
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True)
    ambient.offer(_frame("a slide", name=""))
    await _settle()
    assert sink.images[0].owner == "a participant"


async def test_closing_makes_it_permanently_inert():
    sink = Sink()
    ambient = AmbientVision(FakeCall(), sink, enabled=True)
    await ambient.aclose()
    ambient.offer(_frame("after the call"))
    ambient.flush()
    await _settle()
    assert sink.images == []
