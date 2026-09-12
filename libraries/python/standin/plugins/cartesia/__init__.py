# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Answer Microsoft Teams calls with a Cartesia Line agent.

StandIn answers the Microsoft Teams call and dials this worker. This
plugin answers that dial, opens one Line agent stream per call, and relays
the audio both ways.

Nothing to install beyond the SDK: Cartesia is reached over an ordinary
WebSocket, so this plugin adds no dependency and needs no extra::

    pip install standin-sdk

    export STANDIN_SECRET=...        # your StandIn connection secret
    export CARTESIA_API_KEY=...
    export CARTESIA_AGENT_ID=...
    python -m standin.plugins.cartesia

The agent itself is your code on Cartesia's platform, so this plugin is
transport and nothing else: there are no call capabilities to declare and no
tools to answer here. Caller details reach your agent as stream metadata, and
call context (participant counts, key presses, recording changes) arrives as
``custom`` events for your agent code to act on.

Audio is pinned to ``pcm_16000`` in both directions, which is exactly what
StandIn speaks, so nothing resamples anything.

See https://docs.komaa.com for setup.
"""

from __future__ import annotations

import asyncio

from standin import CallServer

from .agent import AgentSocket, build_start, mint_access_token
from .config import CartesiaConfig
from .handler import CartesiaHandler

__all__ = [
    "AgentSocket",
    "CartesiaConfig",
    "CartesiaHandler",
    "build_start",
    "mint_access_token",
    "serve",
]


async def serve() -> None:
    """Answer Microsoft Teams calls with Cartesia until interrupted."""
    config = CartesiaConfig.from_env()
    server = CallServer(handler_factory=lambda: CartesiaHandler(config))
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.aclose()


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
