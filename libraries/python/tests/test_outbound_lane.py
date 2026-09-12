# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Placing a call, saying the thing, and what to do when nobody answers.

The three are one capability, and splitting them is how the answer gets lost. A
call is placed because somebody is owed something; if they do not pick up, they
are still owed it.
"""

from __future__ import annotations

from typing import Any

import pytest

from standin.chat import InboundMessage
from standin.outbound import (
    CALL_BACK_TOOL,
    CHAT_CALLBACK_TOOL,
    MAX_PENDING_TEXT_CHARS,
    OutboundError,
    OutboundLane,
    OutboundPolicy,
    PendingMessage,
    PendingMessages,
    PlacedCall,
    call_thread_is_postable,
)
from standin.protocol import Caller, SessionStart

pytestmark = pytest.mark.unit

TARGET = "aad-callee"


class FakeCaller:
    def __init__(self) -> None:
        self.placed: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.fail_with: Exception | None = None

    async def place_call(self, user_object_id: str, tenant_id: str) -> PlacedCall:
        if self.fail_with is not None:
            raise self.fail_with
        self.placed.append((user_object_id, tenant_id))
        return PlacedCall(call_id=f"call-{len(self.placed)}")

    async def cancel_call(self, call_id: str) -> bool:
        self.cancelled.append(call_id)
        return True


class FakeChat:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.ok = ok

    async def send(self, *, tenant_id, conversation_id, text, idempotency_key=None) -> bool:
        self.sent.append(
            {
                "tenantId": tenant_id,
                "conversationId": conversation_id,
                "text": text,
                "idempotencyKey": idempotency_key,
            }
        )
        return self.ok


class FakeSession:
    def __init__(self, call_id: str = "call-1", direction: str = "outbound", recording=False):
        self.call_id = call_id
        self.start = SessionStart(
            call_id=call_id,
            thread_id="",
            caller=Caller(display_name="Dana", aad_id=TARGET, tenant_id="tenant-1"),
            direction=direction,
        )
        self.recording_active = recording
        self.ended: list[str] = []

    async def end(self, reason: str) -> None:
        self.ended.append(reason)


def _lane(tmp_path, **kwargs) -> tuple[OutboundLane, FakeCaller, FakeChat]:
    caller, chat = FakeCaller(), FakeChat()
    lane = OutboundLane(
        caller=caller,
        policy=OutboundPolicy(allowed=frozenset({TARGET})),
        pending=PendingMessages(directory=tmp_path),
        chat=chat,
        tenant_id="tenant-1",
        **kwargs,
    )
    return lane, caller, chat


# ------------------------------------------------------------------ placing


async def test_a_call_parks_what_to_say(tmp_path):
    lane, caller, _ = _lane(tmp_path)
    placed = await lane.place(
        user_object_id=TARGET, text="the report is ready", thread_id="19:chat@thread.v2"
    )
    assert caller.placed == [(TARGET, "tenant-1")]
    held = PendingMessages(directory=tmp_path).waiting()
    assert [m.text for m in held] == ["the report is ready"]
    assert held[0].call_id == placed.call_id
    assert held[0].tenant_id == "tenant-1"


async def test_nothing_to_say_means_no_call(tmp_path):
    """Refused before the ring, so nobody's phone goes off for a message that
    was never going to be sent."""
    lane, caller, _ = _lane(tmp_path)
    with pytest.raises(OutboundError, match="nothing to say"):
        await lane.place(user_object_id=TARGET, text="   ")
    assert caller.placed == []


async def test_a_message_too_long_to_say_is_refused(tmp_path):
    lane, caller, _ = _lane(tmp_path)
    with pytest.raises(OutboundError, match="too long"):
        await lane.place(user_object_id=TARGET, text="x" * (MAX_PENDING_TEXT_CHARS + 1))
    assert caller.placed == []


async def test_somebody_not_on_the_list_is_refused_before_the_ring(tmp_path):
    lane, caller, _ = _lane(tmp_path)
    with pytest.raises(OutboundError, match="allowlist"):
        await lane.place(user_object_id="aad-stranger", text="hello")
    assert caller.placed == []


async def test_a_second_call_to_the_same_person_is_refused(tmp_path):
    lane, caller, _ = _lane(tmp_path)
    await lane.place(user_object_id=TARGET, text="first")
    with pytest.raises(OutboundError, match="already calling"):
        await lane.place(user_object_id=TARGET, text="second")
    assert len(caller.placed) == 1


async def test_a_call_with_no_real_conversation_parks_no_fallback(tmp_path):
    """A one-to-one call has no meeting chat, and posting to what the field
    carries instead would fail or reach the wrong place."""
    lane, _, _ = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id=" ")
    assert PendingMessages(directory=tmp_path).waiting()[0].thread_id == ""
    assert placed.call_id


@pytest.mark.parametrize(
    ("thread", "call", "postable"),
    [
        ("19:meeting@thread.v2", "call-1", True),
        ("call-1", "call-1", False),
        ("", "call-1", False),
        ("8:orgid:something", "call-1", False),
    ],
)
def test_which_threads_are_postable(thread: str, call: str, postable: bool):
    assert call_thread_is_postable(thread, call) is postable


# ---------------------------------------------------------------- answering


async def test_the_parked_line_is_said_when_they_answer(tmp_path):
    lane, _, _ = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="the report is ready")

    said: list[str] = []
    session = FakeSession(call_id=placed.call_id, recording=False)

    async def speak(message: PendingMessage) -> None:
        said.append(message.text)

    leg = lane.attach(session, speak)
    assert leg is not None
    assert said == []  # still ringing

    session.recording_active = True
    await leg.on_context()
    assert said == ["the report is ready"]
    # Said once, so the record is gone.
    assert PendingMessages(directory=tmp_path).waiting() == []


async def test_it_is_said_only_once(tmp_path):
    lane, _, _ = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello")
    said: list[str] = []
    session = FakeSession(call_id=placed.call_id, recording=True)

    async def speak(message: PendingMessage) -> None:
        said.append(message.text)

    leg = lane.attach(session, speak)
    await leg.on_context()
    await leg.on_context()
    await leg.answered()
    assert said == ["hello"]


async def test_an_inbound_call_attaches_nothing(tmp_path):
    """So a plugin can call attach unconditionally from on_start."""
    lane, _, _ = _lane(tmp_path)

    async def speak(message: PendingMessage) -> None:  # pragma: no cover
        raise AssertionError("nothing to say on an inbound call")

    assert lane.attach(FakeSession(direction="inbound"), speak) is None


async def test_an_unanswered_leg_gives_the_message_back(tmp_path):
    """Nobody heard it, so it still has to reach them somehow."""
    lane, _, _ = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    session = FakeSession(call_id=placed.call_id)

    async def speak(message: PendingMessage) -> None:  # pragma: no cover
        raise AssertionError("they never answered")

    leg = lane.attach(session, speak)
    await leg.aclose("caller-hung-up")
    assert [m.call_id for m in PendingMessages(directory=tmp_path).waiting()] == [placed.call_id]


async def test_a_reserved_message_is_invisible_to_the_sweep(tmp_path):
    """The sweep must not tell somebody it could not reach them while their
    phone is still ringing."""
    lane, _, chat = _lane(tmp_path, answer_timeout_s=0.0)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    session = FakeSession(call_id=placed.call_id)

    async def speak(message: PendingMessage) -> None:
        pass

    lane.attach(session, speak)
    assert await lane.sweep() == 0
    assert chat.sent == []


async def test_a_speak_that_fails_does_not_lose_the_message(tmp_path):
    lane, _, _ = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    session = FakeSession(call_id=placed.call_id, recording=True)

    async def boom(message: PendingMessage) -> None:
        raise RuntimeError("the provider went away")

    leg = lane.attach(session, boom)
    await leg.on_context()
    await leg.aclose("handler-failure")
    assert [m.call_id for m in PendingMessages(directory=tmp_path).waiting()] == [placed.call_id]


# ------------------------------------------------------------ not answering


async def test_an_unanswered_call_puts_the_answer_in_chat(tmp_path):
    lane, _, chat = _lane(tmp_path)
    placed = await lane.place(
        user_object_id=TARGET, text="the report is ready", thread_id="19:c@thread.v2"
    )
    assert await lane.on_outcome(placed.call_id, "no-answer") is True
    assert len(chat.sent) == 1
    assert "couldn't reach you" in chat.sent[0]["text"]
    assert "the report is ready" in chat.sent[0]["text"]
    assert chat.sent[0]["conversationId"] == "19:c@thread.v2"


@pytest.mark.parametrize(
    ("outcome", "wording"),
    [
        ("declined", "declined my call"),
        ("busy", "line was busy"),
        ("failed", "could not be completed"),
    ],
)
async def test_each_outcome_says_what_happened(tmp_path, outcome: str, wording: str):
    lane, _, chat = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    await lane.on_outcome(placed.call_id, outcome)
    assert wording in chat.sent[0]["text"]


async def test_an_answered_outcome_does_nothing(tmp_path):
    lane, _, chat = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    assert await lane.on_outcome(placed.call_id, "answered") is True
    assert chat.sent == []
    assert PendingMessages(directory=tmp_path).waiting()


async def test_an_outcome_this_sdk_does_not_know_is_ignored(tmp_path):
    """Treating an unknown word as a failure would post "I could not reach you"
    to somebody who answered."""
    lane, _, chat = _lane(tmp_path)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    assert await lane.on_outcome(placed.call_id, "transferred") is True
    assert chat.sent == []
    assert PendingMessages(directory=tmp_path).waiting()


async def test_the_timer_and_the_outcome_tell_them_once(tmp_path):
    lane, _, chat = _lane(tmp_path, answer_timeout_s=0.0)
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    await lane.sweep()
    await lane.on_outcome(placed.call_id, "no-answer")
    keys = [s["idempotencyKey"] for s in chat.sent]
    assert keys == [f"standin-noanswer-{placed.call_id}"]


async def test_the_ringing_leg_is_cancelled_on_the_timer_path_only(tmp_path):
    lane, caller, _ = _lane(tmp_path, answer_timeout_s=0.0)
    first = await lane.place(user_object_id=TARGET, text="a", thread_id="19:c@thread.v2")
    await lane.sweep()
    assert caller.cancelled == [first.call_id]

    caller.cancelled.clear()
    second = await lane.place(user_object_id=TARGET, text="b", thread_id="19:c@thread.v2")
    await lane.on_outcome(second.call_id, "no-answer")
    # The call already ended on its own; cancelling it would be noise.
    assert caller.cancelled == []


async def test_a_call_with_nowhere_to_post_is_retired_loudly(tmp_path):
    lane, _, chat = _lane(tmp_path, answer_timeout_s=0.0)
    await lane.place(user_object_id=TARGET, text="hello")
    assert await lane.sweep() == 0
    assert chat.sent == []
    assert PendingMessages(directory=tmp_path).waiting() == []


async def test_a_failed_delivery_is_tried_again(tmp_path):
    lane, _, chat = _lane(tmp_path, answer_timeout_s=0.0)
    chat.ok = False
    placed = await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    await lane.sweep()
    held = PendingMessages(directory=tmp_path).waiting()
    assert [m.call_id for m in held] == [placed.call_id]
    assert held[0].attempts == 1
    # And the original time is kept, so it still ages out on schedule.
    assert held[0].created_ms > 0


async def test_a_delivery_that_keeps_failing_stops(tmp_path):
    lane, _, chat = _lane(tmp_path, answer_timeout_s=0.0)
    chat.ok = False
    await lane.place(user_object_id=TARGET, text="hello", thread_id="19:c@thread.v2")
    for _ in range(8):
        await lane.sweep()
    assert PendingMessages(directory=tmp_path).waiting() == []


# --------------------------------------------------------- ringing them back


def _chat_message(**over: Any) -> InboundMessage:
    fields = {
        "tenant_id": "tenant-1",
        "conversation_id": "19:c@thread.v2",
        "activity_id": "a1",
        "scope": "personal",
        "text": "call me",
        "sender_aad_id": TARGET,
        "sender_name": "Dana",
    }
    return InboundMessage(**{**fields, **over})


def test_who_to_ring_comes_from_the_message_not_the_model(tmp_path):
    """An agent that can be told who to ring can be told to ring anybody."""
    lane, _, _ = _lane(tmp_path)
    lane.remember_chat_sender(_chat_message())
    target = lane.chat_callback_target("19:c@thread.v2")
    assert target.user_object_id == TARGET
    assert target.tenant_id == "tenant-1"


def test_an_unknown_conversation_is_a_sentence_not_an_error(tmp_path):
    """The caller is a tool result a model reads out loud."""
    lane, _, _ = _lane(tmp_path)
    assert isinstance(lane.chat_callback_target("19:nobody"), str)


def test_a_message_with_no_sender_is_not_recorded(tmp_path):
    lane, _, _ = _lane(tmp_path)
    lane.remember_chat_sender(_chat_message(sender_aad_id=None))
    assert isinstance(lane.chat_callback_target("19:c@thread.v2"), str)


def test_an_old_conversation_will_not_be_rung(tmp_path):
    from standin.outbound import CHAT_CALLBACK_WINDOW_S

    lane, _, _ = _lane(tmp_path)
    lane.remember_chat_sender(_chat_message())
    record = lane._senders["19:c@thread.v2"]
    record.at_ms -= int((CHAT_CALLBACK_WINDOW_S + 60) * 1000)
    assert "Ask me again" in lane.chat_callback_target("19:c@thread.v2")


def test_neither_tool_takes_a_target(tmp_path):
    """There is no target parameter, and there never will be."""
    for spec in (CHAT_CALLBACK_TOOL, CALL_BACK_TOOL):
        assert set(spec.parameters) == {"message"}
        assert spec.required == ("message",)


# --------------------------------------------------------------- the policy


def test_the_allowlist_is_not_case_sensitive():
    """A case mismatch would read as "not allowed" with nothing to say why."""
    policy = OutboundPolicy(allowed=frozenset({"AAD-Person"}))
    policy.check("aad-person")
    policy.check("AAD-PERSON")


def test_an_empty_allowlist_means_outbound_is_off():
    with pytest.raises(OutboundError, match="outbound calling is off"):
        OutboundPolicy(allowed=frozenset()).check(TARGET)


# ------------------------------------------------------------- the recovery


def test_a_reservation_from_a_dead_worker_is_given_back(tmp_path):
    """Without this a process that dies mid-ring leaves the message reserved
    for ever, and the person promised an answer never gets one."""
    import os
    import time as _time

    store = PendingMessages(directory=tmp_path)
    store.park(PendingMessage(call_id="c1", text="hello", thread_id="19:t"))
    store.reserve("c1")
    held = tmp_path / "c1.answering"
    old = _time.time() - 3600
    os.utime(held, (old, old))

    assert store.recover_reservations(600.0) == 1
    assert [m.call_id for m in store.waiting()] == ["c1"]


def test_a_fresh_reservation_is_left_alone(tmp_path):
    store = PendingMessages(directory=tmp_path)
    store.park(PendingMessage(call_id="c1", text="hello"))
    store.reserve("c1")
    assert store.recover_reservations(600.0) == 0


def test_a_record_from_an_older_build_still_loads():
    """A restart must not lose what somebody was told."""
    old = {"callId": "c1", "text": "hello", "threadId": "19:t", "createdMs": 5}
    held = PendingMessage.from_json(old)
    assert held.text == "hello"
    assert held.tenant_id == "" and held.target == "" and held.attempts == 0
