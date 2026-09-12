# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The smallest plugin that answers a real Microsoft Teams call: an echo.

It is called echo because that is what it does - call the number, talk, and hear
yourself back. That makes it the thing to run before you suspect your own agent:
if the echo answers, your secret, your tunnel and your StandIn identity are all
correct, and the fault is further in.

It needs no extra and no framework, because it is already in the base install::

    pip install standin-sdk
    STANDIN_SECRET=... python -m standin.plugins.echo

Copy this directory to start a new plugin: replace
:meth:`EchoHandler.on_caller_audio` with your framework's agent loop, and
everything else in this file is the shape every plugin keeps.
"""

from __future__ import annotations

import asyncio

from standin import CallServer, CallSession

__all__ = ["EchoHandler", "serve"]


class EchoHandler:
    """One instance per call. Sends the caller's own voice back to them.

    A handler implements only what it needs: the SDK treats every callback as
    optional, so this class defines three of the five and the other two are
    no-ops. Nothing inherits from anything.
    """

    def __init__(self) -> None:
        self._call: CallSession | None = None

    async def on_start(self, session: CallSession) -> None:
        self._call = session
        print(f"call {session.call_id} from {session.start.caller.display_name or 'unknown'}")

    async def on_caller_audio(self, pcm: bytes) -> None:
        # Your agent goes here. PCM16, 16 kHz, mono, little-endian - the same
        # format send_audio expects back.
        if self._call is not None:
            await self._call.send_audio(pcm)

    async def on_goodbye(self, text: str) -> None:
        # StandIn is ending the call and wants this line spoken first. A real
        # plugin would interrupt the agent and say it.
        print(f"goodbye: {text}")


async def serve() -> None:
    """Answer Microsoft Teams calls until interrupted."""
    server = CallServer(handler_factory=EchoHandler)
    await server.start()
    try:
        await asyncio.Event().wait()  # run until cancelled
    finally:
        await server.aclose()


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
