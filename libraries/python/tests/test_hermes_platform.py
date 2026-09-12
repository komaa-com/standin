# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The bridge as a platform a host connects, disconnects and sends through.

Python only: it is host glue, and the host is Python.
"""

from __future__ import annotations

import pytest

from standin.delivery import LiveCalls
from standin.plugins.hermes_agent.platform import MicrosoftTeamsPlatform, register_platform

pytestmark = pytest.mark.unit


class Rejecting:
    """A host with no platform API at all, which older ones are."""

    def register_platform(self, *args, **kwargs):
        raise AttributeError("no such thing here")


class Accepting:
    def __init__(self) -> None:
        self.registered: list[tuple[str, object]] = []

    def register_platform(self, name: str, platform: object) -> None:
        self.registered.append((name, platform))


async def test_no_secret_is_off_rather_than_broken(monkeypatch):
    """A host that gets an exception marks the whole plugin broken, when what
    actually happened is that this one platform is not configured."""
    monkeypatch.delenv("STANDIN_SECRET", raising=False)
    platform = MicrosoftTeamsPlatform()
    assert await platform.connect() is False
    assert platform.connected is False
    # And disconnecting something that never connected is not an error either.
    await platform.disconnect()


async def test_a_port_already_taken_is_reported_not_raised(monkeypatch, caplog):
    """Two owners of one port silently split inbound calls, and the split is
    invisible until half of them go unanswered."""
    monkeypatch.setenv("STANDIN_SECRET", "x" * 32)

    from standin.plugins.hermes_agent import platform as module

    class Bound:
        host = "0.0.0.0"
        port = 9442

        async def start(self) -> None:
            raise OSError(48, "Address already in use")

    monkeypatch.setattr(module, "CallServer", lambda **kwargs: Bound())
    platform = MicrosoftTeamsPlatform()
    with caplog.at_level("ERROR"):
        assert await platform.connect() is False
    assert "9442" in caplog.text
    assert "serve" in caplog.text


async def test_connecting_twice_binds_once(monkeypatch):
    monkeypatch.setenv("STANDIN_SECRET", "x" * 32)
    from standin.plugins.hermes_agent import platform as module

    started: list[int] = []

    class Listener:
        host = "127.0.0.1"
        port = 0

        async def start(self) -> None:
            started.append(1)

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(module, "CallServer", lambda **kwargs: Listener())
    platform = MicrosoftTeamsPlatform()
    assert await platform.connect() is True
    assert await platform.connect() is True
    assert started == [1]
    await platform.disconnect()
    assert platform.connected is False
    # A host reload calls these in whatever order it likes.
    await platform.disconnect()


async def test_a_message_for_somebody_on_a_call_is_spoken_not_rung(monkeypatch):
    monkeypatch.setenv("STANDIN_SECRET", "x" * 32)
    from standin.plugins.hermes_agent import platform as module

    class Listener:
        host = "127.0.0.1"
        port = 0

        async def start(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

    class Speaker:
        def __init__(self) -> None:
            self.said: list[str] = []

        async def say(self, text: str) -> None:
            self.said.append(text)

    monkeypatch.setattr(module, "CallServer", lambda **kwargs: Listener())
    live = LiveCalls()
    speaker = Speaker()
    live.register(speaker, call_id="call-1", thread_id="19:thread")
    monkeypatch.setattr(module, "LIVE_CALLS", live)

    platform = MicrosoftTeamsPlatform()
    platform._live = live
    assert await platform.connect() is True
    result = await platform.send("dana", "Your build finished.", {"thread_id": "19:thread"})
    assert result["ok"] is True
    assert result["mode"] == "live-call"
    assert speaker.said == ["Your build finished."]
    await platform.disconnect()


async def test_sending_before_connecting_is_a_sentence():
    result = await MicrosoftTeamsPlatform().send("dana", "hello")
    assert result["ok"] is False
    assert "not connected" in result["error"]


def test_a_voice_leg_is_always_reported_as_a_direct_conversation():
    """It is 1:1 by construction even when the meeting it belongs to has other
    people in it, and a group the host cannot address renders as broken."""
    assert MicrosoftTeamsPlatform().get_chat_info("anything") == {
        "name": "Teams call",
        "type": "dm",
    }


def test_an_older_host_without_platforms_is_told_not_crashed():
    """A plugin that raises at registration takes the whole host down at startup
    over a capability the operator may never use."""
    assert register_platform(Rejecting(), MicrosoftTeamsPlatform()) is False
    host = Accepting()
    assert register_platform(host, MicrosoftTeamsPlatform()) is True
    assert host.registered[0][0] == "msteams"


def test_the_admission_policy_reaches_the_hosts_own_authorization(monkeypatch):
    """The host's authorization cannot see the plugin's config block, so a
    caller admitted to the CALL was refused at the agent-turn layer: the call
    connects and the agent declines to do anything."""
    from standin.plugins.hermes_agent.config import PluginConfig
    from standin.plugins.hermes_agent.platform import _mirror_admission

    monkeypatch.delenv("MSTEAMS_BRIDGE_ALLOWLIST", raising=False)
    monkeypatch.delenv("MSTEAMS_BRIDGE_ALLOW_ALL", raising=False)
    _mirror_admission(PluginConfig(allowlist=("aad-1", "aad-2")))
    import os

    assert os.environ["MSTEAMS_BRIDGE_ALLOWLIST"] == "aad-1,aad-2"

    # An operator who set it by hand keeps what they set.
    monkeypatch.setenv("MSTEAMS_BRIDGE_ALLOWLIST", "only-me")
    _mirror_admission(PluginConfig(allowlist=("aad-1",)))
    assert os.environ["MSTEAMS_BRIDGE_ALLOWLIST"] == "only-me"
