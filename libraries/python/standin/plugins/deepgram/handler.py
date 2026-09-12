# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The relay: one Microsoft Teams call on one side, one Deepgram Voice Agent on the other.

A plain :class:`~standin.CallHandler`. :class:`~standin.CallServer` owns
everything that is the same for every framework, so what is here is only what
Deepgram needs.

Two shapes differ from the other providers and are worth knowing:

**Context rides the prompt.** The Voice Agent API has no non-interrupting
context message, so participant counts and key presses are appended to a
bounded rolling section of the prompt and pushed with ``UpdatePrompt``. Bounded
matters: the prompt is resent in full each time.

**Functions are declared, not configured.** The call capabilities come from
:mod:`standin.calltools` and are sent in the Settings message, so there is
nothing to set up on the Deepgram side and nothing restated here: a capability
added to the SDK reaches this plugin without an edit. Add your own with
:class:`CustomTool` and they are declared the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from standin import CallSession
from standin.calltools import CallTools, tool_schemas
from standin.log import logger
from standin.startup import StartupBuffer
from standin.vision import FrameDescriber
from standin.vision_tools import VisionTools

from .agent import AgentSocket, build_prompt, build_settings
from .config import DeepgramConfig

__all__ = ["CustomTool", "DeepgramHandler", "ToolContext"]

_MAX_EMOTION_CHARS = 40
_MAX_CAPTION_CHARS = 200
_IMAGE_FETCH_TIMEOUT_MS = 10_000

#: How many context notes ride in the prompt. The whole prompt is resent on
#: every update, so this is a cost per participant change, not a one-off.
_MAX_CONTEXT_NOTES = 8

_MAX_PENDING_AUDIO = 200


@dataclass(frozen=True)
class ToolContext:
    """What a custom tool is told about the call it is running inside."""

    call: CallSession
    """The live call, so a tool can speak, show something, or hang up."""

    recording: bool
    """Whether the Microsoft Teams call is being recorded. Gate anything that
    stores what the caller said or showed on this."""


#: A custom tool's implementation. Returns the string the agent is told, which
#: it will read out or reason from. Keep it fast: the caller is waiting in
#: silence while it runs.
ToolHandler = Callable[[dict[str, Any], ToolContext], "str | Awaitable[str]"]


@dataclass(frozen=True)
class CustomTool:
    """A function of your own, which the agent calls and your code answers.

    Declared to Deepgram in the Settings message and executed in your worker,
    so it can reach anything your worker can reach. The name must not collide
    with a built-in call capability.
    """

    name: str
    description: str
    """What the model reads to decide whether to call it. This is the prompt
    for the tool, so write it for a model, not for a developer."""
    handler: ToolHandler
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )

    def schema(self) -> dict[str, Any]:
        """The Settings entry. The handler stays here and is never sent."""
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


#: The call capabilities every Deepgram session gets, declared in Settings.
#: These have no endpoint, which is what tells Deepgram to ask the client to
#: run them. The list is the SDK's, in Deepgram's shape, so every provider
#: offers the same agent and a new capability needs no edit here.
BUILT_IN_TOOLS: list[dict[str, Any]] = tool_schemas("flat")

_BUILT_IN_NAMES = frozenset(str(tool["name"]) for tool in BUILT_IN_TOOLS)


class DeepgramHandler:
    """One Microsoft Teams call answered by one Deepgram Voice Agent."""

    def __init__(
        self,
        config: DeepgramConfig | None = None,
        tools: list[CustomTool] | None = None,
        describer: FrameDescriber | None = None,
    ) -> None:
        self._config = config or DeepgramConfig.from_env()
        self._tools = {tool.name: tool for tool in tools or []}
        self._describer = describer if describer is not None else FrameDescriber.from_env()
        self._call: CallSession | None = None
        self._agent: AgentSocket | None = None
        self._closed = False
        # Holds BOTH the caller's first words and the first context, so
        # neither is lost while the provider is still connecting.
        self._pending = StartupBuffer()
        self._caller: dict[str, str] = {}
        self._notes: deque[str] = deque(maxlen=_MAX_CONTEXT_NOTES)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._vision: VisionTools | None = None
        self._call_tools: CallTools | None = None

        collisions = {t["name"] for t in BUILT_IN_TOOLS} & set(self._tools)
        if collisions:
            # Caught here rather than at the first call: a shadowed built-in is
            # a call capability that silently stops working.
            raise ValueError(f"custom tools may not shadow built-in ones: {sorted(collisions)}")

    def _recording(self) -> bool:
        """Whether the call is being recorded, straight off the session.

        The server keeps this current from ``session.start`` and every later
        ``recording.status``, so there is nothing to re-derive here.
        """
        return self._call is not None and self._call.recording_active

    # ---- the SDK seam -----------------------------------------------------

    async def on_start(self, session: CallSession) -> None:
        self._call = session
        self._vision = VisionTools(session, describer=self._describer)
        self._call_tools = CallTools(session, vision=self._vision)
        caller = session.start.caller
        self._caller = {
            "caller_name": caller.display_name or "the caller",
            "tenant_id": caller.tenant_id or "unknown-tenant",
            "direction": session.start.direction,
        }

        try:
            agent = await AgentSocket.connect(
                self._config, self._on_agent_message, self._on_agent_audio, self._on_agent_close
            )
        except Exception as err:
            logger.error("standin: could not open the Deepgram agent: %s", err)
            await session.end("agent-unavailable")
            return

        if self._closed:
            # The call ended during the connect above.
            await agent.aclose()
            return
        self._agent = agent

        functions = [*BUILT_IN_TOOLS, *(tool.schema() for tool in self._tools.values())]
        agent.send_settings(
            build_settings(
                self._config, build_prompt(self._config, self._caller, list(self._notes)), functions
            )
        )
        released = await self._pending.release(
            send_audio=agent.send_audio,
            send_context=lambda text: self._note_context(agent, text),
        )
        if any(self._pending.dropped):
            logger.info(
                "standin: the caller outran the agent starting up; some early input was dropped"
            )
        del released

    async def on_caller_audio(self, pcm: bytes) -> None:
        """The caller's voice, straight through as a binary frame."""
        agent = self._agent
        if agent is None or not agent.is_open:
            self._pending.audio(pcm)
            return
        agent.send_audio(pcm)

    async def on_video_frame(self, frame) -> None:
        """Keep a short history, so a question about a slide already gone can
        still be answered. Only kept while the call is recorded."""
        vision = self._vision
        call = self._call
        if vision is not None and call is not None:
            vision.keyframes.offer(frame, call.recording_active)

    async def on_context(self, text: str) -> None:
        """Fold context into the prompt, because this API has nowhere else to put it."""
        agent = self._agent
        if agent is None or not agent.is_open:
            # Held rather than dropped: the "there are N people here, stay quiet"
            # line and the recording change both land in this gap.
            self._pending.context(text)
            return
        self._notes.append(text)
        if agent is not None and agent.is_open:
            agent.update_prompt(build_prompt(self._config, self._caller, list(self._notes)))

    def _note_context(self, agent, text: str) -> None:
        """Fold one held context line into the prompt, once the agent exists."""
        self._notes.append(text)
        agent.update_prompt(build_prompt(self._config, self._caller, list(self._notes)))

    async def on_goodbye(self, text: str) -> None:
        """Say this line now, interrupting whatever the agent was saying."""
        agent = self._agent
        if agent is not None and agent.is_open:
            agent.inject_agent_message(text)

    async def aclose(self, reason: str) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        agent, self._agent = self._agent, None
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.aclose()

    # ---- what Deepgram sends us -------------------------------------------

    async def _on_agent_audio(self, pcm: bytes) -> None:
        """Agent audio: already PCM16 at 16 kHz, so it goes straight out."""
        call = self._call
        if call is not None and pcm:
            await call.send_audio(pcm)

    async def _on_agent_message(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "UserStartedSpeaking":
            # Deepgram has decided the caller is talking. Flushing what StandIn
            # has buffered is what actually stops the bot mid-word.
            call = self._call
            if call is not None:
                await call.cancel_playback()
        elif kind == "FunctionCallRequest":
            for function in message.get("functions") or []:
                if isinstance(function, dict):
                    self._spawn(self._run_function(function))
        elif kind == "ConversationText":
            if self._config.log_transcripts and self._recording():
                logger.info("standin: deepgram %s", message.get("role") or "turn")
        elif kind in ("Error", "Warning"):
            logger.warning(
                "standin: Deepgram %s: %s",
                kind.lower(),
                message.get("description") or message.get("code") or "no detail",
            )

    async def _on_agent_close(self, code: int, reason: str) -> None:
        logger.info("standin: the Deepgram agent closed (%s %s)", code, reason)
        call = self._call
        if call is not None and not self._closed:
            await call.end("agent-disconnected")

    # ---- the agent's functions --------------------------------------------

    async def _run_function(self, function: dict[str, Any]) -> None:
        name = function.get("name")
        call_id = function.get("id")
        if not isinstance(name, str) or not isinstance(call_id, str):
            return
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        try:
            result = await self._dispatch(name, arguments)
        except Exception as err:
            # Tell the agent the truth. A tool that silently "succeeded" leaves
            # it talking about something that never happened.
            result = f"{name} failed: {err}"
        agent = self._agent
        if agent is not None:
            agent.send_function_result(call_id, name, result)

    async def _dispatch(self, name: str, params: dict[str, Any]) -> str:
        call = self._call
        if call is None:
            return "the call is no longer active"

        call_tools = self._call_tools
        if call_tools is not None and name in _BUILT_IN_NAMES:
            # The SDK owns these, and its dispatch never raises: what comes back
            # is the sentence the agent should say.
            return await call_tools.dispatch(name, params)

        tool = self._tools.get(name)
        if tool is None:
            return f'"{name}" is not a tool this plugin answers'
        result = tool.handler(params, ToolContext(call=call, recording=self._recording()))
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)

    def _vision_tools(self) -> VisionTools:
        """The shared vision tools for this call.

        They carry the budget, the keyframes and the graceful refusals, so this
        plugin no longer keeps its own copy of any of them.
        """
        assert self._vision is not None
        return self._vision

    # ---- plumbing ---------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        """Run a function off the read loop.

        A tool that fetches a URL or asks a vision model takes seconds. Awaiting
        it inline would stall every other message from the agent, including the
        one that says the caller started speaking.
        """
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
