# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Choosing between speaking into a live call and ringing somebody back.

The twin is ``libraries/typescript/src/delivery.test.ts``.
"""

from __future__ import annotations

import pytest

from standin.delivery import Delivery, LiveCalls, VoiceDelivery
from standin.outbound import OutboundPolicy, PendingMessages, PlacedCall

pytestmark = pytest.mark.unit


class FakeSpeaker:
    def __init__(self, fail: Exception | None = None) -> None:
        self.said: list[str] = []
        self.fail = fail

    async def say(self, text: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.said.append(text)


class FakeCaller:
    def __init__(self, call_id: str = "placed-1") -> None:
        self.call_id = call_id
        self.placed: list[tuple[str, str]] = []
        self.fail: Exception | None = None

    async def place_call(self, user_object_id: str, tenant_id: str) -> PlacedCall:
        if self.fail is not None:
            raise self.fail
        self.placed.append((user_object_id, tenant_id))
        return PlacedCall(call_id=self.call_id, scenario_id="s")


def _delivery(tmp_path, live: LiveCalls | None = None, **kwargs) -> VoiceDelivery:
    return VoiceDelivery(
        live or LiveCalls(),
        caller=kwargs.pop("caller", FakeCaller()),
        policy=kwargs.pop("policy", OutboundPolicy(allowed=frozenset({"dana"}))),
        pending=kwargs.pop("pending", PendingMessages(tmp_path)),
        tenant_id=kwargs.pop("tenant_id", "tenant-1"),
        **kwargs,
    )


async def test_a_live_call_is_spoken_into_rather_than_rung(tmp_path):
    """Ringing somebody who is mid sentence with you is the rudest possible way
    to tell them something."""
    live = LiveCalls()
    speaker = FakeSpeaker()
    live.register(speaker, call_id="call-1", thread_id="19:thread")
    caller = FakeCaller()
    delivery = _delivery(tmp_path, live, caller=caller)

    result = await delivery.deliver("Your build finished.", target="dana", thread_id="19:thread")
    assert result == Delivery(ok=True, mode="live-call")
    assert speaker.said == ["Your build finished."]
    assert caller.placed == []


async def test_a_call_filed_only_by_its_own_id_is_still_found(tmp_path):
    """A delivery addressed by conversation would otherwise ring a second call
    to somebody already on the line."""
    live = LiveCalls()
    speaker = FakeSpeaker()
    live.register(speaker, call_id="dana")
    delivery = _delivery(tmp_path, live)
    assert (await delivery.deliver("hello", target="dana")).mode == "live-call"


async def test_a_second_call_on_one_thread_keeps_its_registration(tmp_path):
    """Call B for the same thread can start before call A's teardown runs. A
    blind delete would wipe B and every later delivery would ring afresh."""
    live = LiveCalls()
    first, second = FakeSpeaker(), FakeSpeaker()
    live.register(first, call_id="call-a", thread_id="19:thread")
    live.register(second, call_id="call-b", thread_id="19:thread")
    live.unregister(first, call_id="call-a", thread_id="19:thread")

    assert live.find("19:thread") is second
    delivery = _delivery(tmp_path, live)
    await delivery.deliver("hello", thread_id="19:thread")
    assert second.said == ["hello"]


async def test_a_wedged_live_call_rings_back_rather_than_swallowing_the_line(tmp_path):
    live = LiveCalls()
    live.register(FakeSpeaker(fail=RuntimeError("socket is wedged")), call_id="dana")
    caller = FakeCaller()
    delivery = _delivery(tmp_path, live, caller=caller)

    result = await delivery.deliver("Your build finished.", target="dana")
    # A half-spoken line can be repeated by the call-back. A swallowed one
    # cannot be recovered by anything.
    assert result.ok is True
    assert result.mode == "call-back"
    assert caller.placed == [("dana", "tenant-1")]


async def test_nothing_to_say_never_rings_anybody(tmp_path):
    caller = FakeCaller()
    delivery = _delivery(tmp_path, caller=caller)
    result = await delivery.deliver("   ", target="dana")
    assert result.ok is False
    assert result.error == "there was nothing to say"
    assert caller.placed == []


async def test_a_refusal_is_a_sentence_not_an_exception(tmp_path):
    """The result is read by a host that marks the platform failed on an
    exception, or by a model that says it out loud."""
    delivery = _delivery(tmp_path, policy=OutboundPolicy(allowed=frozenset()))
    result = await delivery.deliver("hello", target="dana")
    assert result.ok is False
    assert "outbound calling is off" in result.error

    delivery = _delivery(tmp_path, caller=None)
    assert "no outbound caller" in (await delivery.deliver("hello", target="dana")).error

    delivery = _delivery(tmp_path, tenant_id="")
    result = await delivery.deliver("hello", target="dana")
    assert "no tenant is configured" in result.error


async def test_the_tenant_is_never_taken_from_the_message(tmp_path, monkeypatch):
    """A model steered by whoever is talking must not choose which organisation
    gets dialled."""
    monkeypatch.setenv("STANDIN_TENANT_ID", "from-the-operator")
    delivery = VoiceDelivery(
        LiveCalls(),
        caller=FakeCaller(),
        policy=OutboundPolicy(allowed=frozenset({"dana"})),
        pending=PendingMessages(tmp_path),
    )
    result = await delivery.deliver("hello", target="dana")
    assert result.ok is True
    assert delivery.caller.placed == [("dana", "from-the-operator")]


async def test_the_hourly_budget_counts_calls_that_rang_not_attempts(tmp_path):
    """A broken worker would otherwise burn the hour on calls that never rang
    anybody, and the next real delivery is refused because of it."""
    policy = OutboundPolicy(allowed=frozenset({"dana"}), max_per_hour=1)
    caller = FakeCaller()
    caller.fail = RuntimeError("the worker is down")
    delivery = _delivery(tmp_path, caller=caller, policy=policy)

    assert (await delivery.deliver("hello", target="dana")).ok is False
    caller.fail = None
    assert (await delivery.deliver("hello", target="dana")).ok is True
    # The failed attempt did not count; the placed call did.
    assert (await delivery.deliver("hello", target="dana")).error.startswith("this agent has")


async def test_the_line_is_parked_before_the_delivery_returns(tmp_path):
    """The answering leg is a different call and can be answered before a later
    park would have run, and then the person picks up to silence."""
    pending = PendingMessages(tmp_path)
    delivery = _delivery(tmp_path, pending=pending)
    result = await delivery.deliver("Your build finished.", target="dana", thread_id="19:thread")

    parked = pending.pop(result.call_id)
    assert parked is not None
    assert parked.text == "Your build finished."
    # Carried so an unanswered call can post the answer to the chat it came
    # from instead of losing it.
    assert parked.thread_id == "19:thread"
    assert parked.tenant_id == "tenant-1"
    assert parked.target == "dana"


async def test_a_placement_with_no_call_id_is_not_reported_as_delivered(tmp_path):
    delivery = _delivery(tmp_path, caller=FakeCaller(call_id=""))
    result = await delivery.deliver("hello", target="dana")
    assert result.ok is False
    assert result.error == "the call was not placed"


async def test_a_park_that_failed_is_admitted_rather_than_reported_as_delivered(tmp_path):
    """Parking writes to disk. A full or read-only one must not raise out of a
    method whose whole contract is that it does not, and must not be reported as
    a delivery either: the call rang, and nobody will hear the line."""
    pending = PendingMessages(tmp_path)

    def refuse(message):
        raise OSError("read-only file system")

    pending.park = refuse
    delivery = _delivery(tmp_path, pending=pending)
    result = await delivery.deliver("Your build finished.", target="dana")

    assert result.ok is False
    assert result.mode == "call-back"
    assert result.call_id == "placed-1"
    assert result.error == "the call was placed, but what to say could not be saved"
