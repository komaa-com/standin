# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The messages lane: Microsoft Teams chat, without a bot credential.

Managed connections only. StandIn owns the Microsoft Teams bot, authenticates the
activity, resolves it to your connection and strips the bot @mention. Your
handler returns text and StandIn performs the Microsoft Teams send, so your agent never
holds a Bot Framework credential.

Same shape as the call lane: the worker dials OUT and StandIn pushes messages
down that socket, so there is no listener, no port to expose and no tunnel.

    Microsoft Teams message
         |
         v
    StandIn gateway        (authenticates, normalizes, signs)
         |   pushed down the worker's outbound socket
         v
    ChatChannel            (this class)
         |   your async handler returns reply text
         v
    back up the same socket; StandIn sends it to Microsoft Teams
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from ._exceptions import StandInError
from ._hmac import SIGNATURE_HEADER, TIMESTAMP_HEADER, now_ms, sign_handshake
from .log import logger

#: chat-schema.yaml SCHEMA_VERSION. A MAJOR version: additive evolution does not
#: bump it, because the schema already requires receivers to ignore unknown
#: fields. An integer above ours therefore means incompatible semantics.
SCHEMA_VERSION = 1

DEFAULT_CHAT_URL = "wss://teams.standin.komaa.com/api/chat/channel"

#: What may be rendered inline in a reply. Raster only, and the decoded bytes
#: must actually carry the type's signature.
#:
#: ``image/svg+xml`` is deliberately absent and is not a gap to fill later. SVG
#: is scriptable XML, which is precisely what "an image" must not be.
OUTBOUND_IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

#: What an inline image may weigh, decoded.
OUTBOUND_IMAGE_MAX_BYTES = 1024 * 1024

#: The first bytes each allowed type must begin with. A declared type is a claim;
#: this is the check. Without it an HTML page or an SVG labelled image/png is
#: posted under this bot's name.
_IMAGE_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}

#: A filename reaches a chat as a download. One path-free segment, bounded.
_MAX_IMAGE_NAME_CHARS = 200


@dataclass(frozen=True)
class InboundMessage:
    """One user message, already authenticated and resolved to your connection.

    Reserved bot commands are handled by StandIn and never arrive here. In group
    and channel scope only messages that @mention the bot are relayed, and the
    mention is already stripped from ``text``.
    """

    tenant_id: str
    conversation_id: str
    activity_id: str
    scope: str
    text: str
    sender_name: str | None = None
    sender_aad_id: str | None = None
    sender_is_guest: bool = False
    sender_is_linked_owner: bool = False
    attachments: list[dict[str, Any]] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    locale: str | None = None
    #: Submit payload of an Action.Submit on a card this agent sent. ``text`` is
    #: empty on these messages.
    card_action: dict[str, Any] | None = None
    #: Which StandIn connection this conversation resolved to. Stable for a
    #: tenant, and :func:`build_reply` echoes it: one tenant can have several
    #: connections, so the tenant alone no longer says who a reply is from.
    binding_id: str | None = None

    @property
    def is_personal(self) -> bool:
        return self.scope == "personal"


def parse_inbound(body: str) -> InboundMessage:
    """Parse and validate an inbound message. Raises ValueError naming the
    problem; the caller maps that to HTTP 400."""
    try:
        raw = json.loads(body)
    except ValueError as exc:
        raise ValueError("malformed json") from exc
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    for key in ("tenantId", "conversationId", "activityId"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise ValueError(f"{key} is required")
    version = raw.get("schemaVersion", SCHEMA_VERSION)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schemaVersion {version} (this plugin speaks {SCHEMA_VERSION})"
        )
    raw_sender = raw.get("sender")
    sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
    scope = raw.get("scope")
    return InboundMessage(
        tenant_id=raw["tenantId"],
        conversation_id=raw["conversationId"],
        activity_id=raw["activityId"],
        # ChatScope is an OPEN enum: an unknown value relays as a group chat
        # rather than being rejected.
        scope=scope if isinstance(scope, str) and scope else "personal",
        text=raw["text"] if isinstance(raw.get("text"), str) else "",
        sender_name=sender.get("displayName"),
        sender_aad_id=sender.get("aadObjectId"),
        sender_is_guest=bool(sender.get("isGuest", False)),
        sender_is_linked_owner=bool(sender.get("isLinkedOwner", False)),
        attachments=raw["attachments"] if isinstance(raw.get("attachments"), list) else [],
        mentions=raw["mentions"] if isinstance(raw.get("mentions"), list) else [],
        locale=raw.get("locale") if isinstance(raw.get("locale"), str) else None,
        card_action=raw.get("cardAction") if isinstance(raw.get("cardAction"), dict) else None,
        binding_id=raw.get("bindingId") if isinstance(raw.get("bindingId"), str) else None,
    )


#: The clock, named apart from the ``now_ms`` argument on
#: :meth:`PersonalChats.for_caller` that would otherwise shadow it.
_clock = now_ms

#: How long a remembered personal chat stays usable as a delivery target. A day
#: of work is the unit here: somebody who messaged the bot this morning and calls
#: it this afternoon is plainly the same person in the same working context, and
#: a record older than that is a guess about who is on the phone.
CHAT_FALLBACK_WINDOW_MS = 12 * 60 * 60 * 1000

#: Senders remembered at once, oldest evicted. A tenant with many users must not
#: grow this without bound inside a worker that is also carrying live audio.
_MAX_REMEMBERED_SENDERS = 512


@dataclass(frozen=True)
class PersonalChat:
    """Somebody's one-to-one conversation with this bot, as last seen.

    The only honest source for "where do I send this person something": a 1:1
    CALL carries no thread id that can be posted into, so the conversation has to
    come from a message that person actually sent.
    """

    conversation_id: str
    tenant_id: str
    aad_id: str
    display_name: str
    at_ms: int


class PersonalChats:
    """Who has messaged this bot privately, so a call can answer them back.

    Feed it from the chat lane and ask it from the call lane. Both lanes have to
    be in one process for that to work; across processes this needs a shared
    store, and until there is one a caller who has not messaged the bot inside
    the window simply has no personal target.

    Example:
        ```python
        chats = standin.PersonalChats()
        chat = standin.ChatChannel(respond=on_message, chats=chats)
        # later, on a call:
        target = chats.for_caller(caller_aad_id=caller.aad_id, tenant_id=session.tenant_id)
        ```
    """

    def __init__(self, window_ms: int = CHAT_FALLBACK_WINDOW_MS) -> None:
        self._window_ms = window_ms
        #: Keyed by tenant and person, in order of last seen, and bounded. A
        #: sender with no directory id is kept under an empty one rather than in
        #: a second unbounded index, so a worker serving many tenants cannot
        #: grow a record it never forgets.
        self._by_sender: OrderedDict[tuple[str, str], PersonalChat] = OrderedDict()

    def remember(self, message: InboundMessage, at_ms: int | None = None) -> None:
        """Record a personal message's conversation. Anything else is ignored.

        Scope decides this, and ONLY scope. A conversation id prefix looks like
        it would do the same job and does the opposite: a personal chat with a
        bot is addressed ``a:1...`` while ``19:...`` is precisely the group and
        channel shape this has to exclude, so an id test rejects every real
        personal chat and admits nothing. Without the scope test, an @mention in
        a team channel would make that channel somebody's "personal" chat and put
        their private escalation in front of their team.
        """
        if message.scope != "personal":
            return
        conversation_id = (message.conversation_id or "").strip()
        tenant_id = (message.tenant_id or "").strip()
        # A record missing either half addresses nothing, and a target that
        # cannot be addressed is worse than none: the post fails and reads as a
        # chat that refused the minutes.
        if not conversation_id or not tenant_id:
            return
        chat = PersonalChat(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            aad_id=(message.sender_aad_id or "").strip(),
            display_name=message.sender_name or "",
            at_ms=at_ms if at_ms is not None else _clock(),
        )
        key = (chat.tenant_id, chat.aad_id)
        # Re-inserted rather than overwritten: the insertion order is what
        # decides who is forgotten first.
        self._by_sender.pop(key, None)
        self._by_sender[key] = chat
        while len(self._by_sender) > _MAX_REMEMBERED_SENDERS:
            self._by_sender.popitem(last=False)

    def for_caller(
        self,
        *,
        caller_aad_id: str | None,
        tenant_id: str,
        now_ms: int | None = None,
        allow_unidentified: bool = False,
    ) -> PersonalChat | None:
        """The chat this caller may be sent call content in, or ``None``.

        Four rules, all of which must hold. The conversation was recorded from a
        message whose scope was personal; it belongs to the tenant this worker is
        bound to; it was seen inside the recency window; and the call names its
        caller, whose directory id is the remembered sender's. Posting call
        content into the wrong conversation is the failure this exists to
        prevent, and each rule removes one way of getting there.

        Args:
            caller_aad_id: the caller's directory id, from the call.
            tenant_id: the tenant this worker is bound to. Never the caller's own
                tenant, which is absent or foreign for a guest.
            now_ms: the clock, for tests.
            allow_unidentified: for a single-operator install, let an anonymous
                caller reach the last person who messaged this bot in this
                tenant. Default off, and warned by name every time it is used:
                with it on, every anonymous caller collapses onto whoever chatted
                last.
        """
        tenant = (tenant_id or "").strip()
        if not tenant:
            return None
        named = (caller_aad_id or "").strip()
        if named:
            chat = self._by_sender.get((tenant, named))
        elif allow_unidentified:
            logger.warning(
                "standin: allow_unidentified is on; addressing an unidentified caller as the "
                "last person who messaged this bot in tenant %s",
                tenant,
            )
            chat = self._newest_in(tenant, now_ms)
        else:
            # A call that identifies nobody has no conversation that can be
            # asserted as theirs, so it gets none.
            return None
        if chat is None or chat.tenant_id != tenant:
            return None
        return chat if self._fresh(chat, now_ms) else None

    def _newest_in(self, tenant: str, now_ms: int | None) -> PersonalChat | None:
        best: PersonalChat | None = None
        for chat in self._by_sender.values():
            if chat.tenant_id != tenant or not self._fresh(chat, now_ms):
                continue
            if best is None or chat.at_ms >= best.at_ms:
                best = chat
        return best

    def _fresh(self, chat: PersonalChat, now_ms: int | None) -> bool:
        at = now_ms if now_ms is not None else _clock()
        return at - chat.at_ms <= self._window_ms


@dataclass(frozen=True)
class OutboundImage:
    """A picture to render inline in a reply, carried as bytes.

    Bytes rather than a link, because a link is a beacon: an off-domain image
    loads with no click, under this bot's name, and what it serves can be
    swapped after anyone looked at it. Bytes can be checked, and
    :func:`outbound_image` checks them.
    """

    content_type: str
    content_base64: str
    name: str | None = None

    def as_json(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "contentType": self.content_type,
            "contentBase64": self.content_base64,
        }
        if self.name:
            wire["name"] = self.name
        return wire


def sanitize_image_name(name: str | None) -> str | None:
    """One path-free segment, bounded, or ``None``.

    A filename reaches a chat as a download under this bot's identity, and the
    model that chose it is being steered by whoever is in the conversation.
    """
    if not name:
        return None
    segment = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    segment = "".join(ch for ch in segment if ch.isprintable() and ch not in '<>:"|?*').strip()
    if not segment or segment in (".", ".."):
        return None
    if len(segment) > _MAX_IMAGE_NAME_CHARS:
        stem, _, extension = segment.rpartition(".")
        keep = _MAX_IMAGE_NAME_CHARS - len(extension) - 1
        segment = (
            f"{stem[:keep]}.{extension}"
            if extension and keep > 0
            else segment[:_MAX_IMAGE_NAME_CHARS]
        )
    return segment


def sniff_image_type(data: bytes) -> str | None:
    """What these bytes actually are, or ``None``."""
    for content_type, signatures in _IMAGE_MAGIC.items():
        if any(data.startswith(signature) for signature in signatures):
            # A RIFF container is only a webp when it says so.
            if content_type == "image/webp" and data[8:12] != b"WEBP":
                continue
            return content_type
    return None


def outbound_image(data: bytes | str, content_type: str, name: str | None = None) -> OutboundImage:
    """Check a picture and build the wire form. Raises :class:`ValueError`.

    Three checks, and the second is the one that matters. A declared type is a
    claim made by whatever produced the bytes; the signature is what they are.
    Without it an HTML document or an SVG labelled ``image/png`` is posted into
    somebody's chat under this bot's name.
    """
    raw = base64.b64decode(data, validate=True) if isinstance(data, str) else bytes(data)
    declared = content_type.strip().lower()
    # A common spelling that is not a media type.
    if declared == "image/jpg":
        declared = "image/jpeg"
    if declared not in OUTBOUND_IMAGE_CONTENT_TYPES:
        raise ValueError(
            f"{content_type!r} cannot be sent inline; it must be one of "
            f"{', '.join(OUTBOUND_IMAGE_CONTENT_TYPES)}"
        )
    if len(raw) > OUTBOUND_IMAGE_MAX_BYTES:
        raise ValueError(
            f"that image is {len(raw)} bytes, over the {OUTBOUND_IMAGE_MAX_BYTES} limit"
        )
    actual = sniff_image_type(raw)
    if actual != declared:
        raise ValueError(
            f"those bytes are not {declared}: they look like {actual or 'something else'}"
        )
    return OutboundImage(
        content_type=declared,
        content_base64=base64.b64encode(raw).decode("ascii"),
        name=sanitize_image_name(name),
    )


def build_reply(
    message: InboundMessage,
    text: str,
    kind: str = "message",
    image: OutboundImage | None = None,
) -> dict[str, Any]:
    """The gateway-bound reply.

    tenantId and conversationId echo the inbound EXACTLY: a mismatch is rejected,
    and that check is the cross-tenant leak guard the whole relay rests on.
    bindingId echoes for the same reason one level down, between connections
    inside one tenant.
    """
    reply: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "tenantId": message.tenant_id,
        "conversationId": message.conversation_id,
        "replyToId": message.activity_id,
        "kind": kind,
        "idempotencyKey": f"{message.activity_id}:{kind}",
    }
    if message.binding_id:
        # Which connection this reply is FROM. One tenant can have several, so
        # the tenant alone no longer identifies the sender.
        reply["bindingId"] = message.binding_id
    if kind != "typing":
        reply["text"] = text
        # A typing indicator carries neither: it is a state, not a message.
        if image is not None:
            reply["image"] = image.as_json()
    return reply


class _Seen:
    """At-least-once dedupe on the schema's activityId idempotency key. Bounded
    LRU: an aged-out redelivery running again is acceptable at-least-once
    behaviour, a fresh double-run is not."""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def mark_first(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


class ChatChannel:
    """Answer Microsoft Teams messages with your agent.

    Dialed out from the worker, like the call lane, so nothing listens and there
    is nothing to expose. Managed connections only, and that needs no flag: the
    socket authenticates with your connection secret, so if it opens at all you
    are managed.

    Args:
        respond: async callable taking an :class:`InboundMessage` and returning
            the reply text. An empty string makes the channel say so rather than
            leaving the user watching a typing indicator forever.
        secret: the key this lane signs with, defaulting to
            ``STANDIN_CHAT_SECRET`` and then to ``STANDIN_SECRET``.

            A managed deployment issues a SECOND key for chat. That is
            deliberate and worth keeping: the voice lane signs a WebSocket
            handshake and the chat lane signs an HTTP body, so a key that can
            forge one cannot forge the other. Setting the chat key is also what
            turns the lane on, which is why there is no enable flag.
        url: the chat channel URL, defaulting to ``STANDIN_CHAT_URL``.
        chats: a :class:`PersonalChats` to feed. Every personal message that
            arrives is remembered in it, which is what later lets a call post
            back to the caller who sent one.
        listen_only: take messages without answering them. ``chats`` is still
            fed, so a call can find where to post its minutes, but ``respond``
            is never called and nothing is sent back, not even the typing
            indicator. For a lane that exists only to post, such as a meeting
            recap, when something else already answers this connection's chat.

    Example:
        ```python
        async def on_message(msg: standin.InboundMessage) -> str:
            return f"You said: {msg.text}"


        chat = standin.ChatChannel(respond=on_message)
        await chat.start()
        ```
    """

    #: Serialization means a hung turn would wedge its conversation forever, so
    #: every turn is bounded. Generous: agent turns legitimately run long.
    TURN_TIMEOUT_S = 300.0

    def __init__(
        self,
        *,
        respond: Callable[[InboundMessage], Awaitable[str]],
        secret: str | None = None,
        url: str | None = None,
        chats: PersonalChats | None = None,
        listen_only: bool = False,
    ) -> None:
        self._secret = (
            secret
            or os.environ.get("STANDIN_CHAT_SECRET", "")
            or os.environ.get("STANDIN_SECRET", "")
        )
        if not self._secret:
            raise StandInError(
                "a secret is required for the chat lane: pass secret=..., or set "
                "STANDIN_CHAT_SECRET (a managed deployment issues a separate key for chat), "
                "or STANDIN_SECRET to use one key for both lanes"
            )
        self._respond = respond
        self._chats = chats
        self._listen_only = bool(listen_only)
        self._url = url or os.environ.get("STANDIN_CHAT_URL") or DEFAULT_CHAT_URL
        self._seen = _Seen()
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        #: Per-conversation chains. The schema promises per-conversation
        #: ORDERING; independent tasks would let replies overtake each other.
        self._chains: dict[str, asyncio.Task[Any]] = {}
        self._closed = False

    async def start(self) -> None:
        """Dial StandIn and begin taking messages."""
        timestamp = now_ms()
        http = aiohttp.ClientSession()
        try:
            self._ws = await http.ws_connect(
                self._url,
                headers={
                    TIMESTAMP_HEADER: str(timestamp),
                    SIGNATURE_HEADER: sign_handshake(self._secret, timestamp, "chat"),
                },
                max_msg_size=2 * 1024 * 1024,
            )
        except BaseException:
            await http.close()
            raise
        self._http = http
        self._task = asyncio.ensure_future(self._run())
        logger.info("standin: chat channel open")

    async def aclose(self) -> None:
        self._closed = True
        for task in [self._task, *self._tasks]:
            if task is not None:
                task.cancel()
        pending = [t for t in (self._task, *self._tasks) if t is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._chains.clear()
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()

    async def _run(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    inbound = parse_inbound(message.data)
                except ValueError as err:
                    logger.warning("standin: dropping malformed chat message: %s", err)
                    continue
                # Dedupe first: StandIn is at-least-once, and a redelivery must
                # not start a second turn for the same activity.
                key = f"{inbound.tenant_id}:{inbound.conversation_id}:{inbound.activity_id}"
                if self._seen.mark_first(key):
                    # Remembered behind the dedupe, so a redelivery of this
                    # morning's message does not make it look like the sender
                    # messaged just now. The recency window is evidence about
                    # who is on the phone, and a repeat is not.
                    if self._chats is not None:
                        self._chats.remember(inbound)
                    # A listen-only lane remembers and says nothing, not even
                    # the typing indicator: that promises an answer it will
                    # not give.
                    if self._listen_only:
                        continue
                    self._enqueue(inbound)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: chat channel failed")

    def _enqueue(self, message: InboundMessage) -> None:
        chain_key = f"{message.tenant_id}:{message.conversation_id}"
        previous = self._chains.get(chain_key)

        async def run() -> None:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    # Distinguish the PREVIOUS turn being cancelled from THIS
                    # task being cancelled while parked on it. Swallowing our own
                    # cancellation would run a full turn during shutdown.
                    current = asyncio.current_task()
                    if (
                        current is None
                        or not hasattr(current, "cancelling")
                        or current.cancelling() > 0
                    ):
                        raise
                except Exception:
                    pass  # a failed turn must not dam the chain
            await self._process(message)

        task = asyncio.get_running_loop().create_task(run())
        self._chains[chain_key] = task
        self._tasks.add(task)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._tasks.discard(finished)
            if self._chains.get(chain_key) is finished:
                del self._chains[chain_key]

        task.add_done_callback(_done)

    async def _process(self, message: InboundMessage) -> None:
        # Typing is a courtesy, so it must not sit in FRONT of the turn. Send it
        # and let the agent think; the indicator still lands first.
        await self._send(build_reply(message, "", "typing"))
        try:
            text = await asyncio.wait_for(self._respond(message), timeout=self.TURN_TIMEOUT_S)
            if text and text.strip():
                await self._send(build_reply(message, text))
            else:
                # After a typing indicator, silence looks exactly like a hang.
                logger.warning("standin: chat handler returned an empty answer")
                await self._send(
                    build_reply(
                        message,
                        "I couldn't come up with an answer to that - try rephrasing, "
                        "or ask something else.",
                        "error",
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("standin: chat turn failed")
            await self._send(
                build_reply(
                    message, "Something went wrong answering that - please try again.", "error"
                )
            )

    async def send(
        self,
        *,
        tenant_id: str,
        conversation_id: str,
        text: str,
        image: OutboundImage | None = None,
        binding_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> bool:
        """Post into a Microsoft Teams conversation with no inbound message to answer.

        Useful from inside a call. Best-effort: returns False rather than
        raising, because a failed post must never break a live call.
        """
        payload: dict[str, Any] = {
            "schemaVersion": SCHEMA_VERSION,
            "tenantId": tenant_id,
            "conversationId": conversation_id,
            "kind": "message",
            "text": text,
        }
        if image is not None:
            payload["image"] = image.as_json()
        if binding_id:
            payload["bindingId"] = binding_id
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        return await self._send(payload)

    async def _send(self, reply: dict[str, Any]) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            logger.warning("standin: chat channel is not open; dropping a reply")
            return False
        try:
            await ws.send_str(json.dumps(reply, separators=(",", ":")))
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("standin: chat reply failed", exc_info=True)
            return False


__all__ = [
    "CHAT_FALLBACK_WINDOW_MS",
    "OUTBOUND_IMAGE_CONTENT_TYPES",
    "OUTBOUND_IMAGE_MAX_BYTES",
    "SCHEMA_VERSION",
    "ChatChannel",
    "InboundMessage",
    "OutboundImage",
    "PersonalChat",
    "PersonalChats",
    "build_reply",
    "outbound_image",
    "parse_inbound",
    "sanitize_image_name",
    "sniff_image_type",
]
