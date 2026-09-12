# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Calling somebody, instead of waiting for them to call you.

Every other lane in this SDK starts with a caller dialling your agent. This one
runs the other way: your agent asks StandIn to ring a Microsoft Teams user, and
speaks when they answer.

That inversion is what makes it worth its own module, because the leg that
answers is **a different call**. You ask for the call in one place, and minutes
later StandIn dials your worker with ``direction="outbound"`` and a fresh
``callId``. The thing you wanted said has to survive the gap, so
:class:`PendingMessages` parks it on disk and the handler pops it when the leg
arrives. Park it in memory and a restart between the two loses it silently, with
the caller's phone still ringing.

Three pieces, and you will normally use all three:

:class:`OutboundCaller`
    Asks StandIn to place the call, and cancels one that is still ringing.
:class:`PendingMessages`
    Remembers what to say, across processes and restarts.
:class:`OutboundPolicy`
    Decides whether this agent is allowed to ring this person at all.

**Read the policy before you skip it.** Inbound, the caller chose to dial you.
Outbound, a model decided to ring somebody, and that model is steered by whoever
is talking to it. An agent with an outbound tool and no allowlist is an agent
that can be talked into cold-calling your directory, which is why the allowlist
here is separate from and stricter than any inbound one: allowing all inbound
callers does not allow any outbound target.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import aiohttp

from ._exceptions import StandInError
from ._hmac import SIGNATURE_V2_HEADER, TIMESTAMP_HEADER, now_ms, sign_request
from .calltools import ToolSpec

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from .chat import InboundMessage
    from .handler import CallSession
from .log import logger

__all__ = [
    "OutboundCaller",
    "OutboundError",
    "OutboundPolicy",
    "PendingMessage",
    "PendingMessages",
    "PlacedCall",
    "state_dir",
]

#: The control route StandIn exposes for placing a call. v2 signs the path, so
#: this string is part of the signature: a route that is merely plausible
#: produces a valid-looking request that is refused.
_PLACE_PATH = "/api/calls"

#: Where the worker listens for control requests, when nothing says otherwise.
_DEFAULT_WORKER_URL = "http://127.0.0.1:9440"

_DEFAULT_TIMEOUT_S = 15.0


class OutboundError(StandInError):
    """Placing or cancelling an outbound call failed.

    Carries the reason in its message, because the thing that usually wants it
    is a tool result being read back to whoever asked for the call.
    """


@dataclass(frozen=True)
class PlacedCall:
    """StandIn accepted the request and is ringing the callee."""

    call_id: str
    """The id the answering leg will arrive with. Park your message against it."""

    scenario_id: str = ""
    """StandIn's own correlation id, when it sends one."""


def state_dir() -> Path:
    """Where durable outbound state lives.

    ``STANDIN_STATE_DIR`` when set, otherwise ``~/.standin/state``. Created with
    owner-only permissions, because what is parked here is what your agent is
    about to say to somebody.

    Deliberately NOT a temp directory. A temp directory passes every test and
    loses every parked message on the next reboot, which is invisible until a
    caller answers a call that then says nothing.
    """
    configured = os.environ.get("STANDIN_STATE_DIR", "").strip()
    path = Path(configured) if configured else Path.home() / ".standin" / "state"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


# ---------------------------------------------------------------- the client


class OutboundCaller:
    """Asks StandIn to ring a Microsoft Teams user.

    One instance per worker is enough; it holds no per-call state.

        caller = OutboundCaller()
        placed = await caller.place_call(user_object_id=aad_id, tenant_id=tenant)
        pending.park(placed.call_id, "Your build finished.")

    Signed with v2 only, and that is deliberate. v2 binds the method, the path
    and a hash of the body, which is what puts ``tenant_id`` under the
    signature. v1 signs a single value, so a v1-signed request leaves the
    organisation being rung unsigned, and sending both would let a downgrade
    pick the weaker one.
    """

    def __init__(
        self,
        secret: str | None = None,
        worker_url: str | None = None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._secret = secret if secret is not None else os.environ.get("STANDIN_SECRET", "")
        if not self._secret:
            raise OutboundError("STANDIN_SECRET is required to place an outbound call")
        raw = worker_url or os.environ.get("STANDIN_WORKER_URL", "") or _DEFAULT_WORKER_URL
        self._worker_url = _check_worker_url(raw)
        self._timeout_s = timeout_s
        #: Addresses the worker host resolved to the first time we looked. The
        #: connect is pinned to these, so a name that answers publicly once and
        #: privately later cannot move where the secret is sent. Private space
        #: is ALLOWED here: a worker on a private network is the normal
        #: deployment, and the guard is about the host not MOVING.
        self._pinned: tuple[str, ...] | None = None

    @property
    def worker_url(self) -> str:
        """The control endpoint this caller talks to."""
        return self._worker_url

    async def place_call(self, user_object_id: str, tenant_id: str) -> PlacedCall:
        """Ring a Microsoft Teams user. Returns the id the answering leg carries.

        Raises :class:`OutboundError` for anything that is not an accepted
        request, with the reason in the message, so a tool can read it back to
        whoever asked for the call.
        """
        target = user_object_id.strip()
        if not target:
            raise OutboundError("an outbound call needs the person's directory id")
        body = json.dumps(
            {"userObjectId": target, "tenantId": tenant_id.strip()},
            separators=(",", ":"),
            ensure_ascii=False,
        )
        payload = await self._send("POST", _PLACE_PATH, body)
        call_id = payload.get("callId") or payload.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise OutboundError("StandIn accepted the call but returned no callId")
        scenario = payload.get("scenarioId") or payload.get("scenario_id") or ""
        return PlacedCall(call_id=call_id, scenario_id=str(scenario))

    async def cancel_call(self, call_id: str) -> bool:
        """Stop a call that is still ringing. Never raises.

        Best-effort on purpose: this runs on the no-answer path, where the
        caller has already stopped waiting and an exception would only turn a
        tidy-up into a failure. A call that has already gone counts as
        cancelled.
        """
        if not call_id:
            return False
        try:
            await self._send("DELETE", f"{_PLACE_PATH}/{call_id}", "")
            return True
        except OutboundError as err:
            logger.info("standin: could not cancel outbound call %s: %s", call_id, err)
            return False

    async def _send(self, method: str, path: str, body: str) -> dict[str, Any]:
        timestamp = str(now_ms())
        headers = {
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_V2_HEADER: sign_request(self._secret, timestamp, method, path, body),
            "content-type": "application/json",
        }
        connector = aiohttp.TCPConnector(resolver=_PinnedResolver(self))
        timeout = aiohttp.ClientTimeout(total=self._timeout_s)
        url = f"{self._worker_url}{path}"
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.request(
                    method, url, data=body.encode("utf-8") if body else None, headers=headers
                ) as response:
                    text = await response.text()
                    if response.status == 401:
                        # The one failure worth naming precisely: v2 signs the
                        # path, so a wrong path reads exactly like a wrong
                        # secret and has cost people hours.
                        raise OutboundError(
                            f"StandIn rejected the signature on {method} {path}. "
                            "Check STANDIN_SECRET, and that the clock is not skewed."
                        )
                    if response.status >= 400:
                        raise OutboundError(
                            f"{method} {path} returned HTTP {response.status}: {text[:200]}"
                        )
                    if not text.strip():
                        return {}
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        return {}
                    return parsed if isinstance(parsed, dict) else {}
        except aiohttp.ClientError as err:
            raise OutboundError(
                f"could not reach the StandIn worker at {self._worker_url}: {err}"
            ) from err
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise OutboundError(f"{method} {path} timed out after {self._timeout_s}s") from err


def _check_worker_url(raw: str) -> str:
    """Validate the control endpoint and return it without a trailing slash."""
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise OutboundError(f"STANDIN_WORKER_URL must be http or https, got {raw!r}")
    if not parts.hostname:
        raise OutboundError(f"STANDIN_WORKER_URL has no host: {raw!r}")
    if parts.username or parts.password:
        raise OutboundError("STANDIN_WORKER_URL must not carry credentials")
    return raw.rstrip("/")


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Pin the worker host to the addresses it first resolved to.

    The connection secret goes to whatever this resolves. Private addresses are
    fine here, unlike the guard in :mod:`standin.fetch`: a worker on a private
    network is the normal deployment. What is NOT fine is the host moving
    between the first request and a later one, which is what this catches.
    """

    def __init__(self, caller: OutboundCaller) -> None:
        self._caller = caller

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_INET
    ) -> list[dict[str, object]]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port or None, type=socket.SOCK_STREAM)
        addrs = tuple(sorted({info[4][0] for info in infos}))
        if not addrs:
            raise OSError(f"the StandIn worker host {host} resolves to no addresses")
        pinned = self._caller._pinned
        if pinned is None:
            self._caller._pinned = addrs
        elif not set(addrs) & set(pinned):
            raise OSError(
                f"the StandIn worker host {host} now resolves to {addrs}, "
                f"not the {pinned} it resolved to before; refusing to send the secret there"
            )
        allowed = set(self._caller._pinned or addrs)
        results: list[dict[str, object]] = []
        for info in infos:
            addr = info[4][0]
            if addr not in allowed:
                continue
            fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
            if family not in (socket.AF_UNSPEC, fam):
                continue
            results.append(
                {
                    "hostname": host,
                    "host": addr,
                    "port": port,
                    "family": fam,
                    "proto": 0,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not results:
            raise OSError(f"{host} has no pinned address for the requested family")
        return results

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None


# ------------------------------------------------------------- what to say


@dataclass(frozen=True)
class PendingMessage:
    """What to say when this call is answered, and where it came from."""

    call_id: str
    text: str
    """The line to speak on answer."""

    thread_id: str = ""
    """The Microsoft Teams conversation the request came from, so an unanswered
    call can put the answer there instead of losing it."""

    requested_by: str = ""
    """Directory id of whoever asked for the call."""

    created_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ""
    """Which tenant to post the fallback into. Never taken from a model."""

    target: str = ""
    """Directory id of whoever was rung, so a second call to the same person
    can be refused while the first is still ringing."""

    attempts: int = 0
    """Delivery attempts so far, so a chat that keeps failing stops."""

    def as_json(self) -> dict[str, Any]:
        return {
            "callId": self.call_id,
            "text": self.text,
            "threadId": self.thread_id,
            "requestedBy": self.requested_by,
            "createdMs": self.created_ms,
            "metadata": self.metadata,
            "tenantId": self.tenant_id,
            "target": self.target,
            "attempts": self.attempts,
        }

    @staticmethod
    def from_json(raw: dict[str, Any]) -> PendingMessage:
        meta = raw.get("metadata")
        return PendingMessage(
            call_id=str(raw.get("callId") or ""),
            text=str(raw.get("text") or ""),
            thread_id=str(raw.get("threadId") or ""),
            requested_by=str(raw.get("requestedBy") or ""),
            created_ms=int(raw.get("createdMs") or 0),
            metadata=meta if isinstance(meta, dict) else {},
            # Absent on a record parked by an older build. Read as empty rather
            # than failing: a restart must not lose what somebody was told.
            tenant_id=str(raw.get("tenantId") or ""),
            target=str(raw.get("target") or ""),
            attempts=int(raw.get("attempts") or 0),
        )


class PendingMessages:
    """What the agent wanted said, parked until the call is answered.

    On disk, because the answering leg is a different call and may be a
    different process. A restart between asking for a call and it being answered
    is ordinary, and an in-memory store loses the message silently: the callee
    picks up and hears nothing.

    Popping is atomic. Two workers racing the same answered call is a normal
    consequence of running more than one, and only one of them may speak.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or (state_dir() / "outbound")
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, call_id: str) -> Path:
        return self._dir / f"{_safe_name(call_id)}.json"

    def park(self, message: PendingMessage) -> None:
        """Remember what to say on this call. Overwrites an earlier one."""
        record = message.as_json()
        if not record["createdMs"]:
            record["createdMs"] = now_ms()
        target = self._path(message.call_id)
        # Written beside and renamed, so a reader never sees half a record.
        temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(target)

    def pop(self, call_id: str) -> PendingMessage | None:
        """Take the message for this call, once.

        The rename is the lock: exactly one caller can rename a given file, so
        two workers answering the same leg cannot both speak.
        """
        target = self._path(call_id)
        claimed = target.with_suffix(f".{uuid.uuid4().hex}.claimed")
        try:
            target.rename(claimed)
        except OSError:
            return None
        try:
            message = PendingMessage.from_json(json.loads(claimed.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            message = None
        finally:
            with contextlib.suppress(OSError):
                claimed.unlink()
        return message

    def waiting(self) -> list[PendingMessage]:
        """Every parked record, read without claiming any of them.

        For asking "am I already calling this person?" before ringing them
        again. A reserved record is deliberately absent: that call is already
        connected, so it is not one somebody is still waiting on.
        """
        out: list[PendingMessage] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                out.append(PendingMessage.from_json(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        return out

    def reserve(self, call_id: str) -> PendingMessage | None:
        """Take this record for a leg that is ringing, without deleting it.

        A rename, exactly like :meth:`pop`, so only one worker can hold it. The
        difference is what happens next: a reserved record can be given BACK.
        A leg that never gets answered has to leave the message where the sweep
        will find it, or the answer is lost because nobody picked up.

        While reserved the record is invisible to :meth:`claim_stale`, which
        globs ``*.json``: the sweep must not post "I could not reach you" to a
        call that is still ringing.
        """
        target = self._path(call_id)
        held = target.with_suffix(".answering")
        try:
            target.rename(held)
        except OSError:
            return None
        try:
            return PendingMessage.from_json(json.loads(held.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                held.unlink()
            return None

    def commit(self, call_id: str) -> None:
        """It was said. Retire the record."""
        with contextlib.suppress(OSError):
            self._path(call_id).with_suffix(".answering").unlink(missing_ok=True)

    def release(self, call_id: str) -> None:
        """It was not said. Put it back for the sweep to deliver to chat."""
        held = self._path(call_id).with_suffix(".answering")
        with contextlib.suppress(OSError):
            if held.exists():
                held.rename(self._path(call_id))

    def recover_reservations(self, older_than_s: float) -> int:
        """Give back reservations whose worker died holding them.

        Judged by the reservation's own age. Without this a process that dies
        mid-ring leaves the message reserved for ever, and the person who was
        promised an answer never gets one.
        """
        cutoff = time.time() - older_than_s
        recovered = 0
        for path in sorted(self._dir.glob("*.answering")):
            with contextlib.suppress(OSError):
                if path.stat().st_mtime > cutoff:
                    continue
                path.rename(path.with_suffix(".json"))
                recovered += 1
        return recovered

    def claim_stale(self, older_than_s: float) -> list[PendingMessage]:
        """Take every message nobody answered in time.

        Used by the no-answer sweep: a parked message older than the ringing
        window means the callee never picked up, and what the agent wanted said
        should go to the chat it came from rather than evaporate.

        The claim stamps its OWN time rather than inheriting the record's. A
        record is claimed precisely because it is already old, so judging a
        half-finished claim by the record's age would let a second sweep take a
        message the first is still delivering, and post it twice.
        """
        cutoff_ms = now_ms() - int(older_than_s * 1000)
        taken: list[PendingMessage] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if int(record.get("createdMs") or 0) > cutoff_ms:
                continue
            message = self.pop(str(record.get("callId") or path.stem))
            if message is not None:
                taken.append(message)
        return taken

    def recover_orphans(self, older_than_s: float) -> list[PendingMessage]:
        """Take back messages a crashed sweep claimed and never delivered.

        Judged by how long ago the CLAIM was made, which is why claiming writes
        a fresh file rather than renaming in place. Give this a longer window
        than :meth:`claim_stale`, so an in-flight delivery is never taken from
        under a worker that is still working on it.
        """
        cutoff = time.time() - older_than_s
        taken: list[PendingMessage] = []
        for path in sorted(self._dir.glob("*.claimed")):
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                message = PendingMessage.from_json(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
            taken.append(message)
            with contextlib.suppress(OSError):
                path.unlink()
        return taken


def _safe_name(call_id: str) -> str:
    """A filename that cannot escape the directory it belongs in."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in call_id)[:120] or "unnamed"


# ----------------------------------------------------------------- the policy


@dataclass
class OutboundPolicy:
    """Who this agent may ring, and how often.

    Separate from any inbound allowlist, and stricter, because the two answer
    different questions. Inbound asks "may this person talk to the agent?" and
    the person chose to dial. Outbound asks "may the agent ring this person?"
    and the agent was talked into it by whoever is on the call.

    So allowing every inbound caller allows no outbound target. A directory id
    must be listed here, explicitly, before the agent can ring it.
    """

    allowed: frozenset[str] = frozenset()
    """Directory ids the agent may ring. Empty means outbound is off."""

    max_per_hour: int = 6
    """Calls placed in any rolling hour, across all targets. Zero means no cap,
    which is a deliberate choice rather than a default."""

    _placed: list[float] = field(default_factory=list, repr=False)

    @staticmethod
    def from_env() -> OutboundPolicy:
        """Read ``STANDIN_OUTBOUND_ALLOW`` and ``STANDIN_OUTBOUND_MAX_PER_HOUR``.

        The allowlist is a comma-separated list of directory ids. Unset means
        outbound calling is off, which is the right default for a capability
        that can ring a stranger.
        """
        raw = os.environ.get("STANDIN_OUTBOUND_ALLOW", "")
        allowed = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
        try:
            cap = int(os.environ.get("STANDIN_OUTBOUND_MAX_PER_HOUR", "6") or 6)
        except ValueError:
            cap = 6
        return OutboundPolicy(allowed=allowed, max_per_hour=max(0, cap))

    def check(self, user_object_id: str) -> None:
        """Raise :class:`OutboundError` unless this call may be placed now."""
        # Folded on both sides. A directory id is not case-sensitive, and a
        # case mismatch would read as "not allowed" with nothing to say why.
        target = user_object_id.strip().lower()
        allowed = {entry.lower() for entry in self.allowed}
        if not target:
            raise OutboundError("an outbound call needs the person's directory id")
        if not self.allowed:
            raise OutboundError(
                "outbound calling is off: set STANDIN_OUTBOUND_ALLOW to the directory ids "
                "this agent may ring"
            )
        if target not in allowed:
            raise OutboundError("that person is not on this agent's outbound allowlist")
        if self.max_per_hour:
            cutoff = time.monotonic() - 3600
            self._placed = [t for t in self._placed if t > cutoff]
            if len(self._placed) >= self.max_per_hour:
                raise OutboundError(
                    f"this agent has already placed {self.max_per_hour} calls in the last hour"
                )

    def record(self) -> None:
        """Count a placed call against the hourly cap."""
        self._placed.append(time.monotonic())


# ---------------------------------------------------------------- the lane

#: How long a call may ring before nobody is going to answer it.
DEFAULT_ANSWER_TIMEOUT_S = 120.0

#: How often the sweep looks for calls nobody answered.
DEFAULT_SWEEP_INTERVAL_S = 30.0

#: After this, an undelivered answer is too old to be worth sending.
DEFAULT_PENDING_TTL_S = 3600.0

#: A reservation older than this belonged to a worker that died holding it.
RESERVATION_STALE_S = 600.0

#: The answering leg can attach before the message has been parked, so attach
#: waits a little rather than deciding there is nothing to say.
PARK_GRACE_S = 5.0
PARK_POLL_S = 0.25

#: The same race on the outcome path.
OUTCOME_GRACE_S = 5.0

#: How long after somebody's chat message the agent may ring them back.
CHAT_CALLBACK_WINDOW_S = 600.0

#: What a model may park. It is read out loud on answer.
MAX_PENDING_TEXT_CHARS = 4000

#: How many times a failing chat delivery is retried before it is dropped.
MAX_DELIVERY_ATTEMPTS = 5

#: Outcomes that mean nobody took the call.
UNANSWERED_OUTCOMES = frozenset({"no-answer", "declined", "busy", "failed"})

#: What to say in chat for each of them. Written for the person who missed the
#: call, not for an operator reading a log.
OUTCOME_WORDING = {
    "no-answer": "I tried to call you but couldn't reach you.",
    "declined": "You declined my call, no problem.",
    "busy": "I tried to call you but the line was busy.",
    "failed": "I tried to call you but the call could not be completed.",
}

#: Marks the fallback so it reads as a missed call rather than a stray message.
NO_ANSWER_PREFIX = "\U0001f4de "


def call_thread_is_postable(thread_id: str, call_id: str) -> bool:
    """Whether a live call has a chat its answer could go to instead.

    A one-to-one call has no meeting conversation, and the field then carries
    something that is not one. Posting to it would either fail or reach the
    wrong place, so a call without a real thread is parked with no fallback.
    """
    thread = (thread_id or "").strip()
    return bool(thread) and thread.startswith("19:") and thread != call_id


@dataclass(frozen=True)
class ChatCallbackTarget:
    """Who to ring, resolved from a chat message rather than from a model."""

    user_object_id: str
    tenant_id: str
    conversation_id: str
    display_name: str = ""


class ChatSender(Protocol):
    """Whatever can post into a conversation. :class:`ChatChannel` satisfies it."""

    async def send(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        text: str,
        idempotency_key: str | None = None,
    ) -> bool: ...


#: Say the parked line. The plugin owns the wording, because only it knows
#: whether its provider takes an instruction or a literal line of speech.
Speak = Callable[[PendingMessage], Awaitable[None]]


CHAT_CALLBACK_TOOL = ToolSpec(
    name="call_me_with_the_answer",
    description=(
        "Ring the person you are talking to and tell them the answer out loud, instead of "
        "replying here. Use it when they ask you to call them, or when the answer is easier "
        "said than written."
    ),
    parameters={"message": {"type": "string", "description": "What to say when they answer."}},
    required=("message",),
)

CALL_BACK_TOOL = ToolSpec(
    name="call_me_back",
    description=(
        "Ring this caller again later and say something. Use it when the work will not finish "
        "while they are on the line and they asked to be called rather than messaged."
    ),
    parameters={"message": {"type": "string", "description": "What to say when they answer."}},
    required=("message",),
)


@dataclass
class _ChatSenderRecord:
    user_object_id: str
    tenant_id: str
    display_name: str
    at_ms: int


class OutboundLeg:
    """One answering leg, holding the message until somebody actually answers.

    Built by :meth:`OutboundLane.attach` from ``on_start``. The plugin forwards
    two things and the leg does the rest::

        self._leg = lane.attach(session, self._say)
        ...
        async def on_context(self, text): await self._leg.on_context()
        async def aclose(self, reason): await self._leg.aclose(reason)
    """

    def __init__(
        self,
        lane: OutboundLane,
        session: CallSession,
        message: PendingMessage | None,
        speak: Speak,
        answer_timeout_s: float,
    ) -> None:
        self._lane = lane
        self._session = session
        self._message = message
        self._speak = speak
        self._answer_timeout_s = answer_timeout_s
        self._spoken = False
        self._closed = False
        self._watchdog: asyncio.Task[None] | None = None

    @property
    def message(self) -> PendingMessage | None:
        """What is waiting to be said, if anything."""
        return self._message

    def arm(self) -> None:
        """Start watching. Called by the lane once the record is settled."""
        if self._message is None or self._closed:
            return
        self._watchdog = asyncio.ensure_future(self._watch())
        # Answered before we even looked: a fast pickup beats the attach.
        if self._session.recording_active:
            asyncio.ensure_future(self._deliver())

    async def on_context(self) -> None:
        """Forward every ``on_context``. Recording going active is the answer.

        There is no "they picked up" message on the wire. Recording turning on
        is what happens when a Microsoft Teams call is actually connected, so
        that transition is the signal, and the plugin already receives it.
        """
        if self._session.recording_active:
            await self._deliver()

    async def answered(self) -> None:
        """Say it now. For a plugin with a better signal than the recording."""
        await self._deliver()

    async def aclose(self, reason: str = "call-ended") -> None:
        """The leg is over. Anything unsaid goes back for the sweep."""
        if self._closed:
            return
        self._closed = True
        if self._watchdog is not None:
            self._watchdog.cancel()
        if self._message is not None and not self._spoken:
            # Released, not dropped: nobody heard it, so it still has to reach
            # them somehow.
            self._lane._pending.release(self._message.call_id)
            logger.info(
                "standin: outbound call %s ended unanswered (%s); the answer goes to chat",
                _safe_name(self._message.call_id),
                reason,
            )

    async def _deliver(self) -> None:
        if self._spoken or self._closed or self._message is None:
            return
        self._spoken = True
        if self._watchdog is not None:
            self._watchdog.cancel()
        try:
            await self._speak(self._message)
        except Exception:
            # Saying it failed, so it was not said. Put it back rather than
            # pretending the person was told.
            self._spoken = False
            logger.exception("standin: speaking the parked message failed")
            return
        self._lane._pending.commit(self._message.call_id)
        self._lane._finalized.add(self._message.call_id)

    async def _watch(self) -> None:
        """End a leg that rings for ever.

        The idle watchdog cannot do this: a ringing leg carries no caller audio
        by definition, so to that watchdog every outbound call looks dead.
        """
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(self._answer_timeout_s)
            if self._spoken or self._closed:
                return
            logger.info(
                "standin: nobody answered outbound call %s within %.0fs",
                _safe_name(self._session.call_id),
                self._answer_timeout_s,
            )
            await self._session.end("outbound-no-answer")


class OutboundLane:
    """Placing a call, saying the thing, and what to do when nobody answers.

    The three are one capability, and splitting them is how the answer gets
    lost. A call is placed because somebody is owed something; if they do not
    pick up, they are still owed it.

        lane = OutboundLane(chat=channel, tenant_id=tenant)
        lane.start()
        server = CallServer(handler_factory=..., on_call_outcome=lane.on_outcome)

    Everything durable is on disk, so a restart between the ring and the answer
    loses nothing.
    """

    def __init__(
        self,
        *,
        caller: OutboundCaller | None = None,
        policy: OutboundPolicy | None = None,
        pending: PendingMessages | None = None,
        chat: ChatSender | None = None,
        tenant_id: str = "",
        answer_timeout_s: float = DEFAULT_ANSWER_TIMEOUT_S,
        sweep_interval_s: float = DEFAULT_SWEEP_INTERVAL_S,
        ttl_s: float = DEFAULT_PENDING_TTL_S,
        max_in_flight_per_target: int = 1,
    ) -> None:
        self._caller = caller
        self._policy = policy if policy is not None else OutboundPolicy.from_env()
        self._pending = pending if pending is not None else PendingMessages()
        self._chat = chat
        self._tenant_id = tenant_id
        self._answer_timeout_s = answer_timeout_s
        self._sweep_interval_s = sweep_interval_s
        self._ttl_s = ttl_s
        self._max_in_flight = max(1, max_in_flight_per_target)
        self._senders: dict[str, _ChatSenderRecord] = {}
        self._finalized: set[str] = set()
        self._sweeper: asyncio.Task[None] | None = None

    # ---- placing ---------------------------------------------------------

    async def place(
        self,
        *,
        user_object_id: str,
        text: str,
        tenant_id: str = "",
        thread_id: str = "",
        requested_by: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PlacedCall:
        """Ring somebody and park what to say. Raises :class:`OutboundError`.

        Everything that can be refused is refused BEFORE the call is placed, so
        a refusal never leaves somebody's phone ringing for a message that was
        never going to be sent.
        """
        line = (text or "").strip()
        if not line:
            raise OutboundError("there was nothing to say, so I did not call")
        if len(line) > MAX_PENDING_TEXT_CHARS:
            raise OutboundError(
                f"that message is too long to deliver by phone ({len(line)} characters)"
            )
        self._policy.check(user_object_id)

        target = user_object_id.strip().lower()
        in_flight = sum(1 for held in self._pending.waiting() if held.target == target)
        if in_flight >= self._max_in_flight:
            raise OutboundError("I am already calling that person about something else")

        if self._caller is None:
            raise OutboundError("this worker is not set up to place calls")
        tenant = (tenant_id or self._tenant_id).strip()
        placed = await self._caller.place_call(user_object_id.strip(), tenant)
        self._policy.record()

        # Only a real conversation. A call that has none is parked with no
        # fallback rather than one that would fail or reach the wrong place.
        fallback = thread_id if call_thread_is_postable(thread_id, placed.call_id) else ""
        self._pending.park(
            PendingMessage(
                call_id=placed.call_id,
                text=line,
                thread_id=fallback,
                requested_by=requested_by,
                created_ms=now_ms(),
                metadata=metadata or {},
                tenant_id=tenant,
                target=target,
            )
        )
        # One audit line, and never the text: it is somebody's message.
        logger.info(
            "standin: placed an outbound call to %s (call %s, chat fallback %s, asked by %s)",
            _safe_name(target),
            _safe_name(placed.call_id),
            "yes" if fallback else "no",
            _safe_name(requested_by or "unknown"),
        )
        return placed

    # ---- answering -------------------------------------------------------

    def attach(self, session: CallSession, speak: Speak) -> OutboundLeg | None:
        """Bind an answering leg to whatever was parked for it.

        Returns ``None`` on an inbound call, so a plugin can call it
        unconditionally from ``on_start``.
        """
        if session.start.direction != "outbound":
            return None
        held = self._pending.reserve(session.call_id)
        leg = OutboundLeg(self, session, held, speak, self._answer_timeout_s)
        if held is not None:
            leg.arm()
        else:
            # The leg can be answered before place() has finished parking, so
            # waiting a moment beats deciding there is nothing to say. Spawned
            # rather than awaited: on_start must not block the frame loop.
            asyncio.ensure_future(self._attach_later(session, leg))
        return leg

    async def _attach_later(self, session: CallSession, leg: OutboundLeg) -> None:
        deadline = time.monotonic() + PARK_GRACE_S
        while time.monotonic() < deadline:
            await asyncio.sleep(PARK_POLL_S)
            held = self._pending.reserve(session.call_id)
            if held is not None:
                leg._message = held
                leg.arm()
                return
        if session.call_id in self._finalized:
            # Already delivered or already given up on. Not a fresh call.
            await session.end("outbound-expired")

    # ---- not answering ---------------------------------------------------

    async def on_outcome(self, call_id: str, outcome: str) -> bool:
        """What StandIn reports when an outbound call ended without an answer.

        Pass it to ``CallServer(on_call_outcome=...)``. An outcome this does not
        recognise is logged and ignored: an unknown word is not a failure, and
        treating it as one would post "I could not reach you" to somebody who
        answered.
        """
        state = (outcome or "").strip().lower()
        if state == "answered":
            return True
        if state not in UNANSWERED_OUTCOMES:
            logger.info("standin: ignoring an outbound outcome this SDK does not know: %s", state)
            return True

        if call_id in self._finalized:
            # The sweep already told them. Waiting out the grace for a record
            # that is gone delays nothing and helps nobody.
            return True

        deadline = time.monotonic() + OUTCOME_GRACE_S
        while True:
            held = self._pending.pop(call_id)
            if held is not None:
                return await self._deliver_to_chat(held, state)
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(PARK_POLL_S)

    async def sweep(self) -> int:
        """Deliver what nobody answered. Returns how many went out."""
        self._pending.recover_reservations(RESERVATION_STALE_S)
        delivered = 0
        for held in self._pending.claim_stale(self._answer_timeout_s):
            if await self._deliver_to_chat(held, "no-answer"):
                delivered += 1
            if self._caller is not None and held.call_id:
                # Fire and forget. A ring nobody will answer should stop, but a
                # failure to stop it must not lose the message.
                with contextlib.suppress(Exception):
                    await self._caller.cancel_call(held.call_id)
        return delivered

    async def _deliver_to_chat(self, held: PendingMessage, outcome: str) -> bool:
        self._finalized.add(held.call_id)
        if not held.thread_id or self._chat is None:
            logger.warning(
                "standin: outbound call %s went unanswered and there is no chat to tell them",
                _safe_name(held.call_id),
            )
            return False
        body = f"{NO_ANSWER_PREFIX}{OUTCOME_WORDING[outcome]} Here's what I had: {held.text}"
        try:
            sent = await self._chat.send(
                tenant_id=held.tenant_id or self._tenant_id,
                conversation_id=held.thread_id,
                text=body,
                # The timer and the outcome can both fire for one call. The same
                # key means the person is told once.
                idempotency_key=f"standin-noanswer-{held.call_id}",
            )
        except Exception as err:
            logger.warning("standin: posting an unanswered call's message failed: %s", err)
            sent = False
        if sent:
            return True
        self._requeue(held)
        return False

    def _requeue(self, held: PendingMessage) -> None:
        """Put a failed delivery back, or give up loudly."""
        attempts = held.attempts + 1
        age_s = (now_ms() - held.created_ms) / 1000 if held.created_ms else 0.0
        if attempts >= MAX_DELIVERY_ATTEMPTS or age_s > self._ttl_s:
            logger.error(
                "standin: giving up on delivering outbound call %s after %d attempts",
                _safe_name(held.call_id),
                attempts,
            )
            return
        self._finalized.discard(held.call_id)
        self._pending.park(replace(held, attempts=attempts))

    # ---- ringing somebody back -------------------------------------------

    def remember_chat_sender(self, message: InboundMessage) -> None:
        """Record who last wrote in this conversation, from the message itself.

        The ONLY place a callback target comes from. Never from message text,
        and never from a tool parameter: an agent that can be told who to ring
        can be told to ring anybody.
        """
        if not message.sender_aad_id:
            return
        self._senders[message.conversation_id] = _ChatSenderRecord(
            user_object_id=message.sender_aad_id,
            tenant_id=message.tenant_id,
            display_name=message.sender_name or "",
            at_ms=now_ms(),
        )

    def chat_callback_target(self, conversation_id: str) -> ChatCallbackTarget | str:
        """Who to ring for this conversation, or a sentence saying why not.

        A sentence rather than an exception: the caller is a tool result that a
        model reads out loud.
        """
        record = self._senders.get(conversation_id)
        if record is None:
            return "I do not know who to call for this conversation."
        if (now_ms() - record.at_ms) / 1000 > CHAT_CALLBACK_WINDOW_S:
            return "That was a while ago. Ask me again and I can call you."
        return ChatCallbackTarget(
            user_object_id=record.user_object_id,
            tenant_id=record.tenant_id,
            conversation_id=conversation_id,
            display_name=record.display_name,
        )

    # ---- the loop --------------------------------------------------------

    def start(self) -> None:
        """Begin sweeping. Idempotent."""
        if self._sweeper is None:
            self._sweeper = asyncio.ensure_future(self._sweep_loop())

    async def aclose(self) -> None:
        """Stop sweeping. Anything parked stays parked."""
        if self._sweeper is not None:
            self._sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sweeper
            self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweep_interval_s)
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("standin: the outbound sweep failed")
