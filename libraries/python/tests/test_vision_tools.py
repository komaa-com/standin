# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The five vision and display tools, and the two guards that travel with them.

Every one of these returns a sentence rather than raising, because the caller is
a tool result being read back to something that will say it out loud.
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

import pytest

from standin._exceptions import StandInError
from standin.protocol import Caller, SessionStart
from standin.render import render_file
from standin.vision import VideoFrame
from standin.vision_tools import (
    SLIDESHOW_OVERLAP_MS,
    KeyframeStore,
    ShowItem,
    VisionBudget,
    VisionTools,
    WalkthroughStep,
    display_image_name,
    normalize_display_mode,
)

pytestmark = pytest.mark.unit

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0frame").decode()


def _frame(source: str = "screenshare", name: str = "Dana") -> VideoFrame:
    return VideoFrame(
        source=source,
        ts=1,
        width=1280,
        height=720,
        mime="image/jpeg",
        data_base64=JPEG,
        participant_name=name,
    )


class FakeCall:
    def __init__(
        self, recording: bool = False, frames: dict[str, VideoFrame] | None = None
    ) -> None:
        self.call_id = "call-1"
        self.start = SessionStart(
            call_id="call-1", thread_id="", caller=Caller(display_name="Dana"), direction="inbound"
        )
        self.recording_active = recording
        self.images: list[dict[str, Any]] = []
        self._frames = frames or {}
        self.fail_with: Exception | None = None

    def latest_video_frame(self, source: str | None = None) -> VideoFrame | None:
        if source is not None:
            return self._frames.get(source)
        return self._frames.get("screenshare") or self._frames.get("camera")

    async def display_image(
        self, image, mime="image/jpeg", duration_ms=None, mode=None, caption=None
    ):
        if self.fail_with is not None:
            raise self.fail_with
        self.images.append(
            {
                "image": image,
                "mime": mime,
                "caption": caption,
                "durationMs": duration_ms,
                "mode": mode,
            }
        )


class FakeDescriber:
    def __init__(self, answer: str = "a slide about revenue") -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []
        self.fail = False

    async def describe(self, frame: VideoFrame, question: str) -> str:
        if self.fail:
            raise RuntimeError("the vision endpoint is down")
        self.asked.append((frame.source, question))
        return self.answer


# --------------------------------------------------------------------- look


async def test_look_answers_about_the_newest_frame():
    call = FakeCall(frames={"screenshare": _frame()})
    describer = FakeDescriber()
    tools = VisionTools(call, describer=describer)
    assert await tools.look("What is on the slide?") == "a slide about revenue"
    assert describer.asked == [("screenshare", "What is on the slide?")]


async def test_look_prefers_the_screen_share_but_honours_a_request():
    call = FakeCall(frames={"screenshare": _frame(), "camera": _frame("camera")})
    describer = FakeDescriber()
    tools = VisionTools(call, describer=describer)
    await tools.look()
    await tools.look(source="camera")
    assert [source for source, _ in describer.asked] == ["screenshare", "camera"]


async def test_look_says_so_when_there_is_nothing_to_see():
    tools = VisionTools(FakeCall(), describer=FakeDescriber())
    assert "not sharing" in await tools.look()


async def test_look_says_so_when_no_vision_model_is_configured():
    """Saying it plainly beats a silent tool."""
    tools = VisionTools(FakeCall(frames={"screenshare": _frame()}), describer=None)
    assert "no vision model is configured" in await tools.look()


async def test_a_failed_look_is_refunded_not_charged():
    """A flaky endpoint would otherwise burn a budget the caller paid nothing for."""
    call = FakeCall(frames={"screenshare": _frame()})
    describer = FakeDescriber()
    describer.fail = True
    budget = VisionBudget(max_per_minute=2)
    tools = VisionTools(call, describer=describer, budget=budget)

    assert "could not look" in await tools.look()
    assert budget.spent == 0


async def test_the_budget_stops_a_model_looking_in_a_loop():
    call = FakeCall(frames={"screenshare": _frame()})
    tools = VisionTools(call, describer=FakeDescriber(), budget=VisionBudget(max_per_minute=2))
    assert await tools.look() == "a slide about revenue"
    assert await tools.look() == "a slide about revenue"
    assert "reached its limit" in await tools.look()


def test_the_budget_refunds_the_charge_it_was_given_not_the_newest():
    """Two tool calls overlap. Refunding "the most recent" would refund the
    wrong one and let the budget drift upward under exactly the load it bounds."""
    budget = VisionBudget(max_per_minute=2)
    first = budget.try_consume()
    budget.try_consume()
    assert budget.try_consume() is None

    budget.refund(first)
    assert budget.try_consume() is not None
    assert budget.spent == 2


def test_a_zero_budget_is_no_budget():
    budget = VisionBudget(max_per_minute=0)
    for _ in range(50):
        assert budget.try_consume() is not None


# ---------------------------------------------------------------- keyframes


def test_keyframes_are_only_kept_while_the_call_is_recorded():
    """Keeping a history of somebody's screen is a different promise from
    glancing at it once, and the recording is what told them."""
    store = KeyframeStore()
    assert store.offer(_frame(), recording=False) is False
    assert len(store) == 0
    assert store.offer(_frame(), recording=True) is True
    assert len(store) == 1


def _distinct(index: int, source: str = "screenshare") -> VideoFrame:
    """Frames that differ, so the dedup below does not collapse them."""
    return VideoFrame(
        source=source,
        ts=index,
        width=1280,
        height=720,
        mime="image/jpeg",
        data_base64=base64.b64encode(f"frame-{index}".encode()).decode(),
        participant_name="Dana",
    )


def test_keyframes_are_bounded():
    store = KeyframeStore(capacity=3)
    for index in range(10):
        store.offer(_distinct(index), recording=True)
    assert len(store) == 3


def test_an_unchanged_screen_is_kept_once():
    """A screen nobody touched would otherwise fill the whole store with one
    picture, and looking back would find nothing else."""
    store = KeyframeStore(capacity=5)
    for _ in range(10):
        store.offer(_frame(), recording=True)
    assert len(store) == 1


def test_each_source_keeps_its_own_history():
    """An alternating camera and screen share are two things being shown, not
    one changing."""
    store = KeyframeStore(capacity=5)
    for _ in range(3):
        store.offer(_frame("screenshare"), recording=True)
        store.offer(_frame("camera"), recording=True)
    assert len(store) == 2


async def test_looking_back_needs_a_recorded_call():
    call = FakeCall(recording=False)
    tools = VisionTools(call, describer=FakeDescriber())
    assert "while the call is being recorded" in await tools.look_back()


async def test_looking_back_answers_about_a_frame_already_gone():
    call = FakeCall(recording=True)
    tools = VisionTools(call, describer=FakeDescriber("the earlier slide"))
    tools.keyframes.offer(_frame(), recording=True)
    assert await tools.look_back("what did it say?") == "the earlier slide"


# --------------------------------------------------------------------- show


async def test_show_puts_an_image_on_the_tile():
    call = FakeCall()
    tools = VisionTools(call)
    assert (
        await tools.show(b"\xff\xd8\xff\xe0", "image/jpeg", caption="Q3") == "the caller can see it"
    )
    assert call.images[0]["caption"] == "Q3"


async def test_show_refuses_a_type_the_service_will_not_draw():
    tools = VisionTools(FakeCall())
    assert "must be one of" in await tools.show(b"\x00", "image/gif")


async def test_an_oversized_image_is_a_sentence_not_an_exception():
    """The wire has a hard ceiling. A model must be told it in words, because it
    cannot see an exception."""
    call = FakeCall()
    call.fail_with = ValueError("display.image is 9000000 bytes, over the 1400000 limit")
    tools = VisionTools(call)
    result = await tools.show(b"\xff\xd8", "image/jpeg")
    assert result.startswith("could not show that")
    assert "over the" in result


async def test_a_long_caption_is_trimmed_before_it_reaches_the_screen():
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show(b"\xff\xd8", "image/jpeg", caption="x" * 500)
    assert len(call.images[0]["caption"]) == 200


async def test_show_url_refuses_a_private_address():
    """The URL comes from a model steered by whoever is on the call."""
    tools = VisionTools(FakeCall())
    result = await tools.show_url("http://169.254.169.254/latest/meta-data/")
    assert result.startswith("could not fetch")


async def test_show_url_needs_a_url():
    assert "needs a public" in await VisionTools(FakeCall()).show_url("  ")


# -------------------------------------------------------------- walkthrough


async def test_a_walkthrough_speaks_each_step_before_showing_it():
    """The pacing is in the SDK; the speaking is the plugin's, because only the
    provider knows when a line has finished being said."""
    call = FakeCall()
    said: list[str] = []

    async def speak(text: str) -> None:
        said.append(text)

    tools = VisionTools(call)
    result = await tools.walkthrough(
        [
            WalkthroughStep(say="First, the summary.", image=b"\xff\xd8one"),
            WalkthroughStep(say="Then the detail.", image=b"\xff\xd8two"),
        ],
        speak=speak,
    )
    assert said == ["First, the summary.", "Then the detail."]
    assert len(call.images) == 2
    assert "all 2 steps" in result


async def test_a_walkthrough_stops_when_the_caller_cuts_in():
    call = FakeCall()
    said: list[str] = []
    cut_in = False

    async def speak(text: str) -> None:
        nonlocal cut_in
        said.append(text)
        cut_in = True

    tools = VisionTools(call)
    result = await tools.walkthrough(
        [WalkthroughStep(say="one"), WalkthroughStep(say="two"), WalkthroughStep(say="three")],
        speak=speak,
        interrupted=lambda: cut_in,
    )
    assert said == ["one"]
    assert "the caller interrupted" in result


async def test_a_walkthrough_with_no_steps_says_so():
    assert "nothing to walk through" in await VisionTools(FakeCall()).walkthrough([], speak=None)  # type: ignore[arg-type]


# ----------------------------------------------------------- showing a file


async def test_showing_files_is_off_until_a_root_is_named(monkeypatch):
    """The paths reaching this were chosen by a model a caller is steering."""
    monkeypatch.delenv("STANDIN_SHOW_ROOTS", raising=False)
    with pytest.raises(StandInError, match="showing files is off"):
        await render_file("/etc/passwd")


async def test_a_file_outside_the_allowed_roots_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("STANDIN_SHOW_ROOTS", str(tmp_path))
    outside = tmp_path.parent / "elsewhere.png"
    outside.write_bytes(b"\x89PNG")
    with pytest.raises(StandInError, match="outside the directories"):
        await render_file(str(outside))


async def test_a_traversal_is_judged_by_where_it_lands(monkeypatch, tmp_path):
    """Resolved before the comparison, so "../" and a symlink are both judged by
    where they actually go rather than by how they are spelled."""
    root = tmp_path / "shared"
    root.mkdir()
    monkeypatch.setenv("STANDIN_SHOW_ROOTS", str(root))
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"\x89PNG")
    with pytest.raises(StandInError, match="outside the directories"):
        await render_file(str(root / ".." / "secret.png"))


async def test_an_image_inside_a_root_passes_straight_through(monkeypatch, tmp_path):
    monkeypatch.setenv("STANDIN_SHOW_ROOTS", str(tmp_path))
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    data, mime = await render_file(str(png))
    assert mime == "image/png"
    assert data == b"\x89PNG\r\n\x1a\n"


async def test_an_unshowable_type_names_what_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("STANDIN_SHOW_ROOTS", str(tmp_path))
    binary = tmp_path / "thing.bin"
    binary.write_bytes(b"\x00")
    with pytest.raises(StandInError, match="must be one of"):
        await render_file(str(binary))


async def test_a_missing_file_inside_a_root_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("STANDIN_SHOW_ROOTS", str(tmp_path))
    with pytest.raises(StandInError, match="no such file"):
        await render_file(str(tmp_path / "gone.png"))


async def test_show_file_reports_the_reason_rather_than_raising(monkeypatch):
    monkeypatch.delenv("STANDIN_SHOW_ROOTS", raising=False)
    tools = VisionTools(FakeCall())
    result = await tools.show_file("/etc/passwd")
    assert result.startswith("could not show that file")
    assert "showing files is off" in result


def test_allowed_roots_reads_the_environment(monkeypatch, tmp_path):
    from standin.render import allowed_roots

    monkeypatch.setenv("STANDIN_SHOW_ROOTS", f"{tmp_path}{os.pathsep}{tmp_path / 'more'}")
    roots = allowed_roots()
    assert len(roots) == 2
    assert roots[0] == tmp_path.resolve()


# ---- the display surface -------------------------------------------------


def test_only_the_two_real_modes_survive():
    assert normalize_display_mode("FULLSCREEN ") == "fullscreen"
    assert normalize_display_mode("overlay") == "overlay"
    # Everything a model says that is not a mode: "pip", "full", a number,
    # nothing at all. All of them take the default rather than being passed on.
    for said in ("pip", "inset", "full", "", None, 7):
        assert normalize_display_mode(said) is None
        assert normalize_display_mode(said, "overlay") == "overlay"


def test_a_name_is_only_taken_when_it_looks_like_one():
    assert display_image_name("https://x.example/y/chart.png?v=1", "image/png") == "chart.png"
    assert display_image_name("/srv/decks/q3.jpg", "image/jpeg") == "q3.jpg"
    # A traversal, a bare directory, and nothing at all are refused in favour
    # of a name made from the type: this string is about to be shown to the
    # person on the call.
    assert display_image_name("../../etc/passwd", "image/jpeg") == "image.jpg"
    assert display_image_name("", "image/png") == "image.png"


async def test_the_mode_is_omitted_unless_somebody_chose_one():
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show(b"\xff\xd8\xff", "image/jpeg")
    # Not "fullscreen": a default chosen here would override the service's own.
    assert call.images[0]["mode"] is None

    await tools.show(b"\xff\xd8\xff", "image/jpeg", display="overlay")
    assert call.images[1]["mode"] == "overlay"

    # Nonsense from a model falls back to the configured default, not to the
    # nonsense.
    tools = VisionTools(FakeCall(), default_display_mode="fullscreen")
    await tools.show(b"\xff\xd8\xff", "image/jpeg", display="picture-in-picture")
    assert tools._session.images[0]["mode"] == "fullscreen"


async def test_what_was_shown_is_remembered_only_when_it_arrived():
    call = FakeCall()
    tools = VisionTools(call)
    assert tools.last_shown is None

    await tools.show(b"\xff\xd8\xff", "image/jpeg", name="chart.png")
    assert tools.last_shown is not None
    assert tools.last_shown.name == "chart.png"
    assert tools.last_shown.as_base64() == base64.b64encode(b"\xff\xd8\xff").decode()

    # A send that failed must not leave a picture the caller never saw behind
    # for "send me that" to attach.
    call.fail_with = ValueError("that image is too large")
    result = await tools.show(b"\xff\xd8\xffnewer", "image/jpeg", name="other.png")
    assert result.startswith("could not show that")
    assert tools.last_shown.name == "chart.png"


async def test_the_first_picture_is_up_before_the_model_is_answered(monkeypatch):
    monkeypatch.setattr("standin.vision_tools._MIN_HOLD_MS", 0)
    call = FakeCall()
    tools = VisionTools(call)
    items = [ShowItem(b"\xff\xd8\xff%d" % n, "image/jpeg") for n in range(3)]

    said = await tools.show_many(items, hold_ms=1)
    # The model can say "here it is" and be right: one is already on the tile
    # when the tool returns. Waiting out all three would leave the caller in
    # silence.
    assert len(call.images) == 1
    assert "the first of 3" in said

    await tools._slideshow
    assert len(call.images) == 3
    # Every frame but the last is held a little past the pacing gap, so the
    # tile never blanks between pictures. The last carries no duration, so
    # what stays on screen is the service's own default.
    assert call.images[0]["durationMs"] == 1 + SLIDESHOW_OVERLAP_MS
    assert call.images[1]["durationMs"] == 1 + SLIDESHOW_OVERLAP_MS
    assert call.images[2]["durationMs"] is None


async def test_a_slideshow_says_how_many_it_dropped(monkeypatch):
    monkeypatch.setattr("standin.vision_tools._MIN_HOLD_MS", 0)
    tools = VisionTools(FakeCall())
    said = await tools.show_many(
        [ShowItem(b"\xff\xd8\xff%d" % n, "image/jpeg") for n in range(14)], hold_ms=1
    )
    await tools._slideshow
    assert "showing the first 10 of 14" in said
    assert len(tools._session.images) == 10


async def test_a_newer_slideshow_stops_the_older_one(monkeypatch):
    monkeypatch.setattr("standin.vision_tools._MIN_HOLD_MS", 0)
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show_many([ShowItem(b"\xff\xd8\xffold%d" % n) for n in range(6)], hold_ms=40)
    await tools.show_many([ShowItem(b"\xff\xd8\xffnew%d" % n) for n in range(2)], hold_ms=1)
    await tools._slideshow
    await asyncio.sleep(0.15)
    # One tile. The old slideshow must not go on writing to it underneath the
    # new one.
    assert b"old" not in call.images[-1]["image"]
    assert len(call.images) <= 5


async def test_teardown_stops_the_slideshow(monkeypatch):
    monkeypatch.setattr("standin.vision_tools._MIN_HOLD_MS", 0)
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show_many([ShowItem(b"\xff\xd8\xff%d" % n) for n in range(8)], hold_ms=30)
    sent = len(call.images)
    await tools.reset()
    await asyncio.sleep(0.12)
    assert len(call.images) == sent
    assert tools.last_shown is None


async def test_a_slideshow_of_nothing_says_so():
    tools = VisionTools(FakeCall())
    assert await tools.show_many([]) == "there was nothing to show"


async def test_a_walkthrough_takes_the_tile_from_a_slideshow(monkeypatch):
    monkeypatch.setattr("standin.vision_tools._MIN_HOLD_MS", 0)
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show_many([ShowItem(b"\xff\xd8\xffslide%d" % n) for n in range(8)], hold_ms=30)

    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    await tools.walkthrough(
        [WalkthroughStep("here is step one", b"\xff\xd8\xffwalk", "image/jpeg", "step one")],
        speak,
        display="fullscreen",
    )
    await asyncio.sleep(0.12)
    assert b"walk" in call.images[-1]["image"]
    assert call.images[-1]["mode"] == "fullscreen"


async def test_replacing_a_slideshow_does_not_wait_out_its_gap():
    """A model that shows one set and then another must not stall for the gap.

    Thirty seconds is a legal hold. Waiting one out before the new first
    picture went up would look, to the caller, like the agent had frozen.
    """
    call = FakeCall()
    tools = VisionTools(call)
    await tools.show_many([ShowItem(b"\xff\xd8\xffold%d" % n) for n in range(4)], hold_ms=30_000)
    await asyncio.wait_for(
        tools.show_many([ShowItem(b"\xff\xd8\xffnew")], hold_ms=30_000), timeout=2
    )
    assert b"new" in call.images[-1]["image"]
    await tools.reset()


# ---- showing a web page ---------------------------------------------------


async def test_showing_a_page_needs_a_renderer_and_says_so():
    """No browser lives in this SDK, and none ever will. Without one supplied,
    the answer is a sentence rather than a promise the deployment cannot keep."""
    tools = VisionTools(FakeCall())
    assert await tools.show_page("https://example.com") == (
        "showing web pages is not available on this deployment"
    )
    assert await tools.show_page("", render=lambda url: None) == (
        "that needs a public https URL of a page"
    )


async def test_a_private_address_is_refused_before_the_renderer_runs():
    """The guard is here, not in the plugin.

    A host browser's own private-network protection assumes whoever wrote the
    URL already has a shell on the machine. Here it was written by a model a
    stranger is steering, which is the case that relaxation lets through.
    """
    reached = []

    async def render(url: str) -> tuple[bytes, str]:
        reached.append(url)
        return b"\x89PNG\r\n\x1a\n", "image/png"

    tools = VisionTools(FakeCall())
    said = await tools.show_page("http://169.254.169.254/latest/meta-data/", render=render)
    assert said.startswith("could not open that page")
    assert reached == []


async def test_a_rendered_page_reaches_the_tile_with_a_caption():
    call = FakeCall()

    async def render(url: str) -> tuple[bytes, str]:
        return b"\x89PNG\r\n\x1a\n", "image/png"

    tools = VisionTools(call)
    long_url = "https://example.com/" + "a" * 200
    said = await tools.show_page(long_url, render=render)
    assert said == "the caller can see it"
    assert call.images[0]["mime"] == "image/png"
    # A URL makes a poor caption at any length and a worse one at 220
    # characters, and this one is read out on the caller's screen.
    assert len(call.images[0]["caption"]) == 80
    assert call.images[0]["durationMs"] == 15_000


async def test_a_page_that_never_loads_is_told_in_seconds():
    async def render(url: str) -> tuple[bytes, str]:
        await asyncio.sleep(5)
        raise AssertionError("the renderer should have been given up on")

    tools = VisionTools(FakeCall())
    said = await tools.show_page("https://example.com", render=render, timeout_s=0.01)
    assert said == "that page did not finish loading within 1 second"


def test_showing_a_page_is_not_offered_unless_a_plugin_has_a_renderer():
    from standin.calltools import BUILT_IN_TOOLS, SHOW_PAGE_TOOL

    # An absent tool is honest; one that apologises on every call is not.
    assert SHOW_PAGE_TOOL.name not in [spec.name for spec in BUILT_IN_TOOLS]
