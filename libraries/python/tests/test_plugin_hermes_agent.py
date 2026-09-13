# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The Hermes plugin's own behaviour, with no Hermes host and no provider.

The Hermes host is not importable here and never will be in CI: it is not on
PyPI, it is the application the SDK is installed INTO. So the plugin is
built so everything except the consult itself is reachable without it, and this
file is the proof - it runs on a bare interpreter with no API key, no Microsoft
tenant and no StandIn account, which is what makes a fork's pull request pass.

What is pinned here: the seam, the import boundary, the two defects fixed in the
port, the echo guard's decision table, the gate, the audio path, and every
callback's behaviour before start and after close.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

# The group gate and the echo guard are the SDK's, not this plugin's. They used
# to be re-exported from here; the re-exports are gone, so the test reaches for
# the one definition like every other caller does.
from standin import (
    FRAME_BYTES,
    REALTIME_SAMPLE_RATE_HZ,
    SAMPLE_RATE_HZ,
    Caller,
    CallHandler,
    Consultant,
    SessionStart,
    StandInError,
    echo_guard,
    gate,
)
from standin.minutes import Transcript

# Imported outright, not with importorskip. One package ships the Hermes
# plugin in the base install and it holds no import of the host, so a skip
# here could only ever hide a real break.
from standin.plugins import hermes_agent
from standin.plugins.hermes_agent import (
    api as api,
)
from standin.plugins.hermes_agent import config as hconfig
from standin.plugins.hermes_agent import consult as hconsult
from standin.plugins.hermes_agent import (
    handler,
    realtime,
    recap,
    tools,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------- fakes


class FakeSession:
    """A CallSession that records what the plugin does to the wire."""

    def __init__(self, start: SessionStart, participant_count: int = 1) -> None:
        self.start = start
        self.call_id = start.call_id
        self.participant_count = participant_count
        # The active speaker on unmixed audio; None on the mixed path, like the SDK.
        self.speaker: str | None = None
        self.sent: list[bytes] = []
        self.ended: list[str] = []
        self.events: list[str] = []
        self.emotions: list[str] = []
        self.marks: list[list] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)
        self.events.append("send_audio")

    async def cancel_playback(self) -> None:
        self.events.append("cancel_playback")

    async def express(self, emotion: str) -> None:
        self.emotions.append(emotion)
        self.events.append("express")

    async def send_speech_marks(self, marks) -> None:
        self.marks.append(list(marks))
        self.events.append("send_speech_marks")

    async def end(self, reason: str) -> None:
        self.ended.append(reason)
        self.events.append("end")


class FakeRealtime:
    """A RealtimeSession that records calls instead of opening a socket."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self._response_active = True
        self.tools: list[dict] = []
        self.instructions = ""

    @property
    def response_active(self) -> bool:
        return self._response_active

    async def cancel_response(self) -> None:
        self.calls.append(("cancel_response", None))
        self._response_active = False

    async def send_user_text(self, text: str, *, respond: bool = True) -> None:
        self.calls.append(("send_user_text", (text, respond)))
        if respond and not self._response_active:
            self._response_active = True
            self.calls.append(("response.create", None))

    async def request_say(self, instruction: str) -> None:
        await self.send_user_text(instruction, respond=True)

    async def interrupt_and_say(self, instruction: str) -> None:
        await self.cancel_response()
        await self.send_user_text(instruction, respond=True)

    async def create_response(self) -> None:
        self.calls.append(("create_response", None))

    async def push_audio(self, pcm: bytes) -> None:
        self.calls.append(("push_audio", len(pcm)))

    async def set_auto_response(self, enabled: bool) -> None:
        self.calls.append(("set_auto_response", enabled))

    async def update_instructions(self, instructions: str) -> None:
        self.instructions = instructions
        self.calls.append(("update_instructions", None))

    async def send_function_result(self, call_id: str, output: str) -> None:
        self.calls.append(("send_function_result", (call_id, output)))

    async def close(self) -> None:
        self.calls.append(("close", None))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _start(**kwargs) -> SessionStart:
    fields = {
        "call_id": "call-1",
        "thread_id": "",
        "caller": Caller(aad_id="aad-1", display_name="Alaa Elhenawy", tenant_id="t1"),
        "direction": "inbound",
    }
    fields.update(kwargs)
    return SessionStart(**fields)


RT_ENV = {"OPENAI_API_KEY": "sk-not-a-real-key"}


# ---------------------------------------------------------------- meeting recap


class _FakeLane:
    """A chat lane that records what it was asked to post."""

    def __init__(self) -> None:
        self.posted: list[dict[str, str]] = []

    async def send(self, *, tenant_id: str, conversation_id: str, text: str) -> bool:
        self.posted.append(
            {"tenant_id": tenant_id, "conversation_id": conversation_id, "text": text}
        )
        return True


class _FakeConsult:
    def __init__(self, answer: str = "- Ship on Friday") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


def _spoken() -> Transcript:
    transcript = Transcript()
    transcript.add("Dana", "we agreed to ship on Friday")
    transcript.add("Assistant", "noted, Friday it is", role="assistant")
    return transcript


async def test_a_group_call_posts_its_minutes_to_the_thread() -> None:
    """Two people on a call whose thread is not meeting-shaped: the participant
    count is what routes the minutes to the shared thread, not to one caller's
    private chat. The count lives on the session as participant_count."""
    lane = _FakeLane()
    consult = _FakeConsult()
    recap.set_chat_lane(lane)
    try:
        session = FakeSession(
            _start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2
        )
        await recap.run_meeting_recap(
            session=session, transcript=_spoken(), consult=consult, enabled=True
        )
    finally:
        recap.set_chat_lane(None)
    assert len(consult.prompts) == 1
    assert len(lane.posted) == 1
    assert lane.posted[0]["conversation_id"] == "19:group-thread"
    assert lane.posted[0]["tenant_id"] == "t1"
    assert "Ship on Friday" in lane.posted[0]["text"]


async def test_recap_with_no_lane_spends_no_consult() -> None:
    consult = _FakeConsult()
    recap.set_chat_lane(None)
    session = FakeSession(_start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2)
    await recap.run_meeting_recap(
        session=session, transcript=_spoken(), consult=consult, enabled=True
    )
    assert consult.prompts == []


async def test_recap_is_scheduled_off_the_teardown_path() -> None:
    """aclose must not wait on the summarising consult: the SDK frees the
    call's slot only after aclose returns."""
    lane = _FakeLane()
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowConsult:
        async def ask(self, prompt: str) -> str:
            started.set()
            await release.wait()
            return "minutes"

    recap.set_chat_lane(lane)
    try:
        session = FakeSession(
            _start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2
        )
        task = recap.schedule_meeting_recap(
            session=session, transcript=_spoken(), consult=_SlowConsult(), enabled=True
        )
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=2)
        assert lane.posted == [], "the consult is still running, nothing posted yet"
        release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        recap.set_chat_lane(None)
    assert len(lane.posted) == 1
    assert _spool() == []


def _spool() -> list[Path]:
    """Recap files this test's STANDIN_RECAP_DIR currently holds."""
    root = Path(os.environ["STANDIN_RECAP_DIR"])
    if not root.exists():
        return []
    return [
        path
        for path in root.iterdir()
        if path.name.endswith(".json") or path.name.endswith(".claimed")
    ]


async def test_schedule_writes_the_spool_before_the_consult_starts() -> None:
    """aclose returns after the transcript is on disk, not after the minutes
    have been written. A restart in between still has something to drain."""
    lane = _FakeLane()
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowConsult:
        async def ask(self, prompt: str) -> str:
            started.set()
            await release.wait()
            return "minutes"

    recap.set_chat_lane(lane)
    try:
        session = FakeSession(
            _start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2
        )
        task = recap.schedule_meeting_recap(
            session=session, transcript=_spoken(), consult=_SlowConsult(), enabled=True
        )
        assert task is not None
        assert _spool(), "the transcript must be on disk before the consult starts"
        await asyncio.wait_for(started.wait(), timeout=2)
        assert _spool(), "a recap in flight is claimed, not deleted"
        release.set()
        await asyncio.wait_for(task, timeout=2)
    finally:
        recap.set_chat_lane(None)
    assert _spool() == []


async def test_a_restart_still_posts_the_minutes() -> None:
    """No chat lane at hang-up: the recap sits on disk. Opening the lane and
    draining is what a process restart looks like."""
    recap.set_chat_lane(None)
    session = FakeSession(_start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2)
    task = recap.schedule_meeting_recap(
        session=session, transcript=_spoken(), consult=_FakeConsult(), enabled=True
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=2)
    assert _spool(), "a recap with nowhere to post must be kept"

    lane = _FakeLane()
    recap.set_chat_lane(lane)
    try:
        posted = await recap.drain_recap_spool(consult=_FakeConsult())
    finally:
        recap.set_chat_lane(None)
    assert posted == 1
    assert len(lane.posted) == 1
    assert "Ship on Friday" in lane.posted[0]["text"]
    assert _spool() == []


async def test_a_failed_consult_leaves_the_spool() -> None:
    class _Boom:
        async def ask(self, prompt: str) -> str:
            raise RuntimeError("agent down")

    lane = _FakeLane()
    recap.set_chat_lane(lane)
    try:
        session = FakeSession(
            _start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2
        )
        task = recap.schedule_meeting_recap(
            session=session, transcript=_spoken(), consult=_Boom(), enabled=True
        )
        assert task is not None
        await asyncio.wait_for(task, timeout=2)
    finally:
        recap.set_chat_lane(None)
    assert lane.posted == []
    assert _spool()


async def test_two_drains_do_not_double_post() -> None:
    recap.set_chat_lane(None)
    session = FakeSession(_start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2)
    task = recap.schedule_meeting_recap(
        session=session, transcript=_spoken(), consult=_FakeConsult(), enabled=True
    )
    assert task is not None
    await asyncio.wait_for(task, timeout=2)

    lane = _FakeLane()
    recap.set_chat_lane(lane)
    try:
        await asyncio.gather(
            recap.drain_recap_spool(consult=_FakeConsult()),
            recap.drain_recap_spool(consult=_FakeConsult()),
        )
    finally:
        recap.set_chat_lane(None)
    assert len(lane.posted) == 1


def test_an_empty_transcript_is_not_spooled() -> None:
    session = FakeSession(_start(thread_id="19:group-thread", tenant_id="t1"), participant_count=2)
    assert (
        recap.schedule_meeting_recap(
            session=session, transcript=Transcript(), consult=_FakeConsult(), enabled=True
        )
        is None
    )
    assert _spool() == []


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No MSTEAMS_BRIDGE_* or provider variable leaks in from the machine."""
    for key in list(dict(__import__("os").environ)):
        if key.startswith(("MSTEAMS_BRIDGE_", "STANDIN_")) or key in (
            "OPENAI_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "AZURE_FOUNDRY_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)
    for k, v in RT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STANDIN_RECAP_DIR", str(tmp_path / "recap"))


def _handler(**plugin_kwargs) -> handler.RealtimeHandler:
    policy = hconfig.PluginConfig(allow_all=True, require_recording=False, **plugin_kwargs)
    return handler.RealtimeHandler(config=realtime.realtime_config({}), plugin=policy)


async def _started(session: FakeSession, **plugin_kwargs):
    """A handler wired to a FakeRealtime, past on_start, without a socket."""
    h = _handler(**plugin_kwargs)
    rt = FakeRealtime()
    h._call = session
    h._rt = rt
    h._gate = gate.GroupGate(wake_phrases=h._plugin.wake_phrases, thread_id=session.start.thread_id)
    h._consult = hconsult.AgentConsult(session_id="teams:test")
    h._tools = tools.ToolRunner(consult=h._consult, set_language=h.set_call_language)
    return h, rt


# ---------------------------------------------------------------- the seam


def test_handler_satisfies_the_sdk_seam() -> None:
    """The plugin is a CallHandler and nothing more: no base class, no ABC."""
    h = _handler()
    assert isinstance(h, CallHandler)
    for method in ("on_start", "on_caller_audio", "on_context", "on_goodbye", "aclose"):
        assert callable(getattr(h, method)), method
    assert handler.RealtimeHandler.__mro__[1:] == (object,)


def test_missing_realtime_key_fails_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misconfigured worker must say so at startup, not on the first call."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(StandInError) as err:
        handler.RealtimeHandler(config=realtime.realtime_config({}))
    assert "realtime API key" in str(err.value)


# ------------------------------------------------------- the import boundary

HOST_ROOTS = api.HOST_ROOTS
PKG_DIR = Path(api.__file__).resolve().parent


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_only_hermes_api_imports_the_host() -> None:
    """The whole package except the boundary module is Hermes-import-free.

    This is what makes every other test in this file possible, and what keeps a
    host version bump to one file to re-read.
    """
    offenders = {
        str(py.relative_to(PKG_DIR)): sorted(_imported_roots(py) & HOST_ROOTS)
        for py in PKG_DIR.rglob("*.py")
        if py.name != "api.py" and (_imported_roots(py) & HOST_ROOTS)
    }
    assert not offenders, f"Hermes imports outside api.py: {offenders}"


def test_the_package_imports_with_no_host() -> None:
    """Absence of Hermes is a normal state, not an error."""
    assert api.host_available() is False
    assert api.plugin_context() is None


def test_missing_host_is_a_named_error_with_a_sentence() -> None:
    """Not a bare ModuleNotFoundError out of an import statement."""
    with pytest.raises(api.HermesUnavailable) as err:
        api.build_consult_agent()
    assert issubclass(api.HermesUnavailable, ImportError)
    message = str(err.value)
    assert "run_agent" in message and "plugins.enabled" in message


def test_config_helpers_degrade_to_empty_without_a_host() -> None:
    assert api.load_hermes_config() == {}
    assert api.plugin_config_block() == {}
    assert api.model_config_block() == {}
    assert api.soul_text() == ""
    assert api.skills_index_text() == ""


def test_probe_reports_every_surface_and_never_raises() -> None:
    rows = api.probe_boundaries()
    assert rows and all(set(r) == {"surface", "ok", "detail"} for r in rows)
    # Every miss names what it costs; a row that only says "missing" sends the
    # operator to the source to find out what stopped working.
    assert all(r["detail"] for r in rows if not r["ok"])


# ------------------------------------------------------ defect 1: the goodbye


async def test_goodbye_cancels_before_it_speaks() -> None:
    """The shipped bug: a goodbye arriving mid-turn was silently never spoken.

    request_say creates a response only when none is active, and StandIn sends
    the goodbye precisely when one usually is - to end a call in progress. The
    order below is the fix, and it is the whole point of the method.
    """
    session = FakeSession(_start())
    h, rt = await _started(session)
    rt._response_active = True  # the model is mid-answer

    await h.on_goodbye("Thanks for calling, goodbye.")

    assert rt.names() == ["cancel_response", "send_user_text", "response.create"]
    said = rt.calls[1][1][0]
    assert "Thanks for calling, goodbye." in said
    # And the wire was flushed first, so the line is not queued behind buffered
    # audio the call is about to be torn down on top of.
    assert session.events[0] == "cancel_playback"


async def test_goodbye_clears_a_latched_group_drop() -> None:
    """An unaddressed meeting turn latches the egress drop. A goodbye dropped on
    the way out is the same failure a second time, one layer down."""
    session = FakeSession(_start())
    h, rt = await _started(session)
    h._drop_response = True
    await h.on_goodbye("Goodbye.")
    assert h._drop_response is False


async def test_goodbye_is_inert_with_no_text() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.on_goodbye("   ")
    assert rt.calls == []


# ------------------------------------------------------- defect 2: the gate


def test_a_meeting_thread_arms_the_gate_with_no_participant_count() -> None:
    """The shipped bug: the gate keyed on a count that never arrives on the
    meeting-join path, so it never fired and the assistant answered every turn
    of every meeting. The thread id is present on every call."""
    g = gate.GroupGate(wake_phrases=("assistant",), thread_id="19:meeting_abc@thread.v2")
    assert g.is_group is True
    assert g.active is True
    assert g.decide("what do we think about Q3?", 0.0).respond is False
    assert g.decide("assistant, what do we think?", 0.0).respond is True


def test_a_one_to_one_call_always_answers() -> None:
    g = gate.GroupGate(wake_phrases=("assistant",), thread_id="")
    assert g.is_group is False
    assert g.decide("what do we think about Q3?", 0.0).respond is True


def test_a_participant_count_still_arms_the_gate() -> None:
    """The count is kept as a second signal; either one is enough."""
    g = gate.GroupGate(wake_phrases=("assistant",), thread_id="")
    g.note_participants(3)
    assert g.is_group is True
    assert g.decide("unrelated chatter", 0.0).respond is False


def test_a_count_of_one_cannot_disarm_a_meeting_thread() -> None:
    """The count is the signal that goes missing, so it may add certainty and
    must never remove it."""
    g = gate.GroupGate(wake_phrases=("assistant",), thread_id="19:meeting@thread.v2")
    g.note_participants(1)
    assert g.is_group is True


def test_the_follow_up_window_keeps_the_floor() -> None:
    g = gate.GroupGate(
        wake_phrases=("assistant",), thread_id="19:m@thread.v2", follow_up_window_ms=10_000
    )
    assert g.decide("assistant, summarise that", 1_000.0).addressed is True
    assert g.decide("and the second point?", 5_000.0).respond is True
    assert g.decide("much later", 60_000.0).respond is False


def test_a_gate_with_no_wake_phrase_is_off() -> None:
    """An unopenable gate would mute the assistant for the whole call."""
    g = gate.GroupGate(wake_phrases=(), thread_id="19:m@thread.v2")
    assert g.active is False
    assert g.decide("anything", 0.0).respond is True


def test_require_address_off_turns_the_gate_off() -> None:
    g = gate.GroupGate(
        wake_phrases=("assistant",), require_address=False, thread_id="19:m@thread.v2"
    )
    assert g.decide("anything", 0.0).respond is True


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("assistant", True),
        ("Assistant, are you there?", True),
        ("assistants are useful", False),
        ("my assistant-like helper", True),
        ("nothing here", False),
    ],
)
def test_is_addressed_uses_word_boundaries(transcript: str, expected: bool) -> None:
    assert gate.is_addressed(transcript, ("assistant",)) is expected


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("stop", True),
        ("please stop", True),
        ("hermes, stop", True),
        ("توقف", True),
        ("arrête", True),
        ("stop by the store on the way", False),
        ("", False),
    ],
)
def test_verbal_interrupts_match_whole_utterances(transcript: str, expected: bool) -> None:
    assert gate.is_verbal_interrupt(transcript, ("hermes",)) is expected


def test_is_meeting_thread() -> None:
    assert gate.is_meeting_thread("19:meeting_x@thread.v2") is True
    assert gate.is_meeting_thread("") is False
    assert gate.is_meeting_thread(None) is False
    assert gate.is_meeting_thread("a:19:looks-like-one") is False


# ------------------------------------------------------------- the echo guard


def test_the_opening_greeting_cannot_interrupt_itself() -> None:
    """Before the caller's first real turn, no barge-in at all: the greeting
    echoing off a speakerphone is exactly the loud thing that would trigger one,
    and the assistant would re-greet itself in a loop with nobody talking."""
    g = echo_guard.EchoGuard()
    g.note_output(1000.0, now=0.0)
    assert g.allow_input(1.0, now=100.0) is False


def test_a_real_barge_in_gets_through_after_the_first_turn() -> None:
    g = echo_guard.EchoGuard(barge_in_rms=0.04)
    g.mark_caller_turn()
    g.note_output(1000.0, now=0.0)
    assert g.allow_input(0.5, now=100.0) is True  # loud: a person
    assert g.allow_input(0.001, now=100.0) is False  # quiet: our own echo


def test_audio_flows_freely_when_we_are_not_speaking() -> None:
    g = echo_guard.EchoGuard()
    assert g.allow_input(0.0, now=10_000.0) is True


def test_collapse_hands_the_floor_back_immediately() -> None:
    """Without it the guard keeps filtering for the length of the buffer just
    cancelled, which is precisely the words the caller interrupted to say."""
    g = echo_guard.EchoGuard()
    g.mark_caller_turn()
    g.note_output(5000.0, now=0.0)
    assert g.speaking(now=100.0) is True
    g.collapse(now=100.0)
    assert g.speaking(now=100.0) is False
    assert g.allow_input(0.0, now=100.0) is True


def test_the_playout_clock_does_not_fall_behind_real_time() -> None:
    """max(now, horizon) rather than a bare add: after a gap the old horizon is
    in the past, and adding to it would leave the clock behind for the call."""
    g = echo_guard.EchoGuard()
    g.note_output(100.0, now=0.0)
    g.note_output(100.0, now=10_000.0)
    assert g.speaking(now=10_050.0) is True
    assert g.speaking(now=10_800.0) is False


def test_a_disabled_guard_passes_everything() -> None:
    g = echo_guard.EchoGuard(enabled=False)
    g.note_output(5000.0, now=0.0)
    assert g.allow_input(0.0, now=0.0) is True


def test_pcm16_rms() -> None:
    assert echo_guard.pcm16_rms(b"") == 0.0
    assert echo_guard.pcm16_rms(b"\x00\x00" * 160) == 0.0
    loud = echo_guard.pcm16_rms((16384).to_bytes(2, "little", signed=True) * 160)
    assert 0.49 < loud < 0.51
    # An odd trailing byte is a glitch; raising here would be a dropped call.
    assert echo_guard.pcm16_rms(b"\x00\x00\x01") == 0.0


# ------------------------------------------------------------- the audio path


async def test_model_audio_is_resampled_and_framed_onto_the_wire() -> None:
    """24 kHz in, whole 640-byte wire frames out, and the playout clock moves."""
    session = FakeSession(_start())
    h, _ = await _started(session)
    # 100 ms at 24 kHz.
    pcm24 = b"\x01\x02" * (REALTIME_SAMPLE_RATE_HZ // 10)
    await h._on_model_audio(pcm24)
    assert session.sent, "no audio reached the wire"
    assert all(len(f) == FRAME_BYTES for f in session.sent)
    # 100 ms at 16 kHz is 5 frames of 20 ms.
    assert len(session.sent) == 5
    assert h._echo.speaking() is True


async def test_the_residual_is_flushed_at_end_of_turn() -> None:
    """A resampled delta does not divide evenly into a wire frame. Dropping the
    remainder clips the end of every turn."""
    session = FakeSession(_start())
    h, _ = await _started(session)
    await h._on_model_audio(b"\x01\x02" * 100)  # far less than one frame
    assert session.sent == []
    assert h._aligner.pending > 0
    await h._on_response_done()
    assert len(session.sent) == 1
    assert len(session.sent[0]) == FRAME_BYTES  # zero-padded, not dropped


async def test_a_gated_turn_never_reaches_the_wire() -> None:
    """The provider may already be generating by the time the transcript that
    decides arrives, so refusing to SEND is the only real guarantee."""
    session = FakeSession(_start())
    h, _ = await _started(session)
    h._drop_response = True
    await h._on_model_audio(b"\x01\x02" * 2000)
    assert session.sent == []
    assert h._aligner.pending == 0  # and the residual went with it


async def test_caller_audio_is_resampled_up_to_the_model() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    pcm16 = b"\x00\x10" * (SAMPLE_RATE_HZ // 100)  # 10 ms
    await h.on_caller_audio(pcm16)
    assert rt.names() == ["push_audio"]
    # 10 ms at 24 kHz is 240 samples, 480 bytes.
    assert rt.calls[0][1] == 480


async def test_caller_audio_is_dropped_while_we_are_speaking() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    h._echo.note_output(2000.0)
    await h.on_caller_audio(b"\x00\x00" * 160)  # silence: our own echo
    assert rt.calls == []


async def test_caller_audio_waits_for_recording_when_required() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    h._plugin = hconfig.PluginConfig(allow_all=True, require_recording=True)
    await h.on_caller_audio(b"\x01\x02" * 160)
    assert rt.calls == []


# ---------------------------------------------------------------- barge-in


async def test_barge_in_flushes_the_wire_before_cancelling_the_model() -> None:
    """The only order that works. Cancelling the model first stops it
    generating, but the caller still hears every buffered sample StandIn already
    has - the whole length of the answer they interrupted."""
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h._on_model_audio(b"\x01\x02" * 2000)
    session.events.clear()

    await h._on_barge_in()

    assert session.events == ["cancel_playback"]
    assert rt.names() == ["cancel_response"]
    assert h._aligner.pending == 0
    assert h._echo.speaking() is False


# ------------------------------------------------------------- call context


async def test_a_participant_count_reaches_the_gate() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.on_context(
        "There are 4 human participants on this call. Stay quiet unless directly addressed."
    )
    assert h._gate.is_group is True
    assert ("set_auto_response", False) in rt.calls


async def test_a_one_to_one_sentence_enables_auto_response() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.on_context("This is a 1:1 call with a single human caller.")
    assert h._gate.is_group is False
    assert ("set_auto_response", True) in rt.calls


async def test_recording_active_triggers_the_greeting_once() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    h._plugin = hconfig.PluginConfig(allow_all=True, require_recording=True)
    await h.on_context("The Microsoft Teams call recording is now ACTIVE.")
    said = [c for c in rt.calls if c[0] == "send_user_text"]
    assert len(said) == 1 and "Greet" in said[0][1][0] and "Alaa" in said[0][1][0]
    await h.on_context("The Microsoft Teams call recording is now ACTIVE.")
    assert len([c for c in rt.calls if c[0] == "send_user_text"]) == 1


async def test_dtmf_is_a_turn_and_gets_an_answer() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.on_context('The caller pressed the "1" key on their keypad.')
    name, payload = rt.calls[0]
    assert name == "send_user_text"
    assert payload[1] is True  # respond: "press 1 for support" needs an answer


async def test_unknown_context_reaches_the_model_without_provoking_a_reply() -> None:
    """Additive by contract: a sentence a newer StandIn invents is still worth
    the model knowing, and is never worth interrupting a turn for."""
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.on_context("Something new StandIn started sending.")
    assert rt.calls[0][1][1] is False


# ------------------------------------------------------------ the gate in flight


async def test_an_unaddressed_meeting_turn_is_cancelled_and_dropped() -> None:
    session = FakeSession(_start(thread_id="19:meeting@thread.v2"))
    h, rt = await _started(session)
    h._auto_on = False
    await h._on_input_transcript("so anyway, about the budget")
    assert h._drop_response is True
    assert "cancel_response" in rt.names()


async def test_an_addressed_turn_clears_a_stale_drop_and_asks_for_a_reply() -> None:
    """The unaddressed turn created no response, so no response.done ever fired
    to reset the latch. Left set, it would eat THIS answer instead."""
    session = FakeSession(_start(thread_id="19:meeting@thread.v2"))
    h, rt = await _started(session)
    h._auto_on = False
    await h._on_input_transcript("about the budget")
    assert h._drop_response is True
    await h._on_input_transcript("assistant, what did we decide?")
    assert h._drop_response is False
    assert "create_response" in rt.names()


async def test_two_speakers_become_two_transcript_blocks() -> None:
    """Unmixed audio names the active speaker, and each person's words are filed
    under their own name. Filed under the caller alone, the merge would fold
    every attendee into one block with the wrong name on it."""
    session = FakeSession(_start(thread_id="19:meeting@thread.v2"), participant_count=2)
    h, _rt = await _started(session)
    session.speaker = "Dana Reyes"
    await h._on_input_transcript("we agreed to ship on Friday")
    session.speaker = "Omar Haddad"
    await h._on_input_transcript("and the budget stays as it is")
    turns = list(h._transcript.turns)
    assert [t.speaker for t in turns] == ["Dana", "Omar"]
    assert [t.text for t in turns] == [
        "we agreed to ship on Friday",
        "and the budget stays as it is",
    ]


async def test_mixed_audio_still_files_turns_under_the_caller() -> None:
    session = FakeSession(_start())
    h, _rt = await _started(session)
    assert session.speaker is None
    await h._on_input_transcript("hello there")
    assert [t.speaker for t in h._transcript.turns] == ["Alaa"]


async def test_a_verbal_interrupt_cuts_playback_and_suppresses_the_reply() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h._on_input_transcript("stop")
    assert h._drop_response is True
    assert session.events == ["cancel_playback"]
    assert rt.names() == ["cancel_response"]


# ------------------------------------------------------------------- tools


def test_the_tool_set_is_the_two_reachable_ones() -> None:
    names = [t["name"] for t in tools.default_tools()]
    assert names == ["hermes_agent_consult", "set_call_language"]
    assert all(t["type"] == "function" for t in tools.default_tools())


@pytest.mark.parametrize("raw", ["", "not json", "[1,2]", '"a string"', "null"])
def test_tool_arguments_never_raise(raw: str) -> None:
    """Model-generated JSON. A tool with no arguments beats no tool."""
    assert tools.ToolRunner.parse_args(raw) == {}


def test_tool_arguments_parse() -> None:
    assert tools.ToolRunner.parse_args(json.dumps({"query": "x"})) == {"query": "x"}


async def test_an_unknown_tool_answers_in_words() -> None:
    runner = tools.ToolRunner(consult=None, set_language=None)
    out = await runner.run("look_at_screen", {})
    assert "can't do" in out or "isn't something" in out


async def test_set_call_language_rebuilds_and_pushes_the_instructions() -> None:
    """Rebuilt, not patched: a language change must not quietly drop the
    persona, the skills clause or the group etiquette."""
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h._tools.run("set_call_language", {"language": "fr"})
    assert h._cfg.languages == ("fr",)
    assert "French" in rt.instructions
    assert "stay silent unless someone" in rt.instructions


async def test_set_call_language_with_no_code_says_so() -> None:
    session = FakeSession(_start())
    h, _ = await _started(session)
    assert "didn't catch" in await h._tools.run("set_call_language", {})


async def test_the_consult_reaches_the_model_as_words_with_no_host() -> None:
    """Running outside Hermes must sound like a configuration problem, not a
    broken assistant, and must never raise into the provider's tool loop."""
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h._on_function_call("hermes_agent_consult", "call-abc", json.dumps({"query": "hi"}))
    name, (call_id, output) = rt.calls[-1]
    assert name == "send_function_result" and call_id == "call-abc"
    assert "can't reach my tools" in output


# ----------------------------------------------------------------- consult


def _consult_running(agent: Any) -> hconsult.AgentConsult:
    """An AgentConsult whose Hermes agent is replaced by ``agent``.

    The timing, the refusals and the rebuild-after-timeout are the SDK's now, so
    what is left to test here is that this plugin hands the SDK the right thing
    to run and keeps its own boundary message.
    """
    consult = hconsult.AgentConsult()
    consult._build = lambda: agent  # type: ignore[method-assign]
    consult._consultant = Consultant(consult._build, timeout_s=45.0)
    return consult


async def test_an_empty_consult_query_is_answered_not_run() -> None:
    assert "did not catch" in await hconsult.AgentConsult().ask("   ")


async def test_a_timed_out_consult_admits_it_and_promises_nothing() -> None:
    """Never promise a follow-up: the work is abandoned, not queued, and nothing
    will deliver its result."""
    import time

    consult = _consult_running(lambda query: time.sleep(5) or "late")
    out = await consult.ask("something slow", timeout_s=0.05)
    assert "took too long" in out
    assert "get back to you" not in out.lower()


async def test_a_second_concurrent_consult_answers_busy() -> None:
    """Queueing behind an abandoned consult just waits out a second timeout, so
    two dead consults instead of one and an answer."""
    release = asyncio.Event()

    async def slow(query: str) -> str:
        await release.wait()
        return "first"

    consult = _consult_running(slow)
    first = asyncio.create_task(consult.ask("one"))
    await asyncio.sleep(0)
    try:
        assert "still finishing" in await consult.ask("two")
    finally:
        release.set()
        await first


async def test_a_failing_consult_is_still_speakable() -> None:
    def boom(query: str) -> str:
        raise RuntimeError("the agent exploded")

    assert "ran into a problem" in await _consult_running(boom).ask("anything")


async def test_a_consult_outside_a_hermes_host_says_which_problem_it_is() -> None:
    """The named boundary error has to survive the move to the SDK's generic
    failure line, or a configuration problem starts sounding like a broken
    assistant."""
    assert "can't reach my tools" in await hconsult.AgentConsult().ask("anything")


def test_the_consult_carries_a_stable_task_id() -> None:
    c = hconsult.AgentConsult(session_id="teams:aad-1")
    assert c.task_id == "standin:consult:teams:aad-1"


# ------------------------------------------------------------------ config


def test_an_empty_allowlist_denies_everyone() -> None:
    """The alternative - an unconfigured worker answering anyone in the tenant
    who finds its number - is the wrong default to have shipped once."""
    cfg = hconfig.resolve_config({})
    assert hconfig.caller_allowed(cfg, "aad-1", "Alaa") is False


def test_allow_all_is_an_explicit_opt_in() -> None:
    cfg = hconfig.resolve_config({"allow_all": True})
    assert hconfig.caller_allowed(cfg, None, None) is True


def test_the_allowlist_matches_aad_ids_and_not_names_by_default() -> None:
    cfg = hconfig.resolve_config({"allowlist": ["AAD-1"]})
    assert hconfig.caller_allowed(cfg, "aad-1", "x") is True
    assert hconfig.caller_allowed(cfg, "other", "aad-1") is False
    named = hconfig.resolve_config({"allowlist": ["alaa"], "allowlist_allow_names": True})
    assert hconfig.caller_allowed(named, None, "Alaa") is True


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("per-call", "teams:call-1"),
        ("per-thread", "teams:19:meeting@thread.v2"),
        ("per-aad", "teams:aad-1"),
        ("nonsense", "teams:call-1"),
    ],
)
def test_session_scope_keys(scope: str, expected: str) -> None:
    """An unrecognised scope must not silently become the WIDEST one: a typo in
    per-aad would then share one agent memory between callers."""
    cfg = hconfig.resolve_config({"session_scope": scope})
    assert hconfig.session_key(cfg, _start(thread_id="19:meeting@thread.v2")) == expected


def test_a_scope_with_no_key_falls_back_to_the_call() -> None:
    """A guest caller has no AAD id, and two guests must not share a memory."""
    cfg = hconfig.resolve_config({"session_scope": "per-aad"})
    anonymous = _start(caller=Caller(display_name="Guest"))
    assert hconfig.session_key(cfg, anonymous) == "teams:call-1"


def test_wake_phrases_accept_a_yaml_scalar() -> None:
    """Writing `wake_phrases: "assistant, hermes"` is a natural mistake, and
    reading it as one four-word phrase would mute the assistant forever."""
    cfg = hconfig.resolve_config({"wake_phrases": "Assistant, Hermes"})
    assert cfg.wake_phrases == ("assistant", "hermes")


def test_the_environment_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSTEAMS_BRIDGE_SESSION_SCOPE", "per-thread")
    monkeypatch.setenv("MSTEAMS_BRIDGE_ALLOW_ALL", "yes")
    cfg = hconfig.resolve_config({})
    assert cfg.session_scope == "per-thread"
    assert cfg.allow_all is True


def test_the_config_block_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSTEAMS_BRIDGE_SESSION_SCOPE", "per-thread")
    assert hconfig.resolve_config({"session_scope": "per-aad"}).session_scope == "per-aad"


def test_meeting_recap_is_off_unless_asked() -> None:
    """A recap is customer conversation leaving the call. Off is the default."""
    assert hconfig.resolve_config({}).meeting_recap is False
    assert hconfig.resolve_config({"meeting_recap": True}).meeting_recap is True


def test_unknown_config_keys_are_kept() -> None:
    """A forward-compatible config must not silently lose settings."""
    assert hconfig.resolve_config({"future_key": 1}).extra == {"future_key": 1}


# ------------------------------------------------------- the provider config


def test_openai_is_the_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = realtime.realtime_config({})
    assert cfg.base_url == realtime.DEFAULT_BASE_URL
    assert cfg.api_key_header == "Authorization"
    assert cfg.configured is True


def test_an_azure_endpoint_selects_azure() -> None:
    cfg = realtime.realtime_config(
        {
            "azure_endpoint": "https://r.openai.azure.com",
            "azure_deployment": "gpt-realtime",
            "api_key": "k",
        }
    )
    assert cfg.base_url.startswith("wss://r.openai.azure.com/openai/realtime?")
    assert "deployment=gpt-realtime" in cfg.base_url
    assert cfg.api_key_header == "api-key"


def test_an_azure_key_falls_back_to_the_gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Hermes host that already has a gateway key needs no second copy."""
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "azure-key")
    cfg = realtime.realtime_config({"backend": "azure"})
    assert cfg.api_key == "azure-key"


def test_no_key_at_all_is_reported_as_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert realtime.realtime_config({}).configured is False


def test_languages_accept_a_list_or_a_scalar() -> None:
    assert realtime.realtime_config({"languages": ["EN", "ar"]}).languages == ("en", "ar")
    assert realtime.realtime_config({"languages": "en, fr"}).languages == ("en", "fr")


def test_transcription_can_be_turned_off() -> None:
    cfg = realtime.realtime_config({"input_transcribe_model": "off"})
    assert cfg.input_transcribe_model == ""


def test_a_bad_number_falls_back_to_the_default() -> None:
    assert realtime.realtime_config({"vad_threshold": "loud"}).vad_threshold == 0.5


# ------------------------------------------------------------ safe by default


async def test_callbacks_are_inert_before_on_start() -> None:
    """The SDK can deliver context before session.start on a fast dial, and
    cancels a call that never starts. Nothing here may raise."""
    h = _handler()
    await h.on_caller_audio(b"\x01\x02" * 160)
    await h.on_context("early")
    await h.on_goodbye("early goodbye")
    await h.aclose("pre-start-timeout")


async def test_aclose_is_safe_twice() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)
    await h.aclose("first")
    await h.aclose("second")
    assert rt.names().count("close") == 1


async def test_everything_is_dropped_after_close() -> None:
    session = FakeSession(_start())
    h, _ = await _started(session)
    await h.aclose("call-ended")
    await h.on_caller_audio(b"\x01\x02" * 160)
    await h.on_context("late")
    await h.on_goodbye("late goodbye")
    await h._on_model_audio(b"\x01\x02" * 2000)
    assert session.sent == []


async def test_a_caller_off_the_allowlist_is_refused_not_answered() -> None:
    """end() from inside on_start returns immediately instead of deadlocking
    against teardown, which is what makes this a legitimate refusal path."""
    h = handler.RealtimeHandler(config=realtime.realtime_config({}), plugin=hconfig.PluginConfig())
    session = FakeSession(_start())
    await h.on_start(session)
    assert session.ended == ["caller-not-allowlisted"]
    assert h._rt is None  # no provider socket was ever opened


async def test_a_provider_drop_ends_the_call_rather_than_going_quiet() -> None:
    session = FakeSession(_start())
    h, _ = await _started(session)
    await h._on_provider_closed("provider-error")
    assert session.ended == ["realtime-provider-error"]


# --------------------------------------------------------------- readiness


def test_readiness_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` must answer "would a call work right now?" without placing one."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    lines = hermes_agent.report_readiness()
    assert any("no Hermes host" in line for line in lines)
    assert any("no realtime API key" in line for line in lines)
    assert any("STANDIN_SECRET" in line for line in lines)


def test_the_status_tool_returns_json() -> None:
    payload = json.loads(hermes_agent.handle_status())
    assert set(payload) == {"version", "ok", "notes"}
    assert payload["ok"] is False  # no secret in a test environment


def test_the_handler_factory_resolves_config_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config file edited mid-shift must not give two live calls different
    rules, and the host's config is not read on the audio path."""
    factory = hermes_agent.handler_factory()
    a, b = factory(), factory()
    assert a is not b
    assert a._plugin is b._plugin
    assert a._cfg is b._cfg


def test_the_plugin_registers_with_a_hermes_context() -> None:
    """register(ctx) must not start a listener: a plugin that opened a port on
    import would bind it in every Hermes process, CLI invocations included."""

    class Ctx:
        def __init__(self) -> None:
            self.tools: list[str] = []
            self.commands: list[str] = []

        def register_tool(self, *, name, **_kwargs):
            self.tools.append(name)

        def register_cli_command(self, *, name, **_kwargs):
            self.commands.append(name)

    ctx = Ctx()
    try:
        hermes_agent.register(ctx)
        assert ctx.tools == ["msteams_bridge_status"]
        assert ctx.commands == ["msteams-bridge"]
        assert api.plugin_context() is ctx
    finally:
        api.set_plugin_context(None)


def test_register_tolerates_an_older_host() -> None:
    """A context without register_tool must not take the host down at load."""

    class Bare:
        pass

    try:
        hermes_agent.register(Bare())
    finally:
        api.set_plugin_context(None)


def test_no_stray_event_loop_is_needed_for_any_of_this() -> None:
    """A guard on the file's own premise: none of the above needs a socket."""
    assert asyncio.iscoroutinefunction(handler.RealtimeHandler.on_start)


# ------------------------------------------------- a real call, a real socket


class _StubRealtime(FakeRealtime):
    """FakeRealtime with the constructor and connect the handler calls.

    Instances are collected so the test can reach the one a live call built.
    """

    instances: list[_StubRealtime] = []

    def __init__(self, config) -> None:
        super().__init__()
        self.config = config
        _StubRealtime.instances.append(self)

    async def connect(self) -> None:
        self.calls.append(("connect", None))


async def test_a_whole_call_over_a_real_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seam itself, end to end: a signed handshake, session.start, caller
    audio, the goodbye, teardown. Only the provider is stubbed.

    This is where the goodbye fix is worth the most, because the ORDER is the
    SDK's as well as ours: StandIn's assistant.say makes the server flush the
    buffered audio and only then call on_goodbye, and this proves the plugin
    cancels the model's in-flight response before speaking into that gap.
    """
    import socket as _socket

    import aiohttp

    from standin import CallServer
    from standin._hmac import SIGNATURE_HEADER, TIMESTAMP_HEADER, now_ms, sign_handshake

    _StubRealtime.instances.clear()
    monkeypatch.setattr(handler, "RealtimeSession", _StubRealtime)
    monkeypatch.setenv("MSTEAMS_BRIDGE_ALLOW_ALL", "true")
    monkeypatch.setenv("MSTEAMS_BRIDGE_REQUIRE_RECORDING", "false")

    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    secret = "test-secret-never-a-real-one"
    server = CallServer(
        handler_factory=hermes_agent.handler_factory(),
        secret=secret,
        host="127.0.0.1",
        port=port,
        audio_idle_timeout=0.0,
    )
    await server.start()
    try:
        call_id = "call-e2e-1"
        stamp = now_ms()
        headers = {
            TIMESTAMP_HEADER: str(stamp),
            SIGNATURE_HEADER: sign_handshake(secret, stamp, call_id),
        }
        url = f"http://127.0.0.1:{port}{server.ws_path}/{call_id}"
        async with aiohttp.ClientSession() as http, http.ws_connect(url, headers=headers) as ws:
            await ws.send_str(
                json.dumps(
                    {
                        "type": "session.start",
                        "callId": call_id,
                        "threadId": "19:meeting@thread.v2",
                        "direction": "inbound",
                        "caller": {"displayName": "Alaa", "aadId": "aad-1"},
                    }
                )
            )
            for _ in range(50):
                if _StubRealtime.instances:
                    break
                await asyncio.sleep(0.02)
            assert _StubRealtime.instances, "the plugin never opened a realtime session"
            rt = _StubRealtime.instances[0]
            assert "connect" in rt.names()
            # A meeting thread, so the gate is armed and auto-response is off
            # before a single participant message has arrived.
            assert ("set_auto_response", False) in rt.calls
            assert [t["name"] for t in rt.tools] == ["hermes_agent_consult", "set_call_language"]

            import base64

            await ws.send_str(
                json.dumps(
                    {
                        "type": "audio.frame",
                        "payloadBase64": base64.b64encode(b"\x00\x40" * 160).decode(),
                    }
                )
            )
            for _ in range(50):
                if "push_audio" in rt.names():
                    break
                await asyncio.sleep(0.02)
            assert "push_audio" in rt.names()

            rt._response_active = True  # the model is mid-answer
            before = len(rt.calls)
            await ws.send_str(
                json.dumps({"type": "assistant.say", "text": "We have to stop there, goodbye."})
            )
            for _ in range(50):
                if len(rt.calls) > before:
                    break
                await asyncio.sleep(0.02)
            after = [name for name, _ in rt.calls[before:]]
            assert after[:2] == ["cancel_response", "send_user_text"]

            await ws.send_str(json.dumps({"type": "session.end", "reason": "call-ended"}))
            for _ in range(100):
                if server.active_calls == 0:
                    break
                await asyncio.sleep(0.02)
        assert server.active_calls == 0
        assert "close" in rt.names()  # aclose released the provider session
    finally:
        await server.aclose()


# ------------------------------------------------------- the mouth and the face


async def test_a_turn_gets_a_viseme_timeline_spread_over_the_audio_it_sent() -> None:
    """A realtime model hands back no timings, so the only duration this worker
    genuinely knows is what it actually sent."""
    session = FakeSession(_start())
    h, _rt = await _started(session)

    await h._on_reply_text("Hello there")
    # 24 kHz in, 16 kHz out: half a second of speech.
    await h._on_model_audio(bytes(24_000))
    await h._on_response_done()

    assert len(session.marks) == 1
    timeline = session.marks[0]
    assert timeline
    # Strictly increasing, and inside the audio that was sent.
    assert [t for t, _ in timeline] == sorted({t for t, _ in timeline})
    assert timeline[-1][0] <= 500


async def test_the_timeline_goes_once_per_turn_not_once_per_piece_of_text() -> None:
    session = FakeSession(_start())
    h, _rt = await _started(session)

    for piece in ("Hello ", "there, ", "how are you?"):
        await h._on_reply_text(piece)
    assert session.marks == []  # nothing yet: the turn is not over

    await h._on_model_audio(bytes(24_000))
    await h._on_response_done()
    assert len(session.marks) == 1

    # And the next turn starts from zero rather than carrying the last one.
    await h._on_reply_text("Yes")
    await h._on_model_audio(bytes(24_000))
    await h._on_response_done()
    assert len(session.marks) == 2
    assert session.marks[1][-1][0] <= 500


async def test_an_interruption_does_not_make_the_next_mouth_run_long() -> None:
    """On a cut the service flushes audio the caller never heard. Keeping the
    count would spread the next turn's words over its own audio plus the audio
    that was thrown away."""
    session = FakeSession(_start())
    h, _rt = await _started(session)

    await h._on_reply_text("A long answer nobody waited for")
    await h._on_model_audio(bytes(24_000 * 4))
    await h._cut_playback()
    assert h._lip.duration_ms == 0

    await h._on_reply_text("Yes")
    await h._on_model_audio(bytes(24_000))
    await h._on_response_done()
    assert session.marks[-1][-1][0] <= 500


async def test_the_face_changes_as_the_reply_arrives_and_only_when_it_changes() -> None:
    session = FakeSession(_start())
    h, _rt = await _started(session)

    await h._on_reply_text("Sorry, ")
    await h._on_reply_text("I could not find it.")
    # Read on every piece: waiting for the end leaves the face wrong for the
    # whole time the reply is being spoken.
    assert session.emotions == ["sad"]

    await h._on_reply_text(" But that is great news otherwise.")
    assert session.emotions == ["sad"]  # still sad: an apology outranks a nicety


async def test_the_face_thinks_while_a_tool_runs_and_stops_when_it_is_done() -> None:
    session = FakeSession(_start())
    h, rt = await _started(session)

    await h._on_function_call("set_call_language", "c1", '{"code": "ar"}')
    assert session.emotions == ["thinking", "neutral"]
    assert "send_function_result" in rt.names()


async def test_a_tool_that_failed_still_puts_the_face_back() -> None:
    """The model may stay silent after a tool result: with no reply text to
    re-read, the face would stay mid-think for the rest of the call."""
    session = FakeSession(_start())
    h, rt = await _started(session)

    async def explode(name, params):
        raise RuntimeError("the tool is broken")

    h._tools.run = explode
    with contextlib.suppress(RuntimeError):
        await h._on_function_call("anything", "c1", "{}")
    assert session.emotions == ["thinking", "neutral"]


async def test_nothing_cosmetic_can_take_a_turn_down() -> None:
    """The worst acceptable outcome is a still mouth over correct audio."""
    session = FakeSession(_start())
    h, _rt = await _started(session)

    async def refuse(marks):
        raise RuntimeError("the tile is not accepting marks")

    session.send_speech_marks = refuse
    await h._on_reply_text("Hello")
    await h._on_model_audio(bytes(24_000))
    await h._on_response_done()  # must not raise
