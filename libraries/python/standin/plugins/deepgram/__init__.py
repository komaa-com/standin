# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Answer Microsoft Teams calls with a Deepgram Voice Agent.

StandIn answers the Microsoft Teams call and dials this worker. This
plugin answers that dial, opens one Deepgram Voice Agent session per call,
and relays the audio both ways.

Nothing to install beyond the SDK: Deepgram is reached over an ordinary
WebSocket, so this plugin adds no dependency and needs no extra::

    pip install standin-sdk

    export STANDIN_SECRET=...          # your StandIn connection secret
    export DEEPGRAM_API_KEY=...
    python -m standin.plugins.deepgram

Speech to text, reasoning and speech are all configured on the session, so
there is nothing to set up on the Deepgram side. Audio is pinned to linear16 at
16 kHz in both directions, which is exactly what StandIn speaks, so nothing
resamples anything.

The agent gets four call capabilities without any configuration: ``end_call``,
``express``, ``show_image`` and ``look``. Looking needs a vision model, because
a Voice Agent hears but does not see - set ``STANDIN_VISION_API_URL`` and
``STANDIN_VISION_MODEL`` to any OpenAI-compatible endpoint that takes images,
including one you run yourself.

Add tools of your own, executed in your worker::

    from standin import CallServer
    from standin.plugins.deepgram import CustomTool, DeepgramHandler

    async def open_ticket(params, ctx):
        return f"opened ticket for {params.get('summary')}"

    tools = [CustomTool(
        name="open_ticket",
        description="Open a support ticket for the caller.",
        handler=open_ticket,
        parameters={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    )]

    server = CallServer(handler_factory=lambda: DeepgramHandler(tools=tools))
    await server.start()

See https://docs.komaa.com for setup.
"""

from __future__ import annotations

import asyncio

from standin import CallServer

from .agent import AgentSocket, build_prompt, build_settings
from .config import DeepgramConfig
from .handler import BUILT_IN_TOOLS, CustomTool, DeepgramHandler, ToolContext

__all__ = [
    "BUILT_IN_TOOLS",
    "AgentSocket",
    "CustomTool",
    "DeepgramConfig",
    "DeepgramHandler",
    "ToolContext",
    "build_prompt",
    "build_settings",
    "serve",
]


async def serve() -> None:
    """Answer Microsoft Teams calls with Deepgram until interrupted.

    The configuration is read ONCE here: a missing API key should stop the
    worker at startup, not surprise the first caller.
    """
    config = DeepgramConfig.from_env()
    server = CallServer(handler_factory=lambda: DeepgramHandler(config))
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
