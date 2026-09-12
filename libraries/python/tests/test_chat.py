# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Who this bot may send call content to.

A one-to-one call carries no thread that can be posted into, so the only honest
source for "where do I answer this person" is a message they actually sent. That
memory decides who receives a recording of somebody's conversation, so every rule
here is a rule about NOT sending.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest

import standin.chat as chat_module
from standin.chat import (
    CHAT_FALLBACK_WINDOW_MS,
    ChatChannel,
    InboundMessage,
    PersonalChat,
    PersonalChats,
)

pytestmark = pytest.mark.unit

_NOW = 1_700_000_000_000


def _message(
    *,
    scope: str = "personal",
    tenant_id: str = "tenant-1",
    conversation_id: str = "a:1abcdef",
    aad_id: str | None = "caller-1",
    activity_id: str = "activity-1",
) -> InboundMessage:
    return InboundMessage(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        activity_id=activity_id,
        scope=scope,
        text="hello",
        sender_name="Dana",
        sender_aad_id=aad_id,
    )


@pytest.fixture
def chats(monkeypatch) -> PersonalChats:
    monkeypatch.setattr(chat_module, "_clock", lambda: _NOW)
    return PersonalChats()


# ------------------------------------------------------- remembering a sender


def test_a_personal_message_is_remembered(chats: PersonalChats):
    chats.remember(_message())
    found = chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW)
    assert found == PersonalChat(
        conversation_id="a:1abcdef",
        tenant_id="tenant-1",
        aad_id="caller-1",
        display_name="Dana",
        at_ms=_NOW,
    )


@pytest.mark.parametrize("scope", ["groupChat", "channel", "meeting", "something-new"])
def test_a_group_conversation_is_never_remembered(chats: PersonalChats, scope: str):
    """Without this, an @mention in a team channel makes that channel somebody's
    personal chat and puts their private escalation in front of the team."""
    chats.remember(_message(scope=scope, conversation_id="19:team@thread.tacv2"))
    assert chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW) is None


def test_scope_decides_it_and_not_the_conversation_id(chats: PersonalChats):
    """A prefix test inverts the rule: a personal chat with a bot is addressed
    "a:1..." while "19:..." is exactly the shape that must be excluded, so an id
    check would reject every real personal chat and admit nothing."""
    chats.remember(_message(conversation_id="19:looks-like-a-thread"))
    found = chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW)
    assert found is not None
    assert found.conversation_id == "19:looks-like-a-thread"


@pytest.mark.parametrize("blank", ["conversation_id", "tenant_id"])
def test_a_chat_that_cannot_be_addressed_is_not_remembered(chats: PersonalChats, blank: str):
    """A record missing either half addresses nothing. Keeping it would turn a
    call with no target into a call whose post fails, which reads as a chat that
    refused the minutes."""
    chats.remember(_message(**{blank: "   "}))
    assert chats._by_sender == {}
    assert chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW) is None


def test_the_newest_message_from_a_sender_wins(chats: PersonalChats):
    chats.remember(_message(conversation_id="a:old"))
    chats.remember(_message(conversation_id="a:new"))
    found = chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW)
    assert found is not None
    assert found.conversation_id == "a:new"


# -------------------------------------------------------- the narrowing rules


def test_another_tenants_chat_is_never_returned(chats: PersonalChats):
    """Addressing a conversation in an organisation this worker is not bound
    to."""
    chats.remember(_message(tenant_id="tenant-other"))
    assert chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW) is None


def test_a_stale_chat_is_no_longer_a_target(chats: PersonalChats):
    """A record older than the window is a guess about who is on the phone."""
    chats.remember(_message())
    just_inside = _NOW + CHAT_FALLBACK_WINDOW_MS
    assert (
        chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=just_inside)
        is not None
    )
    assert (
        chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=just_inside + 1)
        is None
    )


def test_a_different_caller_gets_nobody_elses_chat(chats: PersonalChats):
    chats.remember(_message(aad_id="caller-1"))
    assert chats.for_caller(caller_aad_id="caller-2", tenant_id="tenant-1", now_ms=_NOW) is None


def test_a_caller_nobody_can_name_gets_no_chat(chats: PersonalChats):
    """Every anonymous caller would otherwise collapse onto whoever chatted
    last."""
    chats.remember(_message())
    assert chats.for_caller(caller_aad_id=None, tenant_id="tenant-1", now_ms=_NOW) is None
    assert chats.for_caller(caller_aad_id="   ", tenant_id="tenant-1", now_ms=_NOW) is None


def test_a_single_operator_install_may_relax_the_identity_rule(chats: PersonalChats, caplog):
    """Default off, and warned by name at every use."""
    chats.remember(_message())
    with caplog.at_level("WARNING"):
        found = chats.for_caller(
            caller_aad_id=None,
            tenant_id="tenant-1",
            now_ms=_NOW,
            allow_unidentified=True,
        )
    assert found is not None
    assert "allow_unidentified" in caplog.text


def test_relaxing_the_identity_rule_relaxes_nothing_else(chats: PersonalChats):
    """The tenant and the recency window still hold, and a group conversation was
    never recorded in the first place."""
    chats.remember(_message())
    assert (
        chats.for_caller(
            caller_aad_id=None, tenant_id="tenant-other", now_ms=_NOW, allow_unidentified=True
        )
        is None
    )
    assert (
        chats.for_caller(
            caller_aad_id=None,
            tenant_id="tenant-1",
            now_ms=_NOW + CHAT_FALLBACK_WINDOW_MS + 1,
            allow_unidentified=True,
        )
        is None
    )


def test_a_tenant_has_to_be_named(chats: PersonalChats):
    chats.remember(_message())
    assert chats.for_caller(caller_aad_id="caller-1", tenant_id="", now_ms=_NOW) is None


def test_the_memory_is_bounded(chats: PersonalChats):
    """A tenant with many users must not grow this without bound inside a worker
    that is also carrying live audio."""
    for index in range(chat_module._MAX_REMEMBERED_SENDERS + 20):
        chats.remember(_message(aad_id=f"caller-{index}", activity_id=f"activity-{index}"))
    assert len(chats._by_sender) == chat_module._MAX_REMEMBERED_SENDERS
    # The oldest went first, and the newest is still there.
    assert chats.for_caller(caller_aad_id="caller-0", tenant_id="tenant-1", now_ms=_NOW) is None
    assert (
        chats.for_caller(caller_aad_id="caller-500", tenant_id="tenant-1", now_ms=_NOW) is not None
    )


# ------------------------------------------------------------- the chat lane


class _FakeSocket:
    """Enough of a WebSocket for the read loop: a few messages, then closed."""

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = bodies
        self.closed = False
        self.sent: list[str] = []

    def __aiter__(self):
        async def messages():
            for body in self._bodies:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=body)

        return messages()

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


async def _drain(channel: ChatChannel) -> None:
    """Let the turns the read loop started actually finish."""
    await asyncio.wait_for(asyncio.gather(*channel._tasks, return_exceptions=True), timeout=5)


def _inbound_body(**overrides) -> str:
    body = {
        "tenantId": "tenant-1",
        "conversationId": "a:1abcdef",
        "activityId": "activity-1",
        "scope": "personal",
        "text": "hello",
        "sender": {"displayName": "Dana", "aadObjectId": "caller-1"},
    }
    body.update(overrides)
    return json.dumps(body)


async def test_the_chat_lane_remembers_everyone_who_messages_it(monkeypatch):
    """The call lane can only answer a caller back if the chat lane wrote down
    where to answer them."""
    monkeypatch.setattr(chat_module, "_clock", lambda: _NOW)
    chats = PersonalChats()

    async def respond(message: InboundMessage) -> str:
        return "ok"

    channel = ChatChannel(respond=respond, secret="k", chats=chats)
    channel._ws = _FakeSocket([_inbound_body()])
    await channel._run()
    await _drain(channel)

    found = chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW)
    assert found is not None
    assert found.conversation_id == "a:1abcdef"


async def test_a_redelivery_does_not_make_an_old_message_look_new(monkeypatch):
    """StandIn is at-least-once. The second copy must not start a second turn,
    and it must not move the recency window either: that window is evidence
    about who is on the phone now, and a repeat of this morning's message is
    not."""
    ticks = [_NOW, _NOW + 60_000]
    monkeypatch.setattr(chat_module, "_clock", lambda: ticks.pop(0) if ticks else _NOW)
    chats = PersonalChats()
    turns: list[str] = []

    async def respond(message: InboundMessage) -> str:
        turns.append(message.activity_id)
        return "ok"

    channel = ChatChannel(respond=respond, secret="k", chats=chats)
    channel._ws = _FakeSocket([_inbound_body(), _inbound_body()])
    await channel._run()
    await _drain(channel)

    assert turns == ["activity-1"]
    found = chats.for_caller(caller_aad_id="caller-1", tenant_id="tenant-1", now_ms=_NOW)
    assert found is not None
    assert found.at_ms == _NOW


async def test_a_channel_with_no_memory_still_answers(monkeypatch):
    """The memory is opt-in: a worker that never places calls has no use for
    it."""

    async def respond(message: InboundMessage) -> str:
        return "ok"

    channel = ChatChannel(respond=respond, secret="k")
    channel._ws = _FakeSocket([_inbound_body()])
    await channel._run()
    await _drain(channel)
    assert channel._chats is None
    assert any("ok" in sent for sent in channel._ws.sent)


def test_the_memory_of_who_messaged_is_bounded():
    """A worker serving many tenants must not grow a record it never forgets."""
    from standin.chat import _MAX_REMEMBERED_SENDERS, PersonalChats

    chats = PersonalChats()
    for n in range(_MAX_REMEMBERED_SENDERS + 50):
        chats.remember(
            InboundMessage(
                tenant_id=f"tenant-{n}",
                conversation_id=f"a:1{n}",
                activity_id=f"act-{n}",
                text="hello",
                scope="personal",
                sender_aad_id=f"aad-{n}",
            ),
            at_ms=1_000 + n,
        )
    assert len(chats._by_sender) == _MAX_REMEMBERED_SENDERS
    # The oldest went, the newest stayed.
    assert chats.for_caller(caller_aad_id="aad-0", tenant_id="tenant-0", now_ms=2_000) is None
    last = _MAX_REMEMBERED_SENDERS + 49
    assert (
        chats.for_caller(caller_aad_id=f"aad-{last}", tenant_id=f"tenant-{last}", now_ms=2_000)
        is not None
    )


def test_a_sender_with_no_directory_id_is_still_remembered_and_still_bounded():
    """An anonymous sender used to go into a second index that nothing trimmed."""
    from standin.chat import PersonalChats

    chats = PersonalChats()
    chats.remember(
        InboundMessage(
            tenant_id="tenant-1",
            conversation_id="a:11",
            activity_id="act-1",
            text="hello",
            scope="personal",
        ),
        at_ms=1_000,
    )
    # Nobody is named, so no identified caller may be answered there.
    assert chats.for_caller(caller_aad_id="aad-1", tenant_id="tenant-1", now_ms=1_100) is None
    # The single-operator escape hatch still reaches it, and still warns.
    found = chats.for_caller(
        caller_aad_id=None, tenant_id="tenant-1", now_ms=1_100, allow_unidentified=True
    )
    assert found is not None
    assert found.conversation_id == "a:11"
