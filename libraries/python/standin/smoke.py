# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Proving an install works, without a bill, a tunnel or a Microsoft tenant.

The question every plugin has to answer before anybody trusts it with a real
call is "does this actually work here?", and until now the only way to find out
was to place a real call: a provider bill, a public tunnel, a Teams tenant and
somebody's afternoon.

So this rings the worker itself. It binds an ephemeral listener on loopback,
connects a real client that speaks the real call wire, streams a few frames of
silence, and reports what came back. Everything it exercises is the thing that
breaks in the field: the secret, the bind, the handshake, the session, and
whether audio makes the round trip.

The client lives next to the wire it speaks, deliberately. A copy of it kept in
a plugin drifts, and the copy that used to exist kept passing against a path
that no longer existed, which is exactly the failure a smoke check is for.

    report(await run_smoke(handler_factory=MyHandler))

**Nothing that delivers is started.** No pending-message sweep, no no-answer
reaper, no outbound caller, no chat lane. A verification command that quietly
resumed durable jobs would place real calls and post real messages as a side
effect of somebody typing ``smoke``, which is the single worst thing this could
possibly do.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp

from ._hmac import SIGNATURE_HEADER, TIMESTAMP_HEADER, now_ms, sign_handshake
from .call_server import CallServer
from .handler import CallHandler
from .log import logger

__all__ = [
    "FRAME_BYTES",
    "FRAME_MS",
    "SmokeCheck",
    "SmokeResult",
    "SyntheticCall",
    "report",
    "run_smoke",
]

#: PCM16 mono at 16 kHz for 20 ms. The cadence a real call arrives at.
FRAME_MS = 20
FRAME_BYTES = 16_000 * FRAME_MS // 1000 * 2

#: How long the whole run may take before it is a recorded failure rather than
#: a hang. A listener that accepts and then stops talking would otherwise hang
#: CI, and hang a status tool call for ever.
RUN_TIMEOUT_S = 15.0
CONNECT_TIMEOUT_S = 5.0

#: How long to look for an echoed frame between sends. Long enough to see one,
#: short enough not to stall the 20 ms cadence and make the call look idle to
#: the server's own watchdogs.
_DRAIN_TIMEOUT_S = 0.001


@dataclass(frozen=True)
class SmokeCheck:
    """One thing that either works here or does not."""

    name: str
    ok: bool
    detail: str = ""
    cost: str = ""
    """What stops working when this fails. A report that names the missing
    surface without naming the consequence sends the operator to the source."""

    required: bool = True
    """Whether ``ok`` depends on it. True by default, so a check a plugin adds
    counts unless it passes ``required=False``."""


@dataclass
class SmokeResult:
    """What the run found."""

    checks: list[SmokeCheck] = field(default_factory=list)
    echo_frames: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        """True only when every mandatory check passed.

        Never true when nothing echoed: the point of the run is that audio made
        the round trip, and a run that proves nothing must not read as a pass.
        """
        return all(check.ok for check in self.checks if check.required)


class SyntheticCall:
    """A client that speaks the call wire, for one call that nobody is on.

    Used by :func:`run_smoke`. Exposed because a plugin with its own listener
    may want to point one at it.
    """

    def __init__(
        self,
        url: str,
        secret: str,
        call_id: str,
        frames: int = 10,
        connect_timeout_s: float = CONNECT_TIMEOUT_S,
    ) -> None:
        self._url = url
        self._secret = secret
        self._call_id = call_id
        self._frames = max(1, frames)
        self._connect_timeout_s = connect_timeout_s
        self.echo_frames = 0

    async def run(self) -> None:
        """Connect, greet, stream, hang up. Raises on anything that fails."""
        session = aiohttp.ClientSession()
        ws: aiohttp.ClientWebSocketResponse | None = None
        try:
            # Signed freshly for this connect. The listener enforces single use
            # inside the freshness window, so a retry that reused these headers
            # would loop on 401 and read as a wrong secret.
            stamp = now_ms()
            ws = await asyncio.wait_for(
                session.ws_connect(
                    self._url,
                    headers={
                        TIMESTAMP_HEADER: str(stamp),
                        SIGNATURE_HEADER: sign_handshake(self._secret, stamp, self._call_id),
                    },
                ),
                self._connect_timeout_s,
            )
            await ws.send_str(
                json.dumps(
                    {
                        "type": "session.start",
                        # Identical to the id in the path. The listener refuses
                        # a start that disagrees with the authenticated path,
                        # and the refusal would read as a wire fault.
                        "callId": self._call_id,
                        "threadId": "",
                        "direction": "inbound",
                        "caller": {"displayName": "StandIn smoke check"},
                    }
                )
            )
            # Handlers commonly gate output on the call being recorded, so
            # without this a recording-gated handler stays silent and the run
            # reports a false negative.
            await ws.send_str(json.dumps({"type": "recording.status", "status": "active"}))

            silence = base64.b64encode(bytes(FRAME_BYTES)).decode("ascii")
            for seq in range(self._frames):
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "audio.frame",
                            "seq": seq,
                            "timestampMs": seq * FRAME_MS,
                            "payloadBase64": silence,
                        }
                    )
                )
                if not await self._drain(ws):
                    break
                await asyncio.sleep(FRAME_MS / 1000)

            await ws.send_str(json.dumps({"type": "session.end", "reason": "smoke-done"}))
            await self._drain(ws)
        finally:
            # On every path. An error path that skipped this leaks a client
            # session per run inside a long-lived host that exposes the check.
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            with contextlib.suppress(Exception):
                await session.close()

    async def _drain(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Take whatever is waiting, counting echoed audio. False to stop.

        Bounded rather than blocking: a blocking read stalls the cadence and
        the call looks idle to the server's watchdogs, and a socket that is
        never drained makes the echo count zero for ever, so "audio came back"
        could never be proven.
        """
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), _DRAIN_TIMEOUT_S)
            except TimeoutError:
                return True
            if msg.type is not aiohttp.WSMsgType.TEXT:
                return False
            with contextlib.suppress(Exception):
                if json.loads(msg.data).get("type") == "audio.frame":
                    self.echo_frames += 1


async def run_smoke(
    handler_factory: Callable[[], CallHandler],
    frames: int = 10,
    extra: Callable[[], Awaitable[list[SmokeCheck]]] | None = None,
    timeout_s: float = RUN_TIMEOUT_S,
) -> SmokeResult:
    """Ring this worker's own handler and report what worked.

    ``extra`` is a plugin's own checks, run after the call. Each one counts
    towards ``ok`` unless it says ``required=False``.
    """
    result = SmokeResult()

    # Its own credential, never the operator's. A fixed string would be a
    # predictable one on a live listener, and borrowing the operator's makes the
    # run pass or fail for reasons that have nothing to do with the wire. It
    # also makes the check runnable before the secret is configured, which is
    # exactly when people run it.
    secret = secrets.token_hex(16)
    result.checks.append(
        SmokeCheck("secret", True, "generated for this run", "nothing could authenticate")
    )

    server = CallServer(
        secret=secret,
        host="127.0.0.1",
        port=0,
        handler_factory=handler_factory,
        stale_call_reaper_seconds=0,
    )
    try:
        await server.start()
    except Exception as err:
        result.error = str(err)
        result.checks.append(
            SmokeCheck("listener", False, str(err), "no call could ever reach this worker")
        )
        return result
    result.checks.append(
        SmokeCheck(
            "listener",
            True,
            f"127.0.0.1:{server.port}{server.ws_path}",
            "no call could ever reach this worker",
        )
    )

    call_id = f"smoke-{secrets.token_hex(4)}"
    url = f"http://127.0.0.1:{server.port}{server.ws_path}/{call_id}"
    call = SyntheticCall(url, secret, call_id, frames=frames)
    started = time.monotonic()
    try:
        await asyncio.wait_for(call.run(), timeout_s)
    except TimeoutError:
        result.error = f"the call did not finish within {timeout_s:g} seconds"
        result.checks.append(
            SmokeCheck("call", False, result.error, "a real call would hang the same way")
        )
    except Exception as err:
        result.error = str(err)
        result.checks.append(
            SmokeCheck("call", False, str(err), "a real call would fail the same way")
        )
    else:
        elapsed = int((time.monotonic() - started) * 1000)
        result.checks.append(
            SmokeCheck("call", True, f"{frames} frames in {elapsed} ms", "calls do not connect")
        )
    finally:
        with contextlib.suppress(Exception):
            await server.aclose()

    result.echo_frames = call.echo_frames
    result.checks.append(
        SmokeCheck(
            "audio",
            call.echo_frames > 0,
            f"{call.echo_frames} frames came back",
            "the caller would hear nothing",
        )
    )

    if extra is not None:
        try:
            result.checks.extend(await extra())
        except Exception as err:
            logger.debug("standin: a plugin smoke check failed: %s", err)
            # Advisory: the plugin's own checks failing to RUN says nothing
            # about whether a call works, which is what this command answers.
            result.checks.append(
                SmokeCheck(
                    "plugin checks",
                    False,
                    str(err),
                    "this plugin's own checks did not run",
                    required=False,
                )
            )
    return result


def report(result: SmokeResult) -> str:
    """The result as something to print."""
    lines = [f"standin: {'ok' if result.ok else 'NOT ok'}"]
    for check in result.checks:
        mark = "ok  " if check.ok else "FAIL"
        note = f" ({check.detail})" if check.detail else ""
        lines.append(f"  {mark} {check.name}{note}")
        if not check.ok and check.cost:
            lines.append(f"       without it: {check.cost}")
    if result.error:
        lines.append(f"  error: {result.error}")
    return "\n".join(lines)
