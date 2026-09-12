# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The relay: one Microsoft Teams call on one side, one Cartesia Line agent on the other.

A plain :class:`~standin.CallHandler`, and the simplest of the provider
plugins, because Cartesia's design puts the agent's logic on their
platform rather than in your worker. There are no client tools to answer here:
what the agent can do, it does in its own code, and this side is transport.

Context has one channel on this wire, ``custom`` metadata, which the Line
agent's code receives. Participant counts, key presses and recording changes go
there.
"""

from __future__ import annotations

import base64
import contextlib
import re

from standin import CallSession
from standin.log import logger
from standin.startup import StartupBuffer

from .agent import AgentSocket, build_start
from .config import CartesiaConfig

__all__ = ["CartesiaHandler"]

_MAX_PENDING_AUDIO = 200

#: The SDK renders a key press as a finished sentence, which is what a model
#: wants. Line has a real dtmf event, so the digit is read back out of the
#: sentence the SDK generated. Safe to do because that sentence is the SDK's
#: own, fixed and covered by a conformance vector, rather than anything a
#: caller can influence.
_DTMF_SENTENCE = re.compile(r'pressed the "([^"]+)" key')


class CartesiaHandler:
    """One Microsoft Teams call answered by one Cartesia Line agent."""

    def __init__(self, config: CartesiaConfig | None = None) -> None:
        self._config = config or CartesiaConfig.from_env()
        self._call: CallSession | None = None
        self._agent: AgentSocket | None = None
        self._closed = False
        # Holds BOTH the caller's first words and the first context, so
        # neither is lost while the provider is still connecting.
        self._pending = StartupBuffer()

    async def on_start(self, session: CallSession) -> None:
        self._call = session
        caller = session.start.caller

        try:
            agent = await AgentSocket.connect(
                self._config, self._on_agent_message, self._on_agent_audio, self._on_agent_close
            )
        except Exception as err:
            logger.error("standin: could not open the Cartesia stream: %s", err)
            await session.end("agent-unavailable")
            return

        if self._closed:
            # The call ended during the connect above.
            await agent.aclose()
            return
        self._agent = agent

        agent.send_start(
            build_start(
                agent.stream_id,
                self._config,
                {
                    "caller_name": caller.display_name or "the caller",
                    "tenant_id": caller.tenant_id or "unknown-tenant",
                    "direction": session.start.direction,
                },
                session.call_id,
            )
        )
        released = await self._pending.release(
            send_audio=lambda pcm: agent.send_audio_chunk(base64.b64encode(pcm).decode("ascii")),
            send_context=lambda text: agent.send_custom({"from": "msteams", "context": text}),
        )
        if any(self._pending.dropped):
            logger.info(
                "standin: the caller outran the agent starting up; some early input was dropped"
            )
        del released

    async def on_caller_audio(self, pcm: bytes) -> None:
        chunk = base64.b64encode(pcm).decode("ascii")
        agent = self._agent
        if agent is None or not agent.is_open:
            self._pending.audio(pcm)
            return
        agent.send_audio_chunk(chunk)

    async def on_context(self, text: str) -> None:
        """Everything the call knows, handed to the agent's own code."""
        agent = self._agent
        if agent is None or not agent.is_open:
            # Held rather than dropped: the "there are N people here, stay quiet"
            # line and the recording change both land in this gap.
            self._pending.context(text)
            return
        digit = _DTMF_SENTENCE.search(text)
        if digit is not None:
            agent.send_dtmf(digit.group(1))
            return
        agent.send_custom({"from": "msteams", "context": text})

    async def on_goodbye(self, text: str) -> None:
        """There is no inject-speech channel on this wire, so the line is handed
        to the agent's code as context and it decides how to say it."""
        agent = self._agent
        if agent is not None and agent.is_open:
            agent.send_custom({"from": "msteams", "goodbye": text})

    async def aclose(self, reason: str) -> None:
        self._closed = True
        agent, self._agent = self._agent, None
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.aclose()

    # ---- what Cartesia sends us -------------------------------------------

    async def _on_agent_audio(self, payload_base64: str) -> None:
        call = self._call
        if call is None:
            return
        try:
            pcm = base64.b64decode(payload_base64, validate=True)
        except Exception:
            logger.warning("standin: Cartesia sent unusable audio; dropping the frame")
            return
        if pcm:
            await call.send_audio(pcm)

    async def _on_agent_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "clear":
            # The agent is telling us the caller barged in. Flushing what
            # StandIn has buffered is what actually stops the bot mid-word.
            call = self._call
            if call is not None:
                await call.cancel_playback()
        elif event == "end":
            call = self._call
            if call is not None and not self._closed:
                await call.end("agent-ended-call")
        elif event == "transfer_call":
            # A phone-network transfer has no meaning on a Microsoft Teams call.
            logger.info("standin: Cartesia asked for a call transfer, which this lane cannot do")

    async def _on_agent_close(self, code: int, reason: str) -> None:
        logger.info("standin: the Cartesia stream closed (%s %s)", code, reason)
        call = self._call
        if call is not None and not self._closed:
            await call.end("agent-disconnected")
