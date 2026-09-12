# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What somebody attached to a chat message, turned into something an agent can use.

A Microsoft Teams message can carry a pasted screenshot, a dragged-in file, or a
voice note. Without this the handler gets :attr:`InboundMessage.attachments` as
raw dictionaries, so the best it can do is read a JSON blob to a model, and the
worst is answer a message about a picture as though nothing had been sent.

:func:`build_chat_turn` is the whole thing in one call: the text, the images
fetched and ready to hand to a vision model, a voice note transcribed, and a
plain sentence naming anything that could not be read.

This is deliberately NOT in :mod:`standin.chat`. That module owns the socket,
the duplicate check and the per-conversation ordering, and none of it changes
here. Fetching is optional work that must never be able to wedge the transport,
so it lives beside it: a handler that fetches nothing pays nothing.

Every fetch is pinned to the one origin the messages themselves arrived from,
and fails CLOSED. An attachment URL is signed, but not by us and not for us: it
arrives inside a message somebody else wrote, and the pin is the only thing
bounding where this worker can be told to go.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .chat import DEFAULT_CHAT_URL, InboundMessage
from .log import logger

__all__ = [
    "CLIP_FETCH_ATTEMPTS",
    "CLIP_FETCH_TIMEOUT_S",
    "IMAGE_FETCH_ATTEMPTS",
    "IMAGE_FETCH_TIMEOUT_S",
    "MAX_CLIPS",
    "MAX_CLIP_BYTES",
    "MAX_IMAGES",
    "MAX_IMAGE_BYTES",
    "ChatAudio",
    "ChatImage",
    "ChatTurn",
    "Transcriber",
    "attachment_origin",
    "attachments_note",
    "build_chat_turn",
    "card_action_note",
    "fetch_chat_audio",
    "fetch_chat_images",
    "spool_clip",
    "transcribe_voice_messages",
]

#: What one image may weigh. Matches the per-attachment ceiling the relay itself
#: applies, so a larger local number could never be reached anyway.
MAX_IMAGE_BYTES = 4 * 1024 * 1024

#: Images kept from one message. Each becomes a base64 blob in front of a model.
MAX_IMAGES = 4

#: How long one image has to arrive.
IMAGE_FETCH_TIMEOUT_S = 10.0

#: How many images may be REQUESTED, whatever the outcome. The accept cap counts
#: only successes, so a message naming fifty attachments that all time out still
#: costs fifty timeouts and blows the turn budget. This makes the worst case
#: arithmetic: eight tries at ten seconds.
IMAGE_FETCH_ATTEMPTS = 8

#: A voice note is minutes of audio, so it gets a bigger budget than a picture.
MAX_CLIP_BYTES = 16 * 1024 * 1024
MAX_CLIPS = 2
CLIP_FETCH_TIMEOUT_S = 20.0
CLIP_FETCH_ATTEMPTS = 4

#: Read size. The cap has to hold WHILE reading or it is not a memory bound.
READ_CHUNK_BYTES = 64 * 1024

#: A card's submit payload is model input, and a card is something this agent
#: sent, so this bounds our own template rather than a stranger's message.
CARD_PAYLOAD_MAX_CHARS = 4096

#: Lines in the "what was attached" note.
ATTACHMENT_NOTE_MAX_LINES = 10

#: Extensions that make a relayed FILE worth trying as an image. A pasted
#: screenshot arrives as an image; the same file dragged in from disk arrives as
#: a file whose declared type is a bare extension, so gating on the kind alone
#: makes an attached picture invisible while the note says one was sent.
_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "heif")

#: The same for a voice note, which also arrives as a file in practice.
_AUDIO_EXTENSIONS = (
    "wav", "mp3", "m4a", "mp4", "ogg", "oga", "opus", "aac", "amr", "webm", "mov", "3gp",
)  # fmt: skip

_SCHEME_FOR_FETCH = {"ws": "http", "wss": "https", "http": "http", "https": "https"}
_DEFAULT_PORT = {"http": 80, "https": 443}


@dataclass(frozen=True)
class ChatImage:
    """One picture from a message, ready for a vision model."""

    data_base64: str
    mime: str
    name: str | None = None
    size_bytes: int = 0

    @property
    def data(self) -> bytes:
        return base64.b64decode(self.data_base64)

    @property
    def data_url(self) -> str:
        """The ``data:`` form most vision APIs take directly."""
        return f"data:{self.mime};base64,{self.data_base64}"


@dataclass(frozen=True)
class ChatAudio:
    """One voice note, as bytes, before anything has transcribed it."""

    data: bytes
    mime: str
    name: str = ""


@dataclass(frozen=True)
class ChatTurn:
    """One inbound message, assembled into what an agent is actually asked."""

    query: str
    """The text to put in front of the model, including every note below."""

    images: list[ChatImage] = field(default_factory=list)
    voice_note: str = ""
    attachment_note: str = ""


#: Turn a voice note into words. Supplied by the handler or a speech plugin: the
#: core ships none and reads no provider key.
Transcriber = Callable[[bytes, str], Awaitable[str]]

#: Opens one URL. Injected so every edge here is testable without a socket.
Opener = Callable[..., Any]


def attachment_origin(url: str | None = None) -> str | None:
    """The one origin attachments may be fetched from, or ``None``.

    Derived from the chat channel's own URL rather than configured separately,
    so it is right by construction for a self-hosted or local gateway and there
    is no second setting to get wrong.

    ``None`` means fetch nothing. That is the safe direction: an unset origin
    read as "anywhere" turns a configuration typo into a fetcher that a message
    can point at any address it likes.
    """
    raw = url if url is not None else (os.environ.get("STANDIN_CHAT_URL") or DEFAULT_CHAT_URL)
    raw = raw.strip()
    parts = urlsplit(raw)
    scheme = _SCHEME_FOR_FETCH.get(parts.scheme.lower())
    if scheme is None or not parts.hostname:
        return None
    port = parts.port
    if port is None or port == _DEFAULT_PORT[scheme]:
        return f"{scheme}://{parts.hostname.lower()}"
    return f"{scheme}://{parts.hostname.lower()}:{port}"


def _same_origin(url: str, origin: str | None) -> bool:
    """Whether this URL is the origin we were told about. Fails closed."""
    if not origin:
        return False
    want, got = urlsplit(origin), urlsplit(url)
    if not got.hostname or got.scheme.lower() not in ("http", "https"):
        return False

    def key(parts: Any) -> tuple[str, str, int]:
        scheme = parts.scheme.lower()
        return (scheme, parts.hostname.lower(), parts.port or _DEFAULT_PORT.get(scheme, 0))

    return key(want) == key(got)


def _looks_like(name: str, content_type: str, extensions: Sequence[str]) -> bool:
    declared = content_type.strip().lower()
    if declared in extensions:
        return True
    suffix = name.rsplit(".", 1)[-1].strip().lower() if "." in name else ""
    return suffix in extensions


def _candidates(
    attachments: Sequence[Mapping[str, Any]], kind: str, extensions: Sequence[str]
) -> list[tuple[int, Mapping[str, Any]]]:
    """Attachments worth a request, in wire order."""
    out = []
    for index, item in enumerate(attachments):
        if not isinstance(item, Mapping) or item.get("relayable") is False:
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        item_kind = str(item.get("kind") or "")
        if item_kind == kind or (
            item_kind == "file"
            and _looks_like(
                str(item.get("name") or ""), str(item.get("contentType") or ""), extensions
            )
        ):
            out.append((index, item))
    return out


def _resolve_mime(declared: str, header: str) -> str:
    """The real media type.

    What the RESPONSE said wins. The declared value arrived inside the message,
    which somebody else wrote, so letting it decide would let a message claim
    ``image/png`` for a page of HTML and have it read as a picture.

    The declared value is the fallback, and only when it IS a media type: a
    relayed file declares a bare extension, and taking that literally fails
    every ``audio/`` check and spools the bytes under a nonsense name.
    """
    for candidate in (header, declared):
        value = str(candidate or "").split(";")[0].strip().lower()
        if "/" in value:
            return value
    return ""


def _header(headers: Any, name: str) -> str:
    """One header, case-insensitively, from a real response or a test double."""
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
        if value is None:
            for key, candidate in dict(headers).items():
                if str(key).lower() == name.lower():
                    value = candidate
                    break
    except Exception:
        return ""
    return str(value or "")


@contextlib.asynccontextmanager
async def _open(url: str, timeout_s: float) -> Any:
    """Fetch one URL, following no redirects.

    The URL is same-origin and signed, so a redirect off it is already
    anomalous, and following one reopens the door the origin pin just closed:
    the pin is checked on the URL in the message, not on wherever a 302 points.
    """
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as response:
            yield response


async def _read_capped(response: Any, max_bytes: int) -> bytes | None:
    """The body, or ``None`` when it is too big.

    Checked while reading, not after. A content-length that lies, or is simply
    absent, otherwise gets to allocate whatever it likes before a later check
    objects.
    """
    declared = _header(getattr(response, "headers", {}), "content-length")
    if declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _fetch(
    attachments: Sequence[Mapping[str, Any]],
    *,
    kind: str,
    extensions: Sequence[str],
    prefixes: tuple[str, ...],
    origin: str | None,
    max_bytes: int,
    max_items: int,
    max_attempts: int,
    timeout_s: float,
    get: Opener | None,
) -> list[tuple[int, bytes, str, str]]:
    """The shared fetch loop. Never raises, and returns what it got."""
    opener = get or _open
    taken: list[tuple[int, bytes, str, str]] = []
    attempts = 0
    for index, item in _candidates(attachments, kind, extensions):
        if len(taken) >= max_items or attempts >= max_attempts:
            break
        url = str(item["url"])
        if not _same_origin(url, origin):
            logger.warning("standin: refusing an attachment from another origin")
            continue
        attempts += 1
        try:
            async with opener(url, timeout_s) as response:
                status = int(getattr(response, "status", 0))
                if not 200 <= status < 300:
                    continue
                mime = _resolve_mime(
                    str(item.get("contentType") or ""),
                    _header(getattr(response, "headers", {}), "content-type"),
                )
                # Judged BEFORE a byte is read: an error page would otherwise be
                # base64'd in front of a model as though it were a picture.
                if not mime.startswith(prefixes):
                    continue
                body = await _read_capped(response, max_bytes)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: could not fetch an attachment: %s", err)
            continue
        if body:
            taken.append((index, body, mime, str(item.get("name") or "")))
    return taken


async def fetch_chat_images(
    attachments: Sequence[Mapping[str, Any]],
    *,
    origin: str | None,
    max_bytes: int = MAX_IMAGE_BYTES,
    max_images: int = MAX_IMAGES,
    timeout_s: float = IMAGE_FETCH_TIMEOUT_S,
    get: Opener | None = None,
) -> list[ChatImage]:
    """Pictures from a message, ready to put in front of a vision model.

    Best-effort per attachment: one that will not load costs that attachment,
    never the answer.
    """
    got = await _fetch(
        attachments,
        kind="image",
        extensions=_IMAGE_EXTENSIONS,
        prefixes=("image/",),
        origin=origin,
        max_bytes=max_bytes,
        max_items=max_images,
        max_attempts=IMAGE_FETCH_ATTEMPTS,
        timeout_s=timeout_s,
        get=get,
    )
    return [
        ChatImage(
            data_base64=base64.b64encode(body).decode("ascii"),
            mime=mime,
            name=name or None,
            size_bytes=len(body),
        )
        for _, body, mime, name in got
    ]


async def fetch_chat_audio(
    attachments: Sequence[Mapping[str, Any]],
    *,
    origin: str | None,
    max_bytes: int = MAX_CLIP_BYTES,
    max_clips: int = MAX_CLIPS,
    timeout_s: float = CLIP_FETCH_TIMEOUT_S,
    get: Opener | None = None,
) -> list[ChatAudio]:
    """Voice notes from a message, as bytes.

    ``video/`` is accepted as well as ``audio/``: some clients label a voice or
    video note with a container type that speech-to-text reads perfectly well.
    """
    got = await _fetch(
        attachments,
        kind="audio",
        extensions=_AUDIO_EXTENSIONS,
        prefixes=("audio/", "video/"),
        origin=origin,
        max_bytes=max_bytes,
        max_items=max_clips,
        max_attempts=CLIP_FETCH_ATTEMPTS,
        timeout_s=timeout_s,
        get=get,
    )
    return [ChatAudio(data=body, mime=mime, name=name) for _, body, mime, name in got]


async def transcribe_voice_messages(
    attachments: Sequence[Mapping[str, Any]],
    *,
    transcribe: Transcriber | None,
    origin: str | None,
    get: Opener | None = None,
    **caps: Any,
) -> str:
    """What the voice notes said, as one block of text.

    Empty when there are none, when no transcriber was supplied, or when every
    one failed. A transcriber that raises costs that clip and nothing else.
    """
    if transcribe is None:
        return ""
    said: list[str] = []
    for clip in await fetch_chat_audio(attachments, origin=origin, get=get, **caps):
        try:
            text = (await transcribe(clip.data, clip.mime)).strip()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            logger.warning("standin: could not transcribe a voice message: %s", err)
            continue
        if text:
            said.append(text)
    return "\n".join(said)


def attachments_note(
    attachments: Sequence[Mapping[str, Any]], *, status: Mapping[int, str] | None = None
) -> str:
    """A plain sentence naming what came with the message.

    Worth saying even when nothing could be read: a model that is told a picture
    was attached and could not be opened says something useful, and a model told
    nothing answers as if the message were empty.
    """
    if not attachments:
        return ""
    marks = status or {}
    lines: list[str] = []
    for index, item in enumerate(attachments):
        if len(lines) >= ATTACHMENT_NOTE_MAX_LINES:
            lines.append(f"and {len(attachments) - len(lines)} more")
            break
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip() or "an unnamed file"
        kind = str(item.get("kind") or "file").strip() or "file"
        mark = marks.get(index)
        suffix = f" ({mark})" if mark else ""
        lines.append(f"- {name} [{kind}]{suffix}")
    if not lines:
        return ""
    return "[Attached to this message]\n" + "\n".join(lines)


def card_action_note(card_action: Mapping[str, Any] | None) -> str:
    """What a button press on one of this agent's own cards submitted.

    A card message arrives with EMPTY text, so without this the agent is asked
    nothing at all and answers as though the person said nothing.
    """
    if not card_action:
        return ""
    import json

    try:
        payload = json.dumps(card_action, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        payload = str(card_action)
    return "[The person used a button on your card]\n" + payload[:CARD_PAYLOAD_MAX_CHARS]


async def build_chat_turn(
    message: InboundMessage,
    *,
    origin: str | None = None,
    images: bool = True,
    transcribe: Transcriber | None = None,
    get: Opener | None = None,
) -> ChatTurn:
    """One inbound message, assembled into what to ask an agent.

    The order is fixed, and it is the order a person would say it in: what they
    typed, what they pressed, what they said out loud, then what they attached::

        turn = await standin.build_chat_turn(message, transcribe=my_stt)
        answer = await agent.ask(turn.query, images=[i.data_url for i in turn.images])

    Never raises. Anything that will not load is named in the note rather than
    failing the turn.
    """
    where = origin if origin is not None else attachment_origin()
    attachments = message.attachments or []

    fetched = await fetch_chat_images(attachments, origin=where, get=get) if images else []
    voice = await transcribe_voice_messages(
        attachments, transcribe=transcribe, origin=where, get=get
    )

    # Which ones actually made it, so the note can say so rather than implying
    # the model has seen something it has not.
    readable = {image.name for image in fetched if image.name}
    marks = {
        index: ("attached" if str(item.get("name") or "") in readable else "unreadable")
        for index, item in enumerate(attachments)
        if isinstance(item, Mapping)
    }

    card = card_action_note(message.card_action)
    note = attachments_note(attachments, status=marks)
    spoken = f"[They sent a voice message]\n{voice}" if voice else ""

    query = "\n\n".join(part for part in (message.text.strip(), card, spoken, note) if part)
    return ChatTurn(query=query, images=fetched, voice_note=voice, attachment_note=note)


@contextlib.contextmanager
def spool_clip(clip: ChatAudio, directory: str | Path | None = None) -> Iterator[str]:
    """Put a voice note on disk for an engine that only takes a path.

    Removed on the way out, on every path. A transcription engine is handed
    somebody's voice, and leaving it in a temporary directory is a copy nobody
    decided to keep.
    """
    suffix = "." + (clip.mime.rsplit("/", 1)[-1] or "bin")
    handle = tempfile.NamedTemporaryFile(
        suffix=suffix, dir=str(directory) if directory else None, delete=False
    )
    try:
        handle.write(clip.data)
        handle.close()
        yield handle.name
    finally:
        with contextlib.suppress(OSError):
            os.unlink(handle.name)
