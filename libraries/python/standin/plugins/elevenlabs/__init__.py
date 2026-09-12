# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Answer Microsoft Teams calls with an ElevenLabs agent.

StandIn answers the Microsoft Teams call and dials this worker. This
plugin answers that dial, opens one ElevenLabs agent conversation per
call, and relays the audio both ways.

Nothing to install beyond the SDK itself: ElevenLabs is reached over an
ordinary WebSocket, so this plugin adds no dependency and needs no extra::

    pip install standin-sdk

    export STANDIN_SECRET=...           # your StandIn connection secret
    export ELEVENLABS_API_KEY=...
    export ELEVENLABS_AGENT_ID=...
    python -m standin.plugins.elevenlabs

Configure the agent for ``pcm_16000`` audio in BOTH directions. That is exactly
what StandIn speaks, so nothing resamples anything and the latency you measure
is the model's, not the transport's. An agent set to anything else is refused at
the first frame rather than producing a whole call of garbled audio.

Give the agent the SDK's client tools and it can use the rest of the call, not
just the voice channel: hanging up, the avatar's expression, putting a picture
on the bot's tile, and looking at what the caller is sharing. ElevenLabs
declares tools on the agent rather than over the wire, so
:func:`client_tools` prints the exact declarations to paste in::

    python -c "import json, standin.plugins.elevenlabs as e; \
        print(json.dumps(e.client_tools(), indent=2))"

Drive it yourself instead, inside your own worker, by using the handler
directly::

    from standin import CallServer
    from standin.plugins.elevenlabs import ElevenLabsHandler

    server = CallServer(handler_factory=ElevenLabsHandler)
    await server.start()

See https://docs.komaa.com for setup.
"""

from __future__ import annotations

import asyncio

from standin import CallServer

from .agent import AgentSocket, build_conversation_init
from .config import ElevenLabsConfig
from .handler import ElevenLabsHandler, client_tools

__all__ = [
    "AgentSocket",
    "ElevenLabsConfig",
    "ElevenLabsHandler",
    "build_conversation_init",
    "client_tools",
    "serve",
]


async def serve() -> None:
    """Answer Microsoft Teams calls with ElevenLabs until interrupted.

    The configuration is read ONCE here rather than per call: a missing API key
    should stop the worker at startup, not surprise the first caller.
    """
    config = ElevenLabsConfig.from_env()
    server = CallServer(handler_factory=lambda: ElevenLabsHandler(config))
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
