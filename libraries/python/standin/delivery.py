# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Getting a line of text to a person, whether or not they are already on a call.

Something outside the call wants to reach somebody: a scheduled job, a chat
message, a host handing off a task. There are two ways that can land, and which
one is right depends entirely on whether a call to that person is up right now.

If one is, the line should be spoken into it. Ringing somebody who is mid
sentence with you is the rudest possible way to tell them something.

If one is not, the call has to be placed, and the line parked so it is said the
moment they answer. :mod:`standin.outbound` already has every piece of that:
who may be rung, how often, and the durable parking. What it has never had is a
way to reach a call that is ALREADY up, so every plugin that wanted this wrote
its own registry, and the ones that exist disagree with each other about
identity and about what to do when the live path fails.

So the registry is here, and the policy that chooses between the two lanes is
here with it::

    live = LiveCalls()
    delivery = VoiceDelivery(live, caller=caller, policy=policy, pending=pending)

    # in the handler, for the life of the call
    live.register(speaker, call_id=session.call_id, thread_id=session.start.thread_id)

    # from anywhere else
    result = await delivery.deliver("Your build finished.", target=user_id)

Nothing in this module imports a provider or a host.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .log import logger
from .outbound import (
    OutboundCaller,
    OutboundError,
    OutboundPolicy,
    PendingMessage,
    PendingMessages,
)

__all__ = [
    "TENANT_ENV",
    "Delivery",
    "LiveCalls",
    "LiveSpeaker",
    "VoiceDelivery",
]

#: Which organisation a placed call belongs to. Operator configuration only.
TENANT_ENV = "STANDIN_TENANT_ID"


@runtime_checkable
class LiveSpeaker(Protocol):
    """Something that can say a line on a call that is already up.

    Implemented by the plugin, because only it knows how to make its provider
    speak without stepping on whatever the agent was already saying.
    """

    async def say(self, text: str) -> None: ...


class LiveCalls:
    """Which calls are up, and how to speak into them.

    A call is filed under its own id and, when it has one, under the Microsoft
    Teams conversation it belongs to. Both, because a delivery addressed by
    conversation would otherwise miss a call that is only filed by call id, and
    the agent would place a second call to somebody already on the line with it.
    """

    def __init__(self) -> None:
        self._speakers: dict[str, LiveSpeaker] = {}

    def register(self, speaker: LiveSpeaker, call_id: str, thread_id: str = "") -> None:
        if call_id.strip():
            self._speakers[call_id.strip()] = speaker
        if thread_id.strip():
            self._speakers[thread_id.strip()] = speaker

    def unregister(self, speaker: LiveSpeaker, call_id: str, thread_id: str = "") -> None:
        """Remove this speaker's keys, and only this speaker's.

        The identity check is the point. A second call on the same thread can
        start before the first one's teardown runs, and a blind delete would
        then wipe the live call's entry: every later delivery for that thread
        would ring a fresh call, and the person would hear a second ring
        instead of an answer.
        """
        for key in (call_id.strip(), thread_id.strip()):
            if key and self._speakers.get(key) is speaker:
                del self._speakers[key]

    def find(self, *keys: str) -> LiveSpeaker | None:
        """The first live call any of these keys names. Conversation first."""
        for key in keys:
            found = self._speakers.get(key.strip()) if key else None
            if found is not None:
                return found
        return None

    def __len__(self) -> int:
        return len(self._speakers)


@dataclass(frozen=True)
class Delivery:
    """What happened to one line of text."""

    ok: bool
    mode: str = ""
    """``"live-call"`` when it was spoken into a call that was already up, or
    ``"call-back"`` when a call was placed and the line parked for the answer.
    Both strings are part of this API."""

    call_id: str = ""
    """The call that was placed, on the call-back path."""

    error: str = ""
    """One sentence, safe to read out loud. Empty when ``ok``."""


#: Places the call. Normally :meth:`standin.outbound.OutboundCaller.place_call`.
PlaceCall = Callable[[str, str], Awaitable[object]]


@dataclass
class VoiceDelivery:
    """Speak into a live call if there is one, otherwise ring back.

    :meth:`deliver` never raises. Every refusal comes back as a sentence,
    because the thing reading the result is either a host that will mark the
    whole platform failed on an exception, or a model that will say it out loud.
    """

    live: LiveCalls
    caller: OutboundCaller | None = None
    policy: OutboundPolicy = field(default_factory=OutboundPolicy)
    pending: PendingMessages | None = None
    tenant_id: str = ""
    """The organisation to place calls into.

    Operator configuration only: the constructor argument, falling back to
    ``STANDIN_TENANT_ID``. Never the message, the metadata, the model or the
    caller, because a model steered by whoever is talking must not be able to
    choose which organisation gets dialled.
    """

    requested_by: str = ""
    """Directory id recorded against a parked message, for the audit trail."""

    def __post_init__(self) -> None:
        if not self.tenant_id:
            self.tenant_id = os.environ.get(TENANT_ENV, "").strip()

    async def deliver(self, text: str, target: str = "", thread_id: str = "") -> Delivery:
        """Say this line to that person, by whichever lane can reach them."""
        line = (text or "").strip()
        if not line:
            # Checked before the registry is even consulted: an empty message
            # must never place a real phone call to a real person.
            return Delivery(ok=False, error="there was nothing to say")

        speaker = self.live.find(thread_id, target)
        if speaker is not None:
            try:
                await speaker.say(line)
            except Exception as err:
                # Falls through to the call-back rather than stopping here. A
                # wedged provider socket would otherwise swallow the message
                # with nobody told. The trade is deliberate: a half-spoken line
                # can be repeated by the call-back, and a repeat beats silence.
                logger.warning(
                    "standin: could not speak into the live call, ringing back instead: %s", err
                )
            else:
                return Delivery(ok=True, mode="live-call")

        return await self._call_back(line, target, thread_id)

    async def _call_back(self, line: str, target: str, thread_id: str) -> Delivery:
        if self.caller is None:
            return Delivery(ok=False, error="no outbound caller is configured on this deployment")
        who = (target or "").strip()
        try:
            self.policy.check(who)
        except OutboundError as err:
            return Delivery(ok=False, error=str(err))
        if not self.tenant_id:
            return Delivery(
                ok=False,
                error=f"no tenant is configured: set {TENANT_ENV} to place calls",
            )

        try:
            placed = await self.caller.place_call(who, self.tenant_id)
        except Exception as err:
            return Delivery(ok=False, error=f"could not place the call: {err}")
        call_id = str(getattr(placed, "call_id", "") or "").strip()
        if not call_id:
            return Delivery(ok=False, error="the call was not placed")

        # Recorded only now. Counting the attempt instead would let a broken
        # worker burn the hourly budget on calls that never rang anybody, and
        # the next real delivery would be refused for an hour because of it.
        self.policy.record()

        if self.pending is not None:
            # Parked before this returns. The answering leg is a different call
            # and can be answered before a later park would have run, and then
            # the person picks up to silence.
            #
            # Guarded because parking writes to disk, and a full or read-only
            # one would otherwise raise out of a method whose whole contract is
            # that it does not. The call HAS been placed by now, so the honest
            # answer names what was lost rather than pretending nothing rang.
            try:
                self._park(
                    PendingMessage(
                        call_id=call_id,
                        text=line,
                        thread_id=thread_id.strip(),
                        requested_by=self.requested_by,
                        tenant_id=self.tenant_id,
                        target=who,
                    )
                )
            except Exception as err:
                logger.warning("standin: the call was placed but the line was not parked: %s", err)
                return Delivery(
                    ok=False,
                    mode="call-back",
                    call_id=call_id,
                    error="the call was placed, but what to say could not be saved",
                )
        return Delivery(ok=True, mode="call-back", call_id=call_id)

    def _park(self, message: PendingMessage) -> None:
        assert self.pending is not None
        self.pending.park(message)
