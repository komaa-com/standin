# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""One Cartesia Line agent stream, as a socket.

This plugin is a transport and nothing more, and that is Cartesia's design
rather than a shortcut here: the agent itself - the model, the tools, the
conversation logic - is YOUR code, deployed on Cartesia's platform. There is no
client-side tool channel on this wire, so unlike the other providers there are
no call capabilities to declare. What the agent can do about the call, it does
through its own code.

Audio is pinned to ``pcm_16000``, which is exactly what StandIn speaks, so
nothing here resamples anything.

The long-lived API key never touches the agent socket. It mints a short-lived
token over HTTPS and that token authenticates the socket, so a key cannot leak
from a per-call connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Callable
from typing import Any

import aiohttp

from standin.log import logger

from .config import CartesiaConfig

__all__ = ["AgentSocket", "build_start", "mint_access_token"]

#: The one wire rate: StandIn's PCM16 at 16 kHz is Line's pcm_16000.
WIRE_SAMPLE_RATE_HZ = 16_000
_INPUT_FORMAT = "pcm_16000"

#: Line drops an idle connection after about three minutes, and a caller who
#: is simply listening sends nothing. A protocol ping keeps it alive.
_KEEPALIVE_INTERVAL_S = 60.0

_REST_TIMEOUT_S = 10.0

#: Per-call tokens are minted with the maximum lifetime the API allows.
_ACCESS_TOKEN_TTL_S = 3600

_MAX_SEND_BUFFER_BYTES = 1024 * 1024


def build_start(
    stream_id: str, config: CartesiaConfig, caller: dict[str, str], call_id: str
) -> dict[str, Any]:
    """The ``start`` event, sent once as the first message on the socket.

    Caller details ALWAYS ride ``metadata``, which the Line agent's own code
    receives. They are appended to the system prompt only when you set one
    here, because a plugin must never silently rewrite the prompt you
    wrote on Cartesia's side.
    """
    stream_config: dict[str, Any] = {"input_format": _INPUT_FORMAT}
    if config.voice_id:
        stream_config["voice_id"] = config.voice_id

    agent: dict[str, Any] = {}
    if config.introduction:
        agent["introduction"] = config.introduction
    if config.system_prompt:
        agent["system_prompt"] = (
            f"{config.system_prompt.strip()}\n\n"
            f"Call context: you are speaking with {caller['caller_name']} "
            f"(tenant: {caller['tenant_id']}) on an {caller['direction']} Microsoft Teams call."
        )

    start: dict[str, Any] = {
        "event": "start",
        "stream_id": stream_id,
        "config": stream_config,
        "metadata": {
            "from": "msteams",
            "callId": call_id,
            "callerName": caller["caller_name"],
            "tenantId": caller["tenant_id"],
            "direction": caller["direction"],
        },
    }
    if agent:
        start["agent"] = agent
    return start


async def mint_access_token(config: CartesiaConfig) -> str:
    """Mint a short-lived token for one call.

    A fresh client session per call is deliberate: sessions are bound to an
    event loop and calls last minutes, so one extra handshake per call is
    cheaper than managing a shared session across loops.
    """
    timeout = aiohttp.ClientTimeout(total=_REST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"https://{config.api_host}/access-token",
            json={"grants": {"agent": True}, "expires_in": _ACCESS_TOKEN_TTL_S},
            headers={
                "authorization": f"Bearer {config.api_key}",
                "cartesia-version": config.version,
            },
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"minting a Cartesia token failed: HTTP {response.status}")
            body = await response.json()
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("the Cartesia token response carried no token")
    return token


class AgentSocket:
    """The socket to one Cartesia Line agent stream."""

    def __init__(self, config: CartesiaConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self.stream_id = uuid.uuid4().hex
        self._pending_bytes = 0

    @classmethod
    async def connect(
        cls,
        config: CartesiaConfig,
        on_message: Callable[[dict[str, Any]], Any],
        on_audio: Callable[[str], Any],
        on_close: Callable[[int, str], Any],
    ) -> AgentSocket:
        """Open the stream, retrying once on a transient failure."""
        socket = cls(config)
        try:
            await socket._open()
        except Exception as err:
            logger.warning("standin: Cartesia connect failed (%s); retrying once", err)
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
        token = await mint_access_token(self._config)
        url = f"wss://{self._config.api_host}/agents/stream/{self._config.agent_id}"
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(
                url,
                headers={
                    "authorization": f"Bearer {token}",
                    "cartesia-version": self._config.version,
                },
                max_msg_size=16 * 1024 * 1024,
            ),
            timeout=_REST_TIMEOUT_S,
        )

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
            with contextlib.suppress(Exception):
                await ws.ping()

    async def _read_loop(
        self,
        on_message: Callable[[dict[str, Any]], Any],
        on_audio: Callable[[str], Any],
        on_close: Callable[[int, str], Any],
    ) -> None:
        ws = self._ws
        assert ws is not None
        code, reason = 1000, ""
        try:
            while True:
                frame = await ws.receive()
                if frame.type is aiohttp.WSMsgType.TEXT:
                    try:
                        message = json.loads(frame.data)
                    except ValueError:
                        logger.warning("standin: Cartesia sent an unparseable frame; dropping")
                        continue
                    if not isinstance(message, dict):
                        continue
                    try:
                        # The hot path first: agent audio goes straight out
                        # without touching the rest of the dispatch.
                        if message.get("event") == "media_output":
                            payload = (message.get("media") or {}).get("payload")
                            if isinstance(payload, str) and payload:
                                await _call(on_audio, payload)
                            continue
                        await _call(on_message, message)
                    except Exception:
                        logger.exception(
                            "standin: handling Cartesia %s failed",
                            message.get("event") or "event",
                        )
                elif frame.type is aiohttp.WSMsgType.BINARY:
                    # This wire is JSON only: audio rides base64 inside events.
                    logger.warning("standin: Cartesia sent an unexpected binary frame; dropping")
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
            logger.warning("standin: Cartesia socket failed: %s", err)
            reason = "transport-failure"
        finally:
            code = ws.close_code or code
            await self._dispose()
            await _call(on_close, code, reason)

    @property
    def is_open(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _send(self, message: dict[str, Any], droppable: bool = False) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        payload = json.dumps(message)
        if droppable and self._pending_bytes > _MAX_SEND_BUFFER_BYTES:
            return  # stale caller audio is worth nothing by the time it lands
        self._pending_bytes += len(payload)
        asyncio.ensure_future(self._send_now(ws, payload))

    async def _send_now(self, ws: aiohttp.ClientWebSocketResponse, payload: str) -> None:
        try:
            await ws.send_str(payload)
        except Exception:
            pass  # the read loop reports the close
        finally:
            self._pending_bytes -= len(payload)

    def send_start(self, start: dict[str, Any]) -> None:
        self._send(start)

    def send_audio_chunk(self, pcm_base64: str) -> None:
        """The caller's voice, as a media_input event."""
        self._send(
            {
                "event": "media_input",
                "stream_id": self.stream_id,
                "media": {"payload": pcm_base64},
            },
            droppable=True,
        )

    def send_dtmf(self, digit: str) -> None:
        self._send({"event": "dtmf", "stream_id": self.stream_id, "digit": digit})

    def send_custom(self, metadata: dict[str, Any]) -> None:
        """Hand arbitrary context to the Line agent's own code.

        The only channel this wire has for call context, which is why
        participant counts and recording changes arrive here rather than as a
        prompt update.
        """
        self._send({"event": "custom", "stream_id": self.stream_id, "metadata": metadata})

    async def aclose(self) -> None:
        """Close the stream and stop reading. Safe to call twice."""
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
