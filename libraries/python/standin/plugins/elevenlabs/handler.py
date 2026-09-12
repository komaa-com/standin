# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The relay: one Microsoft Teams call on one side, one ElevenLabs agent on the other.

This is a plain :class:`~standin.CallHandler`. Everything that is the same for
every framework - the socket StandIn dials, the handshake, capacity, the frame
loop, the watchdogs - belongs to :class:`~standin.CallServer`, so what is left
here is only what ElevenLabs specifically needs:

* open a conversation when the call starts, personalised with the caller
* forward the caller's voice, and play the agent's voice back
* stop talking the instant ElevenLabs reports the caller interrupted
* answer the agent's client tools with what a Microsoft Teams call can actually do

The client tools it answers are the SDK's call capabilities, so this agent can
do what every other provider's can. ElevenLabs declares client tools on the
agent rather than over the wire, so :func:`client_tools` prints the exact
declarations to paste in rather than leaving you to retype them.

Two of them mean something slightly different here, and the difference is the
reason they are not the SDK's implementations. ``look`` uploads the frame into
the conversation for the model to read directly, which PERSISTS the caller's
screen with ElevenLabs and is why it takes a recorded call. ``show_image`` also
accepts inline bytes, which no other provider offers.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from typing import Any

from standin import CallSession
from standin.calltools import tool_schemas
from standin.fetch import fetch_public_image
from standin.log import logger
from standin.startup import StartupBuffer
from standin.vision import DISPLAY_IMAGE_MIME_TYPES, MAX_IMAGE_BYTES, VideoFrame
from standin.vision_tools import KeyframeStore

from .agent import AgentSocket, build_conversation_init
from .config import ElevenLabsConfig

__all__ = ["ElevenLabsHandler", "client_tools"]

#: Bound on what a model may put in an emotion or a caption. These reach the
#: avatar tile, and an unbounded string from a model is an unbounded string
#: from whoever is steering it.
_MAX_EMOTION_CHARS = 40
_MAX_CAPTION_CHARS = 200
_MAX_MODE_CHARS = 20

#: How long to wait for an agent-supplied URL to produce an image.
_IMAGE_FETCH_TIMEOUT_MS = 10_000


def client_tools() -> list[dict[str, Any]]:
    """The client tools to declare on the ElevenLabs agent.

    ElevenLabs configures tools on the agent, not over the wire, so there is
    nothing this plugin can send. What it can do is hand you the declarations
    verbatim::

        python -c "import json, standin.plugins.elevenlabs as e; \
            print(json.dumps(e.client_tools(), indent=2))"

    The names and descriptions are the SDK's, so this agent is told about the
    same capabilities as every other provider's. ``show_image`` is widened here
    because ElevenLabs is the one provider that can take the bytes inline.
    """
    tools = [dict(spec) for spec in tool_schemas("flat")]
    for tool in tools:
        if tool["name"] != "show_image":
            continue
        tool["description"] = (
            f"{tool['description']} You can give the image inline instead, "
            "as 'dataBase64' with its 'mime'."
        )
        parameters = {**tool["parameters"], "properties": {**tool["parameters"]["properties"]}}
        parameters["properties"].update(
            {
                "dataBase64": {
                    "type": "string",
                    "description": "The image itself, base64 encoded. An alternative to 'url'.",
                },
                "mime": {
                    "type": "string",
                    "description": f"Required with dataBase64: one of "
                    f"{', '.join(DISPLAY_IMAGE_MIME_TYPES)}.",
                },
                "durationMs": {
                    "type": "integer",
                    "description": "How long to leave it up, in milliseconds.",
                },
            }
        )
        # Either form will do, so neither argument can be required.
        parameters["required"] = []
        tool["parameters"] = parameters
    return tools


#: Caller audio and context that arrive while the conversation is still opening.
#: Bounded: on a socket that never opens these would otherwise grow for the
#: length of the call.
_MAX_PENDING_AUDIO = 200
_MAX_PENDING_CONTEXT = 20


class ElevenLabsHandler:
    """One Microsoft Teams call answered by one ElevenLabs agent."""

    def __init__(self, config: ElevenLabsConfig | None = None) -> None:
        self._config = config or ElevenLabsConfig.from_env()
        self._call: CallSession | None = None
        self._agent: AgentSocket | None = None
        self._closed = False
        # Holds BOTH the caller's first words and the first context, so
        # neither is lost while the provider is still connecting.
        self._pending = StartupBuffer()
        # Audio already in flight when an interruption lands is audio the caller
        # must never hear: the model stopped, and playing the tail would talk
        # over the person who just interrupted.
        self._last_audio_event = 0
        self._last_interrupt_event = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        # Frames kept so a question about a slide already gone can still be
        # answered. The store keeps nothing unless the call is recorded.
        self._keyframes = KeyframeStore()

    def _recording(self) -> bool:
        """Whether the call is being recorded, straight off the session.

        The server keeps this current from ``session.start`` and every later
        ``recording.status``, so there is nothing to re-derive here.
        """
        return self._call is not None and self._call.recording_active

    # ---- the SDK seam -----------------------------------------------------

    async def on_start(self, session: CallSession) -> None:
        """Open the conversation, personalised with who is calling."""
        self._call = session
        caller = session.start.caller

        try:
            agent = await AgentSocket.connect(
                self._config, self._on_agent_message, self._on_agent_close
            )
        except Exception as err:
            logger.error("standin: could not open the ElevenLabs conversation: %s", err)
            await session.end("agent-unavailable")
            return

        # The call can end DURING the connect above. Keeping a socket opened
        # after teardown would leave a live, billed conversation with nothing on
        # the other end of it.
        if self._closed:
            await agent.aclose()
            return
        self._agent = agent

        agent.send_conversation_init(
            build_conversation_init(
                dynamic_variables={
                    "caller_name": caller.display_name or "caller",
                    "tenant_id": caller.tenant_id or "unknown-tenant",
                    "call_direction": session.start.direction,
                },
                first_message=self._config.first_message,
                environment=self._config.environment,
                # Per-person memory, and only when the person is actually
                # identified. Guests and anonymous callers get no user_id at
                # all, so two of them can never share one identity.
                user_id=caller.aad_id,
                branch_id=self._config.agent_branch_id,
            )
        )

        # Whatever arrived while the socket was opening. The "there are N people
        # here, stay quiet" signal usually lands exactly in this window.
        released = await self._pending.release(
            send_audio=lambda pcm: agent.send_audio_chunk(base64.b64encode(pcm).decode("ascii")),
            send_context=agent.send_contextual_update,
        )
        if any(self._pending.dropped):
            logger.info(
                "standin: the caller outran the agent starting up; some early input was dropped"
            )
        del released

    async def on_caller_audio(self, pcm: bytes) -> None:
        """The caller's voice, straight through. Both sides are PCM16 at 16 kHz."""
        chunk = base64.b64encode(pcm).decode("ascii")
        agent = self._agent
        if agent is None or not agent.is_open:
            self._pending.audio(pcm)
            return
        agent.send_audio_chunk(chunk)

    async def on_video_frame(self, frame: VideoFrame) -> None:
        """Keep a short history, so ``look_back`` has something to look at.

        The store itself refuses to keep anything unless the call is being
        recorded, which is the same promise ``look`` makes.
        """
        call = self._call
        if call is not None:
            self._keyframes.offer(frame, call.recording_active)

    async def on_context(self, text: str) -> None:
        """Participant counts, key presses, recording changes.

        Sent as a contextual update, which ElevenLabs delivers WITHOUT
        interrupting the agent mid-sentence.
        """
        agent = self._agent
        if agent is None or not agent.is_open:
            self._pending.context(text)
            return
        agent.send_contextual_update(text)

    async def on_goodbye(self, text: str) -> None:
        """StandIn is ending the call and wants this line spoken first.

        Delivered as a user turn, which interrupts whatever the agent was
        saying. The SDK has already flushed the buffered playback, so the line
        is not queued behind the answer it interrupted.
        """
        agent = self._agent
        if agent is None or not agent.is_open:
            return
        self._last_interrupt_event = max(self._last_interrupt_event, self._last_audio_event)
        agent.send_user_message(f'[system: the call is ending. Say a brief goodbye now: "{text}"]')

    async def aclose(self, reason: str) -> None:
        """Close the conversation. Always runs exactly once."""
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        agent, self._agent = self._agent, None
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.aclose()

    # ---- what ElevenLabs sends us -----------------------------------------

    async def _on_agent_message(self, message: dict[str, Any]) -> None:
        kind = message["type"]
        if kind == "audio":
            await self._on_agent_audio(message)
        elif kind == "interruption":
            await self._on_interruption(message)
        elif kind == "ping":
            event = message.get("ping_event")
            if isinstance(event, dict) and isinstance(event.get("event_id"), (int, float)):
                agent = self._agent
                if agent is not None:
                    agent.send_pong(int(event["event_id"]))
        elif kind == "client_tool_call":
            call = message.get("client_tool_call")
            if (
                isinstance(call, dict)
                and isinstance(call.get("tool_name"), str)
                and isinstance(call.get("tool_call_id"), str)
            ):
                await self._on_tool_call(call)
        elif kind in ("user_transcript", "agent_response"):
            # Gated twice. A transcript in your logs is a recording of the
            # caller, so it takes both an explicit opt-in AND the call actually
            # being recorded.
            if self._config.log_transcripts and self._recording():
                logger.info("standin: elevenlabs %s", kind)

    async def _on_agent_audio(self, message: dict[str, Any]) -> None:
        event = message.get("audio_event")
        if (
            not isinstance(event, dict)
            or not isinstance(event.get("event_id"), (int, float))
            or not isinstance(event.get("audio_base_64"), str)
        ):
            return
        event_id = int(event["event_id"])
        self._last_audio_event = max(self._last_audio_event, event_id)
        if event_id <= self._last_interrupt_event:
            # Audio the model generated before it was interrupted, arriving
            # after. Playing it is exactly the thing barge-in exists to stop.
            return
        call = self._call
        if call is None:
            return
        try:
            pcm = base64.b64decode(event["audio_base_64"], validate=True)
        except Exception:
            logger.warning("standin: ElevenLabs sent unusable audio; dropping the frame")
            return
        await call.send_audio(pcm)

    async def _on_interruption(self, message: dict[str, Any]) -> None:
        event = message.get("interruption_event")
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), (int, float)):
            return
        self._last_interrupt_event = max(self._last_interrupt_event, int(event["event_id"]))
        call = self._call
        if call is not None:
            # The only lever that un-sends audio StandIn already has buffered.
            # Without it the model stops but the bot keeps talking.
            await call.cancel_playback()

    async def _on_agent_close(self, code: int, reason: str) -> None:
        logger.info("standin: ElevenLabs conversation closed (%s %s)", code, reason)
        call = self._call
        if call is not None and not self._closed:
            await call.end("agent-disconnected")

    # ---- the agent's client tools -----------------------------------------

    async def _on_tool_call(self, call: dict[str, Any]) -> None:
        params = call.get("parameters")
        if not isinstance(params, dict):
            params = {}
        name = call["tool_name"]
        tool_call_id = call["tool_call_id"]

        if name == "end_call":
            self._reply(tool_call_id, "the call is ending")
            if self._call is not None:
                await self._call.end("agent-ended-call")
        elif name == "express":
            await self._on_express(tool_call_id, params)
        elif name == "show_image":
            self._spawn(self._on_show_image(tool_call_id, params))
        elif name == "look":
            self._spawn(self._on_look(tool_call_id, params))
        elif name == "look_back":
            self._spawn(self._on_look_back(tool_call_id, params))
        else:
            self._reply(tool_call_id, f'"{name}" is not a tool this plugin answers', True)

    async def _on_express(self, tool_call_id: str, params: dict[str, Any]) -> None:
        emotion = params.get("emotion")
        emotion = emotion.strip() if isinstance(emotion, str) else ""
        if not emotion:
            self._reply(tool_call_id, "express needs an 'emotion'", True)
            return
        if self._call is not None:
            try:
                await self._call.express(emotion)
            except ValueError as err:
                # The bound lives where the message is built, so every plugin
                # gets it. Read it back rather than raising at the model.
                self._reply(tool_call_id, str(err), True)
                return
        self._reply(tool_call_id, f"expressing {emotion}")

    async def _on_show_image(self, tool_call_id: str, params: dict[str, Any]) -> None:
        """Put a picture on the bot's tile, from inline bytes or a URL."""
        call = self._call
        if call is None:
            return
        try:
            data_base64 = params.get("dataBase64")
            data_base64 = data_base64 if isinstance(data_base64, str) and data_base64 else None
            mime = params.get("mime") if isinstance(params.get("mime"), str) else None
            url = params.get("url") if isinstance(params.get("url"), str) else None

            if data_base64 is None and url:
                # The URL came from the model, which is steered by whoever is on
                # the call, so it is untrusted input. fetch_public_image refuses
                # private and link-local hosts and re-checks the address at
                # connect time, which is what stops a crafted prompt reaching
                # cloud metadata.
                image, mime = await fetch_public_image(
                    url, MAX_IMAGE_BYTES, _IMAGE_FETCH_TIMEOUT_MS
                )
                data_base64 = base64.b64encode(image).decode("ascii")

            if not data_base64 or mime not in DISPLAY_IMAGE_MIME_TYPES:
                raise ValueError(
                    "show_image needs {dataBase64, mime} or {url}, "
                    f"and the image must be one of {', '.join(DISPLAY_IMAGE_MIME_TYPES)}"
                )

            duration = params.get("durationMs")
            mode = params.get("mode")
            caption = params.get("caption")
            await call.display_image(
                data_base64,
                mime,
                duration_ms=int(duration) if isinstance(duration, (int, float)) else None,
                mode=mode[:_MAX_MODE_CHARS] if isinstance(mode, str) else None,
                caption=caption[:_MAX_CAPTION_CHARS] if isinstance(caption, str) else None,
            )
            self._reply(tool_call_id, "the caller can see the image")
        except Exception as err:
            # Tell the agent the truth. Claiming success would leave it talking
            # about a picture the caller never saw.
            self._reply(tool_call_id, f"show_image failed: {err}", True)

    async def _on_look(self, tool_call_id: str, params: dict[str, Any]) -> None:
        """Look at what the caller is showing.

        The frame is uploaded to the conversation, which PERSISTS the caller's
        screen or face with ElevenLabs. That is why it takes the call being
        recorded: the caller has been told the call is being kept, and this is
        part of what is kept.
        """
        call, agent = self._call, self._agent
        if call is None or agent is None:
            return
        source = params.get("source")
        frame = call.latest_video_frame(source if source in ("camera", "screenshare") else None)
        if frame is None:
            self._reply(
                tool_call_id,
                "there is nothing to look at: the caller is not sharing their camera or screen",
                True,
            )
            return
        if not self._recording():
            self._reply(
                tool_call_id,
                "cannot look: the Microsoft Teams call is not being recorded, and looking would "
                "store the caller's screen with a third party",
                True,
            )
            return

        await self._attach(tool_call_id, frame, params, "look")

    async def _on_look_back(self, tool_call_id: str, params: dict[str, Any]) -> None:
        """Look at something the caller has already moved past.

        Only possible on a recorded call, because that is the only time frames
        are kept at all.
        """
        if self._call is None or self._agent is None:
            return
        frames = self._keyframes.recent()
        if not frames:
            self._reply(
                tool_call_id,
                "I can only look back at earlier screens while the call is being recorded, "
                "and nothing has been kept"
                if not self._recording()
                else "nothing has been shown on this call yet",
                True,
            )
            return
        await self._attach(tool_call_id, frames[-1], params, "look_back")

    async def _attach(
        self, tool_call_id: str, frame: VideoFrame, params: dict[str, Any], tool: str
    ) -> None:
        """Upload one frame into the conversation and tell the agent to read it."""
        agent = self._agent
        if agent is None:
            return
        question = params.get("question")
        question = question.strip() if isinstance(question, str) else ""
        question = question or "Describe what is visible."
        who = frame.participant_name or (
            "a participant" if frame.source == "screenshare" else "the caller"
        )
        described = (
            f"screen shared by {who}" if frame.source == "screenshare" else f"camera of {who}"
        )
        try:
            await agent.attach_image(
                frame.data, frame.mime, f"[live call frame: {described}] {question}"
            )
            self._reply(tool_call_id, "the frame is attached; answer from what you can see")
        except Exception as err:
            self._reply(tool_call_id, f"{tool} failed: {err}", True)

    # ---- plumbing ---------------------------------------------------------

    def _reply(self, tool_call_id: str, result: str, is_error: bool = False) -> None:
        agent = self._agent
        if agent is not None:
            agent.send_tool_result(tool_call_id, result, is_error)

    def _spawn(self, coro: Any) -> None:
        """Run a tool off the read loop, keeping a reference so it survives.

        A tool that fetches a URL or uploads a frame takes seconds. Awaiting it
        inline would stall every other message from the agent, including the
        interruption that stops it talking.
        """
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
