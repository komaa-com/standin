# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The OpenAI / Azure Realtime WebSocket, as a plain async object.

Provider code, not Hermes code, and not StandIn code. It deals only in the
model's native PCM 24 kHz and fires callbacks; resampling to the wire's 16 kHz,
frame alignment, the echo guard and barge-in all belong to
:mod:`~standin.plugins.hermes_agent.handler`, which is the half that knows about a
Microsoft Teams call.

It is in a plugin rather than in the SDK core because the core stays provider
neutral: this is one vendor's protocol, and a plugin for a different realtime
provider would replace this file and change nothing else.

Azure: pass a ``base_url`` of the form
``wss://<resource>.openai.azure.com/openai/realtime?api-version=...&deployment=...``
with ``api_key_header="api-key"``. OpenAI's bearer auth is the default.

Uses aiohttp's WebSocket client, which the SDK already depends on, rather than
adding a second WebSocket library to every deployment.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .config import plugin_env
from .log import logger

__all__ = ["DEFAULT_INSTRUCTIONS", "RealtimeConfig", "RealtimeSession", "realtime_config"]

DEFAULT_BASE_URL = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime"
DEFAULT_VOICE = "alloy"
DEFAULT_AZURE_API_VERSION = "2024-10-01-preview"

#: The voice-behaviour layer. Deliberately says nothing about identity: that
#: comes from the operator's SOUL.md through the Hermes boundary, so the caller
#: talks to the same assistant they know from chat.
DEFAULT_INSTRUCTIONS = (
    "You are a helpful voice assistant on a Microsoft Teams call. Keep replies "
    "brief and conversational. For anything requiring real work - lookups, "
    "actions, files, skills - delegate to the agent rather than guessing."
)

AsyncCb = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class RealtimeConfig:
    """Resolved provider settings. Built by :func:`realtime_config`."""

    api_key: str
    model: str = DEFAULT_MODEL
    voice: str = DEFAULT_VOICE
    instructions: str = DEFAULT_INSTRUCTIONS
    base_url: str = DEFAULT_BASE_URL
    #: ``"api-key"`` for Azure, ``"Authorization"`` (bearer) for OpenAI.
    api_key_header: str = "Authorization"
    vad_threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    #: Transcribe the CALLER's audio, which is what the group gate and the
    #: verbal-interrupt check read. Empty disables it - some deployments do not
    #: support the field - and both of those degrade to off when it is.
    input_transcribe_model: str = "whisper-1"
    #: Languages to handle, e.g. ``("en", "ar")``. Empty means detect and mirror
    #: whatever the caller speaks.
    languages: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        """Is there an API key? Nothing else can be checked before connecting."""
        return bool(self.api_key)


def _to_ws(url: str) -> str:
    """Accept the ``https://`` form people paste out of the Azure portal."""
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    return url


def _pick(block: dict[str, Any], key: str, env: str, default: str = "") -> str:
    value = block.get(key)
    if value is not None and str(value).strip():
        return str(value).strip()
    return plugin_env(env, "").strip() or default


def realtime_config(block: dict[str, Any] | None = None) -> RealtimeConfig:
    """Resolve the provider from the ``realtime:`` config block and environment.

    Azure is selected when ``backend: azure`` is set, an Azure endpoint is
    configured, or an explicit ``*.azure.com`` url is given; otherwise OpenAI.
    The Azure key falls back to ``AZURE_OPENAI_API_KEY`` and then
    ``AZURE_FOUNDRY_API_KEY``, so a Hermes host that already has a gateway key
    needs no second copy of it.

    Args:
        block: the ``realtime`` sub-block of the plugin's config. Read from the
            host when omitted; pass ``{}`` for environment-only.
    """
    if block is None:
        from .api import plugin_config_block

        raw = plugin_config_block().get("realtime")
        block = raw if isinstance(raw, dict) else {}

    backend = _pick(block, "backend", "MSTEAMS_BRIDGE_REALTIME_BACKEND").lower()
    explicit_url = _pick(block, "url", "MSTEAMS_BRIDGE_REALTIME_URL")
    azure_endpoint = _pick(block, "azure_endpoint", "MSTEAMS_BRIDGE_AZURE_ENDPOINT")

    def _num(key: str, env: str, default: str, cast: Any) -> Any:
        try:
            return cast(_pick(block, key, env, default))
        except ValueError:
            return cast(default)

    transcribe = _pick(
        block, "input_transcribe_model", "MSTEAMS_BRIDGE_INPUT_TRANSCRIBE_MODEL", "whisper-1"
    )
    if transcribe.lower() in ("none", "off", "disabled"):
        transcribe = ""

    raw_langs = block.get("languages")
    if isinstance(raw_langs, (list, tuple)):
        languages = tuple(str(v).strip().lower() for v in raw_langs if str(v).strip())
    else:
        # A YAML scalar ("en,fr") is honoured rather than ignored: it is the
        # natural way to write it and silently dropping it would leave the
        # assistant answering in the wrong language with no clue why.
        source = raw_langs if isinstance(raw_langs, str) else plugin_env("MSTEAMS_BRIDGE_LANGUAGES")
        languages = tuple(p.strip().lower() for p in (source or "").split(",") if p.strip())

    common = {
        "voice": _pick(block, "voice", "MSTEAMS_BRIDGE_REALTIME_VOICE", DEFAULT_VOICE),
        "instructions": _pick(
            block, "instructions", "MSTEAMS_BRIDGE_REALTIME_INSTRUCTIONS", DEFAULT_INSTRUCTIONS
        ),
        "vad_threshold": _num("vad_threshold", "MSTEAMS_BRIDGE_VAD_THRESHOLD", "0.5", float),
        "prefix_padding_ms": _num(
            "prefix_padding_ms", "MSTEAMS_BRIDGE_PREFIX_PADDING_MS", "300", int
        ),
        "silence_duration_ms": _num(
            "silence_duration_ms", "MSTEAMS_BRIDGE_SILENCE_DURATION_MS", "500", int
        ),
        "input_transcribe_model": transcribe,
        "languages": languages,
    }

    if backend == "azure" or azure_endpoint or "azure.com" in explicit_url:
        deployment = _pick(block, "azure_deployment", "MSTEAMS_BRIDGE_AZURE_DEPLOYMENT")
        api_version = _pick(
            block,
            "azure_api_version",
            "MSTEAMS_BRIDGE_AZURE_API_VERSION",
            DEFAULT_AZURE_API_VERSION,
        )
        if explicit_url:
            base_url = _to_ws(explicit_url)
        else:
            base = _to_ws(azure_endpoint.rstrip("/"))
            base_url = f"{base}/openai/realtime?api-version={api_version}&deployment={deployment}"
        return RealtimeConfig(
            api_key=_pick(block, "api_key", "MSTEAMS_BRIDGE_REALTIME_API_KEY")
            or os.getenv("AZURE_OPENAI_API_KEY", "").strip()
            or os.getenv("AZURE_FOUNDRY_API_KEY", "").strip(),
            model=deployment or DEFAULT_MODEL,
            base_url=base_url,
            api_key_header="api-key",
            **common,
        )

    return RealtimeConfig(
        api_key=_pick(block, "api_key", "MSTEAMS_BRIDGE_REALTIME_API_KEY")
        or os.getenv("OPENAI_API_KEY", "").strip(),
        model=_pick(block, "model", "MSTEAMS_BRIDGE_REALTIME_MODEL", DEFAULT_MODEL),
        base_url=explicit_url or DEFAULT_BASE_URL,
        api_key_header="Authorization",
        **common,
    )


class RealtimeSession:
    """One realtime model connection, for one call.

    Set the ``on_*`` callbacks and :attr:`tools` before :meth:`connect`. Every
    callback is awaited and best-effort: an exception inside one is logged and
    swallowed, because the receive loop must survive a handler's bad turn.
    """

    def __init__(self, config: RealtimeConfig) -> None:
        self._cfg = config
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._http: aiohttp.ClientSession | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_fired = False
        # True between response.created and response.done. Every send path that
        # would start a response is guarded on it, because the provider rejects
        # a second concurrent response and the rejection is not recoverable
        # in-band.
        self._response_active = False
        self._auto_response = True
        # In-flight tool tasks: held so they are not garbage collected mid-run,
        # cancelled at close so a long tool cannot outlive the call.
        self._tool_tasks: set[asyncio.Task[None]] = set()
        # One batch at a time, in arrival order. The next model response starts
        # only after every tool call in the preceding response has returned.
        self._tool_serial = asyncio.Lock()
        self._pending_tool_batches = 0

        #: Function tools offered to the model, in the realtime flat shape.
        self.tools: list[dict[str, Any]] = []

        #: (pcm24k: bytes) the model's voice.
        self.on_audio_delta: AsyncCb | None = None
        #: (text: str) a delta of what the model is saying.
        self.on_transcript_delta: AsyncCb | None = None
        #: (text: str) the caller's finished turn, transcribed.
        self.on_input_transcript: AsyncCb | None = None
        #: () the caller started speaking. This is the barge-in signal.
        self.on_speech_started: AsyncCb | None = None
        #: () the model finished a response.
        self.on_response_done: AsyncCb | None = None
        #: (name, call_id, args_json) the model called a tool.
        self.on_function_call: AsyncCb | None = None
        #: (error) the provider reported an error event.
        self.on_error: AsyncCb | None = None
        #: (reason: str) the socket dropped on the PROVIDER's side. Not fired by
        #: our own :meth:`close`, so the handler can tell "the model went away,
        #: end the call" from "the call ended, close the model".
        self.on_close: AsyncCb | None = None

    @property
    def response_active(self) -> bool:
        """Is the model mid-response? Read by the handler's goodbye path."""
        return self._response_active

    # ---- lifecycle -------------------------------------------------------

    async def connect(self) -> None:
        """Open the socket, configure the session, start the receive loop."""
        if self._cfg.api_key_header.lower() == "authorization":
            headers = {
                "Authorization": f"Bearer {self._cfg.api_key}",
                "OpenAI-Beta": "realtime=v1",
            }
        else:
            headers = {self._cfg.api_key_header: self._cfg.api_key}

        url = self._cfg.base_url
        if "model=" not in url and "deployment=" not in url:
            url = f"{url}{'&' if '?' in url else '?'}model={self._cfg.model}"

        self._http = aiohttp.ClientSession()
        self._ws = await self._http.ws_connect(url, headers=headers, max_msg_size=0)

        session: dict[str, Any] = {
            "modalities": ["audio", "text"],
            "instructions": self._cfg.instructions,
            "voice": self._cfg.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": self._turn_detection(True),
        }
        if self._cfg.input_transcribe_model:
            session["input_audio_transcription"] = {"model": self._cfg.input_transcribe_model}
        if self.tools:
            session["tools"] = self.tools
            session["tool_choice"] = "auto"
        await self._send({"type": "session.update", "session": session})
        self._recv_task = asyncio.create_task(self._recv_loop())
        logger.info("standin: realtime connected, model %s", self._cfg.model)

    def _turn_detection(self, create_response: bool) -> dict[str, Any]:
        return {
            "type": "server_vad",
            "threshold": self._cfg.vad_threshold,
            "prefix_padding_ms": self._cfg.prefix_padding_ms,
            "silence_duration_ms": self._cfg.silence_duration_ms,
            "create_response": create_response,
        }

    async def close(self) -> None:
        """Release the socket and everything running on it. Idempotent."""
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(BaseException):
                await self._recv_task
            self._recv_task = None
        tasks = list(self._tool_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            # Cancelled AND awaited: a consult must be fully unwound before the
            # sockets go, or it can still fire a say against a closing session.
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tool_tasks.clear()
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        http, self._http = self._http, None
        if http is not None and not http.closed:
            with contextlib.suppress(Exception):
                await http.close()

    # ---- sending ---------------------------------------------------------

    async def push_audio(self, pcm24k: bytes) -> None:
        """Append caller audio, PCM16 mono at 24 kHz, to the input buffer."""
        if self._closed or not pcm24k:
            return
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm24k).decode("ascii"),
            }
        )

    async def update_instructions(self, instructions: str) -> None:
        """Replace the session instructions mid-call.

        A language change applies to the call in progress rather than the next
        one, which is the whole point of a caller asking for it.
        """
        if self._closed or not (instructions or "").strip():
            return
        await self._send({"type": "session.update", "session": {"instructions": instructions}})

    async def set_auto_response(self, enabled: bool) -> None:
        """Turn server-VAD auto-response on or off.

        Off in a group call, so the gate decides whether to answer BEFORE any
        audio is generated. Cancelling a response that already started is a race
        the caller can hear.
        """
        if self._closed or enabled == self._auto_response:
            return
        self._auto_response = enabled
        await self._send(
            {"type": "session.update", "session": {"turn_detection": self._turn_detection(enabled)}}
        )

    async def create_response(self) -> None:
        """Ask for a spoken response. Used when auto-response is off."""
        if self._closed or self._response_active:
            return
        self._response_active = True
        await self._send({"type": "response.create"})

    async def cancel_response(self) -> None:
        """Cancel the in-flight response, if there is one.

        Guarded on :attr:`response_active` so a server-VAD ``speech_started`` on
        every utterance does not spam cancels at a model that is not speaking.
        """
        if not self._closed and self._response_active:
            self._response_active = False
            await self._send({"type": "response.cancel"})

    async def send_user_text(self, text: str, *, respond: bool = True) -> None:
        """Put a user-role text item in the conversation.

        ``respond`` is guarded on :attr:`response_active`, so this can never
        provoke "conversation already has an active response".
        """
        if self._closed or not text:
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if respond and not self._response_active:
            self._response_active = True
            await self._send({"type": "response.create"})

    async def request_say(self, instruction: str) -> None:
        """Have the model speak, in its own voice, right now.

        Used where there is no caller turn to reply to - the greeting, and the
        goodbye. NOT interrupting: if a response is already in flight this
        queues behind it, which is correct for a greeting and wrong for a
        goodbye. Use :meth:`interrupt_and_say` for anything that must be heard.
        """
        await self.send_user_text(instruction, respond=True)

    async def interrupt_and_say(self, instruction: str) -> None:
        """Cancel whatever is being said, then say this instead.

        **This method exists because of a shipped bug.** The goodbye path used
        :meth:`request_say`, whose ``respond`` is guarded on
        :attr:`response_active` - so a goodbye that arrived while the model was
        mid-answer created no response at all and was silently never spoken. It
        was the one line guaranteed to arrive mid-turn, because StandIn sends it
        precisely when it is about to end a call that is still in progress.

        Cancel first, then speak: :meth:`cancel_response` clears the latch, so
        the ``response.create`` that follows is not swallowed by the guard.
        """
        await self.cancel_response()
        await self.send_user_text(instruction, respond=True)

    async def send_function_result(self, call_id: str, output: str) -> None:
        """Return a tool result and let the model carry on speaking."""
        if self._closed:
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            }
        )
        if not self._response_active and not self._pending_tool_batches:
            self._response_active = True
            await self._send({"type": "response.create"})

    async def _send(self, obj: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            return
        await ws.send_str(json.dumps(obj))

    # ---- receiving -------------------------------------------------------

    async def _recv_loop(self) -> None:
        reason = "provider-closed"
        ws = self._ws
        if ws is None:
            return
        try:
            async for msg in ws:
                if msg.type is aiohttp.WSMsgType.ERROR:
                    reason = "provider-error"
                    break
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        break
                    continue
                try:
                    event = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    await self._dispatch(event)
        except asyncio.CancelledError:
            # Our own close() cancelled us. An intentional teardown, so do NOT
            # fire on_close - the handler is already ending the call and would
            # end it a second time with the wrong reason.
            raise
        except Exception:
            reason = "provider-error"
            logger.exception("standin: realtime receive loop failed")
        await self._notify_close(reason)

    async def _notify_close(self, reason: str) -> None:
        if self._close_fired or self._closed:
            return
        self._close_fired = True
        await self._safe(self.on_close, reason)

    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        # Both the beta and GA event names, so one plugin serves both.
        if etype in ("response.audio.delta", "response.output_audio.delta"):
            delta = event.get("delta")
            if delta:
                await self._safe(self.on_audio_delta, base64.b64decode(delta))
        elif etype in (
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        ):
            text = event.get("delta") or ""
            if text:
                await self._safe(self.on_transcript_delta, text)
        elif etype == "response.created":
            self._response_active = True
        elif etype == "input_audio_buffer.speech_started":
            await self._safe(self.on_speech_started)
        elif etype == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript") or ""
            if text:
                await self._safe(self.on_input_transcript, text)
        elif etype == "response.done":
            self._response_active = False
            response = event.get("response") or {}
            calls = [
                item
                for item in response.get("output") or []
                if isinstance(item, dict) and item.get("type") == "function_call"
            ]
            if calls:
                # A consult runs for seconds. Keep receiving audio and barge-in
                # events while the batch runs, then request one model response.
                self._spawn_tool_batch(calls)
            await self._safe(self.on_response_done)
        elif etype == "error":
            # A rejected response.create fires 'error' with NO matching
            # response.done, so the latch would stay True forever - and every
            # send path that creates a response is guarded on it, which mutes
            # the assistant for the rest of the call. Clear it here.
            self._response_active = False
            logger.warning("standin: realtime provider error: %s", event.get("error"))
            await self._safe(self.on_error, event.get("error"))

    def _spawn_tool_batch(self, calls: list[dict[str, Any]]) -> None:
        self._pending_tool_batches += 1

        async def runner() -> None:
            try:
                async with self._tool_serial:
                    for item in calls:
                        await self._safe(
                            self.on_function_call,
                            item.get("name", ""),
                            item.get("call_id", ""),
                            item.get("arguments") or "{}",
                        )
            finally:
                self._pending_tool_batches -= 1
            if not self._pending_tool_batches:
                await self.create_response()

        task = asyncio.create_task(runner())
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _safe(self, cb: AsyncCb | None, *args: Any) -> None:
        if cb is None:
            return
        try:
            await cb(*args)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: realtime callback failed")
