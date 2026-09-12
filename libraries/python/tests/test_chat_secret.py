# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The chat lane signs with its own key.

A managed deployment issues a second key for chat. Keeping the lanes separate is
the point: the voice lane signs a WebSocket handshake and the chat lane signs an
HTTP body, so a key that can forge one cannot forge the other.
"""

from __future__ import annotations

import pytest

from standin import ChatChannel, StandInError

pytestmark = pytest.mark.unit


async def _respond(message) -> str:
    return "ok"


def test_the_chat_key_is_preferred_over_the_voice_key(monkeypatch):
    monkeypatch.setenv("STANDIN_SECRET", "voice-key")
    monkeypatch.setenv("STANDIN_CHAT_SECRET", "chat-key")
    channel = ChatChannel(respond=_respond)
    assert channel._secret == "chat-key"


def test_one_key_still_works_for_both_lanes(monkeypatch):
    """The simple deployment should not have to set two variables."""
    monkeypatch.setenv("STANDIN_SECRET", "one-key")
    monkeypatch.delenv("STANDIN_CHAT_SECRET", raising=False)
    channel = ChatChannel(respond=_respond)
    assert channel._secret == "one-key"


def test_an_explicit_secret_wins(monkeypatch):
    monkeypatch.setenv("STANDIN_SECRET", "voice-key")
    monkeypatch.setenv("STANDIN_CHAT_SECRET", "chat-key")
    channel = ChatChannel(respond=_respond, secret="explicit")
    assert channel._secret == "explicit"


def test_no_key_at_all_names_both_variables(monkeypatch):
    monkeypatch.delenv("STANDIN_SECRET", raising=False)
    monkeypatch.delenv("STANDIN_CHAT_SECRET", raising=False)
    with pytest.raises(StandInError, match="STANDIN_CHAT_SECRET"):
        ChatChannel(respond=_respond)
