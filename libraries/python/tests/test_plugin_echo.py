# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The echo plugin: the base install's proof that a call can be answered.

Echo is what you run before you suspect your own agent, so it has to work with
nothing installed but the SDK and nothing configured but the secret. It was
called `minimal` when it was a package of its own; inside one SDK "minimal"
names nothing, and what it does - send the caller's own voice back - is the
name.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from standin import Caller, CallHandler, SessionStart
from standin.plugins import echo

pytestmark = pytest.mark.unit


class _Session:
    """Records what the handler puts on the wire. Enough of a CallSession."""

    def __init__(self) -> None:
        self.call_id = "call-1"
        self.start = SessionStart(
            call_id="call-1", thread_id="19:thread", caller=Caller(display_name="Marwan")
        )
        self.sent: list[bytes] = []

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)


def test_the_echo_handler_implements_only_what_it_needs() -> None:
    """No base class, no ABC, and no obligation to fill in the rest of the seam.

    Echo defines three of the five callbacks. The other two are simply absent,
    and the server treats a missing one as a no-op - which is the whole reason
    a new plugin can start this small. So `isinstance(..., CallHandler)`
    is deliberately False here: the runtime-checkable Protocol asks for all
    five, and requiring all five is exactly what the seam refuses to do.
    """
    assert echo.EchoHandler.__mro__[1:] == (object,)
    assert not isinstance(echo.EchoHandler(), CallHandler)

    implemented = {name for name in dir(echo.EchoHandler) if name.startswith("on_")}
    assert implemented == {"on_start", "on_caller_audio", "on_goodbye"}
    assert implemented < {name for name in dir(CallHandler) if name.startswith("on_")}


async def test_it_sends_the_caller_their_own_audio_back() -> None:
    handler = echo.EchoHandler()
    session = _Session()
    await handler.on_start(session)  # type: ignore[arg-type]
    await handler.on_caller_audio(b"\x01\x02" * 160)
    assert session.sent == [b"\x01\x02" * 160]


async def test_audio_before_start_is_dropped_not_raised() -> None:
    """A frame that arrives before on_start must not take the call down."""
    await echo.EchoHandler().on_caller_audio(b"\x00" * 320)


async def test_the_goodbye_callback_exists_and_is_quiet() -> None:
    await echo.EchoHandler().on_goodbye("thanks, bye")


def test_python_dash_m_reaches_the_call_server() -> None:
    """`python -m standin.plugins.echo` must still be a way to answer a call.

    Run with no secret, so it gets exactly as far as building the CallServer and
    then stops with the SDK's own configuration error. Any other failure means
    the module entry point is broken.
    """
    done = subprocess.run(
        [sys.executable, "-m", "standin.plugins.echo"],
        capture_output=True,
        text=True,
        cwd=str(Path(sys.executable).parent),
        env={"PATH": "/usr/bin:/bin"},
    )
    assert done.returncode != 0
    assert "a StandIn connection secret is required" in done.stderr


# ------------------------------------------------- the echo against a real dial


async def test_it_answers_a_real_dial_and_sends_the_voice_back() -> None:
    """The whole point, over a real socket: dial, speak, hear yourself.

    Everything between is the SDK's - the HMAC handshake, session.start, the
    frame loop, the outbound timeline - which is exactly why echo is what you
    run when you are not sure whose fault the silence is.
    """
    import asyncio
    import base64
    import json
    import socket

    import aiohttp

    from standin import SIGNATURE_HEADER, TIMESTAMP_HEADER, CallServer, now_ms, sign_handshake

    secret = "echo-secret-not-a-real-one"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    server = CallServer(
        handler_factory=echo.EchoHandler,
        secret=secret,
        host="127.0.0.1",
        port=port,
        audio_idle_timeout=0.0,
    )
    await server.start()
    try:
        call_id = "call-echo"
        stamp = now_ms()
        headers = {
            TIMESTAMP_HEADER: str(stamp),
            SIGNATURE_HEADER: sign_handshake(secret, stamp, call_id),
        }
        url = f"http://127.0.0.1:{port}{server.ws_path}/{call_id}"
        pcm = bytes(range(256)) * 2  # 512 bytes: 256 samples of PCM16

        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url, headers=headers) as ws:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "session.start",
                            "callId": call_id,
                            "threadId": "19:meeting@thread.v2",
                            "direction": "inbound",
                            "caller": {"displayName": "Marwan"},
                        }
                    )
                )
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "audio.frame",
                            "seq": 1,
                            "timestampMs": 0,
                            "payloadBase64": base64.b64encode(pcm).decode(),
                        }
                    )
                )

                async def first_audio_frame() -> dict:
                    async for msg in ws:
                        if msg.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        frame = json.loads(msg.data)
                        if frame.get("type") == "audio.frame":
                            return frame
                    raise AssertionError("the socket closed before the echo came back")

                echoed = await asyncio.wait_for(first_audio_frame(), 3.0)
                assert base64.b64decode(echoed["payloadBase64"]) == pcm

                await ws.send_str(json.dumps({"type": "session.end", "reason": "caller-hung-up"}))
    finally:
        await server.aclose()
