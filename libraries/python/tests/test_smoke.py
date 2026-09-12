# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Proving an install works without placing a real call.

The twin is ``libraries/typescript/src/smoke.test.ts``.
"""

from __future__ import annotations

import pytest

from standin.handler import CallSession
from standin.smoke import SmokeCheck, report, run_smoke

pytestmark = pytest.mark.unit


class Echo:
    async def on_start(self, session: CallSession) -> None:
        self._session = session

    async def on_caller_audio(self, pcm: bytes) -> None:
        await self._session.send_audio(pcm)


class Silent:
    async def on_start(self, session: CallSession) -> None:
        pass


async def test_it_rings_the_workers_own_handler_and_proves_audio_came_back():
    result = await run_smoke(Echo, frames=4)
    assert result.ok is True
    assert result.echo_frames > 0
    assert [check.name for check in result.checks] == ["secret", "listener", "call", "audio"]
    # The listener is on loopback, never on every interface: a verification run
    # has no business being reachable from the network.
    assert "127.0.0.1:" in result.checks[1].detail


async def test_a_run_that_proved_nothing_is_not_ok():
    result = await run_smoke(Silent, frames=3)
    assert result.ok is False
    assert result.echo_frames == 0
    assert "the caller would hear nothing" in report(result)


async def test_a_plugins_own_checks_are_advisory_unless_they_say_otherwise():
    async def advisory() -> list[SmokeCheck]:
        return [SmokeCheck("browser", False, cost="show_page would apologise", required=False)]

    async def mandatory() -> list[SmokeCheck]:
        return [SmokeCheck("model", False, cost="the agent could not answer")]

    assert (await run_smoke(Echo, frames=2, extra=advisory)).ok is True
    assert (await run_smoke(Echo, frames=2, extra=mandatory)).ok is False


async def test_a_plugin_check_that_raised_is_recorded_not_fatal():
    async def explodes() -> list[SmokeCheck]:
        raise RuntimeError("the plugin check exploded")

    result = await run_smoke(Echo, frames=2, extra=explodes)
    assert result.ok is True
    assert result.checks[-1].name == "plugin checks"


async def test_the_run_borrows_nothing_from_the_operators_configuration(monkeypatch):
    """A fixed or borrowed secret makes the run pass or fail for reasons that
    have nothing to do with the wire, and makes it unrunnable before the secret
    is set, which is exactly when people run it."""
    monkeypatch.delenv("STANDIN_SECRET", raising=False)
    result = await run_smoke(Echo, frames=2)
    assert result.ok is True
    assert result.checks[0].detail == "generated for this run"
