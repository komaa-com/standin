# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""One ElevenLabs agent conversation, as a socket.

Deliberately thin: this file opens the socket, parses frames, and offers send
helpers. What any of it MEANS to a Microsoft Teams call lives in
:mod:`~standin.plugins.elevenlabs.handler`, so the wire and the relay can
be read, and tested, one at a time.

The contract that makes this plugin simple is the audio format. An
ElevenLabs agent configured for ``pcm_16000`` in both directions speaks exactly
what StandIn speaks, so nothing here resamples anything. An agent configured
for something else is a misconfiguration that would produce a whole call of
garbled audio, so it is caught at the metadata frame and refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

import aiohttp

from standin.log import logger

from .config import ElevenLabsConfig

__all__ = ["AgentSocket", "build_conversation_init"]

#: Bound on the REST calls and on the socket open, so a hung ElevenLabs API
#: cannot wedge ``on_start`` with a caller already on the line.
_REST_TIMEOUT_S = 10.0

#: ElevenLabs pings roughly every ten seconds, so a minute of total silence
#: means the peer is gone without having sent a close frame. Surfacing it here
#: ends the call rather than relaying into the void until the SDK's own idle
#: watchdog notices.
_RECEIVE_TIMEOUT_S = 60.0

#: Outbound send-buffer ceiling. A stalled agent socket must not accumulate an
#: unbounded queue of caller audio; past this, realtime audio is dropped while
#: control messages still go.
_MAX_SEND_BUFFER_BYTES = 1024 * 1024

#: The only audio format this plugin speaks, in both directions. It is
#: also exactly what StandIn sends and expects, which is why no resampling
#: happens anywhere in this plugin.
_REQUIRED_FORMAT = "pcm_16000"

_EXT_FOR_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


def build_conversation_init(
    dynamic_variables: dict[str, str],
    first_message: str | None = None,
    environment: str | None = None,
    user_id: str | None = None,
    branch_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``conversation_initiation_client_data`` that opens a call.

    ``user_id`` is a stable per-person id, which is what ElevenLabs keys
    analytics and memory on. Pass the caller's directory id when there is one
    and NOTHING when there is not: a shared default would make every anonymous
    caller the same person, and one caller would read another's conversation
    memory.
    """
    message: dict[str, Any] = {
        "type": "conversation_initiation_client_data",
        "dynamic_variables": dynamic_variables,
    }
    # Overrides are rejected unless the agent's own security settings allow
    # them, so send one only when it was actually configured.
    if first_message:
        message["conversation_config_override"] = {"agent": {"first_message": first_message}}
    if environment:
        message["environment"] = environment
    if user_id:
        message["user_id"] = user_id
    if branch_id:
        message["branch_id"] = branch_id
    return message


async def get_signed_url(config: ElevenLabsConfig) -> str:
    """Mint a short-lived signed URL for a private agent.

    Expires in about fifteen minutes, so this is called per call and the result
    is never cached.
    """
    params = {"agent_id": config.agent_id}
    if config.environment:
        params["environment"] = config.environment
    url = f"https://{config.host}/v1/convai/conversation/get-signed-url?{urlencode(params)}"
    timeout = aiohttp.ClientTimeout(total=_REST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"xi-api-key": config.api_key}) as response:
            if response.status != 200:
                raise RuntimeError(f"get-signed-url failed: HTTP {response.status}")
            body = await response.json()
    signed = body.get("signed_url")
    if not signed:
        raise RuntimeError("get-signed-url returned no signed_url")
    return str(signed)


async def upload_conversation_file(
    config: ElevenLabsConfig, conversation_id: str, data: bytes, mime: str
) -> str:
    """Upload one frame to the live conversation and return its file id.

    This PERSISTS the caller's screen or face with ElevenLabs, which is why the
    handler gates it on the Microsoft Teams call being recorded rather than
    calling it whenever a model asks to look.
    """
    ext = _EXT_FOR_MIME.get(mime.lower())
    if not ext:
        raise ValueError(f"unsupported image type for upload: {mime}")
    quoted = quote(conversation_id, safe="")
    url = f"https://{config.host}/v1/convai/conversations/{quoted}/files"
    form = aiohttp.FormData()
    form.add_field("file", data, filename=f"frame.{ext}", content_type=mime)
    timeout = aiohttp.ClientTimeout(total=_REST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=form, headers={"xi-api-key": config.api_key}) as response:
            if response.status != 200:
                raise RuntimeError(f"file upload failed: HTTP {response.status}")
            body = await response.json()
    file_id = body.get("file_id")
    if not file_id:
        raise RuntimeError("file upload returned no file_id")
    return str(file_id)


class AgentSocket:
    """The socket to one ElevenLabs agent conversation."""

    def __init__(self, config: ElevenLabsConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._read_task: asyncio.Task[None] | None = None
        self.conversation_id: str | None = None
        self._pending_bytes = 0
        self._dropped = 0
        self._last_drop_warning = 0.0

    @classmethod
    async def connect(
        cls,
        config: ElevenLabsConfig,
        on_message: Callable[[dict[str, Any]], Any],
        on_close: Callable[[int, str], Any],
    ) -> AgentSocket:
        """Open the agent socket, retrying once with a fresh signed URL.

        Signed URLs are short-lived and minting one can fail transiently. One
        retry costs a quarter of a second and saves a caller from hearing a
        dropped call because a URL expired between minting and connecting.
        """
        socket = cls(config)
        try:
            await socket._open()
        except Exception as err:
            logger.warning(
                "standin: ElevenLabs connect failed (%s); retrying with a fresh URL", err
            )
            await socket._dispose()
            await asyncio.sleep(0.25)
            try:
                await socket._open()
            except Exception:
                await socket._dispose()
                raise
        socket._read_task = asyncio.ensure_future(socket._read_loop(on_message, on_close))
        return socket

    async def _open(self) -> None:
        signed_url = await get_signed_url(self._config)
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        # Bound the open as tightly as the REST calls: a blackholed connect or a
        # stalled TLS upgrade must not hold on_start open forever.
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(
                signed_url,
                max_msg_size=16 * 1024 * 1024,
                receive_timeout=_RECEIVE_TIMEOUT_S,
            ),
            timeout=_REST_TIMEOUT_S,
        )

    async def _dispose(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        if self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
            self._session = None

    async def _read_loop(
        self,
        on_message: Callable[[dict[str, Any]], Any],
        on_close: Callable[[int, str], Any],
    ) -> None:
        ws = self._ws
        assert ws is not None
        code, reason = 1000, ""
        try:
            while True:
                frame = await ws.receive()
                if frame.type is aiohttp.WSMsgType.TEXT:
                    message = _parse(frame.data)
                    if message is None:
                        continue
                    if message["type"] == "conversation_initiation_metadata":
                        if not self._read_metadata(message):
                            reason = "audio-format-mismatch"
                            break
                    try:
                        result = on_message(message)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        # A handler error must never escape and kill the relay
                        # for the rest of the call.
                        logger.exception("standin: handling ElevenLabs %s failed", message["type"])
                elif frame.type is aiohttp.WSMsgType.CLOSE:
                    code = frame.data or code
                    reason = frame.extra or ""
                    break
                elif frame.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    break
                elif frame.type is aiohttp.WSMsgType.ERROR:
                    reason = "socket-error"
                    break
        except (TimeoutError, asyncio.TimeoutError):
            reason = "receive-timeout"
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: ElevenLabs socket failed: %s", err)
            reason = "transport-failure"
        finally:
            code = ws.close_code or code
            await self._dispose()
            result = on_close(code, reason)
            if asyncio.iscoroutine(result):
                await result

    def _read_metadata(self, message: dict[str, Any]) -> bool:
        meta = message.get("conversation_initiation_metadata_event") or {}
        if isinstance(meta.get("conversation_id"), str):
            self.conversation_id = meta["conversation_id"]
        out_format = meta.get("agent_output_audio_format")
        in_format = meta.get("user_input_audio_format")
        wrong = next(
            (f for f in (out_format, in_format) if f and f != _REQUIRED_FORMAT),
            None,
        )
        if wrong:
            # Refuse rather than log and carry on. The alternative is a call
            # that stays "up" for its whole duration while the caller hears
            # noise, and an operator with nothing to go on.
            logger.error(
                "standin: ElevenLabs agent audio is %s, expected %s both ways; ending the call",
                wrong,
                _REQUIRED_FORMAT,
            )
            return False
        return True

    @property
    def is_open(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _send(self, message: dict[str, Any], droppable: bool = False) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        payload = json.dumps(message)
        if droppable and self._pending_bytes > _MAX_SEND_BUFFER_BYTES:
            # Realtime audio is the only droppable thing here. Control messages
            # are tiny and load-bearing, so they always queue.
            self._dropped += 1
            now = time.monotonic()
            if now - self._last_drop_warning >= 1:
                logger.warning(
                    "standin: ElevenLabs send backpressure, dropped %d chunk(s)", self._dropped
                )
                self._last_drop_warning = now
                self._dropped = 0
            return
        self._pending_bytes += len(payload)
        asyncio.ensure_future(self._send_now(ws, payload))

    async def _send_now(self, ws: aiohttp.ClientWebSocketResponse, payload: str) -> None:
        try:
            await ws.send_str(payload)
        except Exception:
            pass  # the socket died mid-send; the read loop reports the close
        finally:
            self._pending_bytes -= len(payload)

    def send_audio_chunk(self, pcm_base64: str) -> None:
        """Caller audio, forwarded verbatim. This message carries no ``type``."""
        self._send({"user_audio_chunk": pcm_base64}, droppable=True)

    def send_conversation_init(self, init: dict[str, Any]) -> None:
        self._send(init)

    def send_pong(self, event_id: int) -> None:
        self._send({"type": "pong", "event_id": event_id})

    def send_contextual_update(self, text: str) -> None:
        """Background context that must NOT interrupt the agent mid-sentence."""
        self._send({"type": "contextual_update", "text": text})

    def send_user_message(self, text: str) -> None:
        """An interrupting user turn, which is how a goodbye gets spoken."""
        self._send({"type": "user_message", "text": text})

    def send_tool_result(self, tool_call_id: str, result: str, is_error: bool = False) -> None:
        self._send(
            {
                "type": "client_tool_result",
                "tool_call_id": tool_call_id,
                "result": result,
                "is_error": is_error,
            }
        )

    async def attach_image(self, data: bytes, mime: str, question: str) -> None:
        """Upload a frame and inject it as a multimodal user turn."""
        if not self.conversation_id:
            raise RuntimeError("the conversation has not started yet")
        file_id = await upload_conversation_file(self._config, self.conversation_id, data, mime)
        self._send(
            {
                "type": "multimodal_message",
                "text": {"type": "user_message", "text": question},
                "file": {"type": "file_input", "file_id": file_id},
            }
        )

    async def aclose(self) -> None:
        """Close the conversation and stop reading. Safe to call twice."""
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(BaseException):
                await self._read_task
            self._read_task = None
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close(code=1000, message=b"session-end")
        await self._dispose()


def _parse(raw: str) -> dict[str, Any] | None:
    """Read one frame, dropping anything that is not a typed object."""
    try:
        message = json.loads(raw)
    except ValueError:
        logger.warning("standin: ElevenLabs sent an unparseable frame; dropping")
        return None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        return None
    return message
