# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""A received session.start gets its own handler startup deadline."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from standin import SIGNATURE_HEADER, TIMESTAMP_HEADER, CallServer, now_ms, sign_handshake


async def test_received_start_is_not_closed_by_the_pre_start_watchdog() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    closed = asyncio.Event()

    class Handler:
        async def on_start(self, session: object) -> None:
            entered.set()
            await release.wait()
            finished.set()

        async def aclose(self, reason: str) -> None:
            closed.set()

    server = CallServer(
        handler_factory=Handler,
        secret="local-startup-test",
        host="127.0.0.1",
        port=0,
        pre_start_timeout=0.15,
        on_start_timeout=2.0,
        audio_idle_timeout=0,
    )
    await server.start()
    try:
        assert server._runner is not None
        port = server._runner.addresses[0][1]
        timestamp = str(now_ms())
        headers = {
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign_handshake("local-startup-test", timestamp, "slow-start"),
        }
        async with (
            aiohttp.ClientSession() as http,
            http.ws_connect(
                f"http://127.0.0.1:{port}{server.ws_path}/slow-start", headers=headers
            ) as ws,
        ):
            await ws.send_str(json.dumps({"type": "session.start", "callId": "slow-start"}))
            await asyncio.wait_for(entered.wait(), 1.0)
            # Cross the frame-arrival deadline while remaining well inside the
            # separate two-second budget for the provider's startup.
            await asyncio.sleep(0.25)
            release.set()
            await asyncio.wait_for(finished.wait(), 1.0)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(closed.wait(), 0.1)
            assert server.active_calls == 1
    finally:
        release.set()
        await server.aclose()
