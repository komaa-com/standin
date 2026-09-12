# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""One Deepgram Voice Agent conversation, as a socket.

Thin on purpose: framing and send helpers only. What any of it means to a
Microsoft Teams call is in :mod:`~standin.plugins.deepgram.handler`.

Two things about this wire are worth knowing before reading the code. Audio
travels as raw BINARY frames in both directions, not base64 inside JSON, so the
hot path here is a copy rather than an encode. And the session is pinned to
``linear16`` at 16 kHz both ways, which is exactly what StandIn speaks, so
nothing in this plugin resamples anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import aiohttp

from standin.log import logger

from .config import DeepgramConfig

__all__ = ["AgentSocket", "build_prompt", "build_settings", "synthesize"]

#: The one wire rate: StandIn's PCM16 at 16 kHz is Deepgram's linear16 at 16000.
WIRE_SAMPLE_RATE_HZ = 16_000

#: The Voice Agent socket idles out when no audio is flowing, which happens
#: whenever the caller is simply listening. Cheap to send, and sent for the
#: whole call.
_KEEPALIVE_INTERVAL_S = 8.0

_REST_TIMEOUT_S = 10.0

#: How long to wait for the server's Welcome. Settings may not be sent before it.
_WELCOME_TIMEOUT_S = 10.0

_MAX_SEND_BUFFER_BYTES = 1024 * 1024


def build_prompt(
    config: DeepgramConfig, caller: dict[str, str], notes: list[str] | None = None
) -> str:
    """Assemble the agent prompt: your instructions, who is calling, and what
    has happened on the call so far.

    The live notes are here rather than in their own message because the Voice
    Agent API has no non-interrupting context channel. Context rides an updated
    prompt instead, which is why the handler keeps that list bounded.
    """
    lines = [
        config.instructions,
        "",
        f"Call context: you are speaking with {caller['caller_name']} "
        f"(tenant: {caller['tenant_id']}) on an {caller['direction']} call.",
    ]
    if notes:
        lines += ["", "Live call context (most recent last):", *(f"- {note}" for note in notes)]
    return "\n".join(lines)


def build_settings(
    config: DeepgramConfig, prompt: str, functions: list[dict[str, Any]]
) -> dict[str, Any]:
    """The Settings message, sent once per call right after Welcome."""
    think: dict[str, Any] = {
        "provider": {"type": config.think_provider, "model": config.think_model},
        "prompt": prompt,
        "functions": functions,
    }
    if config.think_endpoint_url:
        endpoint: dict[str, Any] = {"url": config.think_endpoint_url}
        if config.think_endpoint_headers:
            endpoint["headers"] = config.think_endpoint_headers
        think["endpoint"] = endpoint

    agent: dict[str, Any] = {
        "listen": {
            "provider": {
                "type": "deepgram",
                "model": config.listen_model,
                "language": config.language,
            }
        },
        "think": think,
        "speak": {
            "provider": {
                "type": "deepgram",
                "model": config.speak_model,
                "language": config.language,
            }
        },
    }
    if config.greeting:
        agent["greeting"] = config.greeting
    return {
        "type": "Settings",
        "audio": {
            # "container" is an output-side field: every official Settings
            # example omits it on input, and sending it there is rejected.
            "input": {"encoding": "linear16", "sample_rate": WIRE_SAMPLE_RATE_HZ},
            "output": {
                "encoding": "linear16",
                "sample_rate": WIRE_SAMPLE_RATE_HZ,
                "container": "none",
            },
        },
        "agent": agent,
    }


async def synthesize(config: DeepgramConfig, model: str, text: str) -> bytes:
    """Speak one exact line through Deepgram's standalone text to speech.

    Returns raw PCM16 at 16 kHz, ready to hand to
    :meth:`~standin.CallSession.send_audio` with no conversion.
    """
    params = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": str(WIRE_SAMPLE_RATE_HZ),
        "container": "none",
    }
    url = f"https://{config.api_host}/v1/speak?{urlencode(params)}"
    timeout = aiohttp.ClientTimeout(total=_REST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, json={"text": text}, headers={"authorization": f"Token {config.api_key}"}
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Deepgram speak failed: HTTP {response.status}")
            return await response.read()


class AgentSocket:
    """The socket to one Deepgram Voice Agent conversation."""

    def __init__(self, config: DeepgramConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._pending_bytes = 0
        self._dropped = 0
        self._last_drop_warning = 0.0

    @classmethod
    async def connect(
        cls,
        config: DeepgramConfig,
        on_message: Callable[[dict[str, Any]], Any],
        on_audio: Callable[[bytes], Any],
        on_close: Callable[[int, str], Any],
    ) -> AgentSocket:
        """Open the socket and wait for Welcome. Retries once on a transient failure."""
        socket = cls(config)
        try:
            await socket._open()
        except Exception as err:
            logger.warning("standin: Deepgram connect failed (%s); retrying once", err)
            await socket._dispose()
            await asyncio.sleep(0.25)
            try:
                await socket._open()
            except Exception:
                await socket._dispose()
                raise
        socket._read_task = asyncio.ensure_future(socket._read_loop(on_message, on_audio, on_close))
        socket._keepalive_task = asyncio.ensure_future(socket._keepalive_loop())
        return socket

    async def _open(self) -> None:
        url = f"wss://{self._config.agent_host}/v1/agent/converse"
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(
                url,
                headers={"authorization": f"Token {self._config.api_key}"},
                max_msg_size=16 * 1024 * 1024,
            ),
            timeout=_REST_TIMEOUT_S,
        )
        await self._await_welcome(self._ws)

    async def _await_welcome(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        deadline = time.monotonic() + _WELCOME_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("no Welcome from Deepgram within the timeout")
            frame = await asyncio.wait_for(ws.receive(), timeout=remaining)
            if frame.type is aiohttp.WSMsgType.TEXT:
                try:
                    message = json.loads(frame.data)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "Welcome":
                    return
                if message.get("type") == "Error":
                    # A bad key or a bad config must fail HERE with the real
                    # reason, rather than being swallowed until the Welcome
                    # timeout reports something generic ten seconds later.
                    raise RuntimeError(
                        "Deepgram rejected the session: "
                        f"{message.get('code') or 'unknown'}: "
                        f"{message.get('description') or 'no description'}"
                    )
            elif frame.type is aiohttp.WSMsgType.BINARY:
                continue  # audio cannot arrive before Settings; ignore it
            elif frame.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                raise RuntimeError(f"the socket closed before Welcome ({frame.data})")
            elif frame.type is aiohttp.WSMsgType.ERROR:
                raise RuntimeError("the socket failed before Welcome")

    async def _dispose(self) -> None:
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            ws = self._ws
            if ws is None or ws.closed:
                return
            self._send({"type": "KeepAlive"})

    async def _read_loop(
        self,
        on_message: Callable[[dict[str, Any]], Any],
        on_audio: Callable[[bytes], Any],
        on_close: Callable[[int, str], Any],
    ) -> None:
        ws = self._ws
        assert ws is not None
        code, reason = 1000, ""
        try:
            while True:
                frame = await ws.receive()
                if frame.type is aiohttp.WSMsgType.BINARY:
                    await _call(on_audio, frame.data)
                elif frame.type is aiohttp.WSMsgType.TEXT:
                    try:
                        message = json.loads(frame.data)
                    except ValueError:
                        logger.warning("standin: Deepgram sent an unparseable frame; dropping")
                        continue
                    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                        continue
                    try:
                        await _call(on_message, message)
                    except Exception:
                        logger.exception("standin: handling Deepgram %s failed", message["type"])
                elif frame.type is aiohttp.WSMsgType.CLOSE:
                    code = frame.data or code
                    reason = frame.extra or ""
                    break
                elif frame.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
                elif frame.type is aiohttp.WSMsgType.ERROR:
                    reason = "socket-error"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: Deepgram socket failed: %s", err)
            reason = "transport-failure"
        finally:
            code = ws.close_code or code
            await self._dispose()
            await _call(on_close, code, reason)

    @property
    def is_open(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _send(self, message: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        payload = json.dumps(message)
        self._pending_bytes += len(payload)
        asyncio.ensure_future(self._send_text(ws, payload))

    async def _send_text(self, ws: aiohttp.ClientWebSocketResponse, payload: str) -> None:
        try:
            await ws.send_str(payload)
        except Exception:
            pass  # the read loop reports the close
        finally:
            self._pending_bytes -= len(payload)

    def send_audio(self, pcm: bytes) -> None:
        """The caller's voice, as a raw binary frame.

        Dropped rather than queued past the buffer ceiling: on a stalled socket
        the alternative is an unbounded pile of stale audio, and stale caller
        audio is worth nothing by the time it arrives.
        """
        ws = self._ws
        if ws is None or ws.closed:
            return
        if self._pending_bytes > _MAX_SEND_BUFFER_BYTES:
            self._dropped += 1
            now = time.monotonic()
            if now - self._last_drop_warning >= 1:
                logger.warning(
                    "standin: Deepgram send backpressure, dropped %d frame(s)", self._dropped
                )
                self._last_drop_warning = now
                self._dropped = 0
            return
        self._pending_bytes += len(pcm)
        asyncio.ensure_future(self._send_bytes(ws, pcm))

    async def _send_bytes(self, ws: aiohttp.ClientWebSocketResponse, pcm: bytes) -> None:
        try:
            await ws.send_bytes(pcm)
        except Exception:
            pass
        finally:
            self._pending_bytes -= len(pcm)

    def send_settings(self, settings: dict[str, Any]) -> None:
        self._send(settings)

    def update_prompt(self, prompt: str) -> None:
        """Replace the agent's prompt mid-call, which is how context arrives."""
        self._send({"type": "UpdatePrompt", "prompt": prompt})

    def inject_agent_message(self, text: str) -> None:
        """Make the agent say this, now, interrupting whatever it was saying."""
        self._send({"type": "InjectAgentMessage", "content": text})

    def send_function_result(self, function_call_id: str, name: str, content: str) -> None:
        self._send(
            {
                "type": "FunctionCallResponse",
                "id": function_call_id,
                "name": name,
                "content": content,
            }
        )

    async def aclose(self) -> None:
        """Close the conversation and stop reading. Safe to call twice."""
        for task in (self._read_task, self._keepalive_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._read_task = None
        self._keepalive_task = None
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close(code=1000, message=b"session-end")
        await self._dispose()


async def _call(fn: Callable[..., Any], *args: Any) -> None:
    result = fn(*args)
    if asyncio.iscoroutine(result):
        await result
