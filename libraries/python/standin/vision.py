# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The vision lane: what the caller shows you, and what you show back.

A Microsoft Teams call carries more than voice. StandIn samples the caller's
camera and their screen share and forwards single JPEG frames, and it will draw
an image you send onto the bot's own tile. This module is both halves of that:
:func:`parse_video_frame` reads what arrives, and :func:`display_image` builds
what goes back.

Frames arrive **sparsely and best-effort**. StandIn drops a frame rather than
queueing it when the socket is busy, so this is not a video stream and must not
be treated as one. The useful shape is the one every provider plugin ends
up with: keep the latest frame per source and send it to a vision model only
when something asks to look. :class:`~standin.CallSession` does that for you
through :meth:`~standin.CallSession.latest_video_frame`, so a plugin that
only wants on-demand vision implements no callback at all.

Take every frame instead - ambient vision, counting slides, watching a
whiteboard - by implementing
:meth:`~standin.CallHandler.on_video_frame`. Do the model call off the frame
loop if it is slow: the loop is suspended while your callback runs, which is
the same rule that governs :meth:`~standin.CallHandler.on_caller_audio`.

Frames are the caller's screen and face. Nothing here writes one to disk or
sends one anywhere: where a frame goes is the plugin's decision, and a
provider that persists uploads (ElevenLabs attaches frames to the stored
conversation) is the plugin's to gate on the call's recording status.

:class:`FrameDescriber` is the other way round, and the one most voice
providers need: a speech-to-speech model that hears but cannot see gets a
sentence of text instead of a picture. The frame goes to a vision model of your
choosing, transiently, and only the description comes back.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from ._protocol_runtime import encode
from .protocol import TYPE_DISPLAY_FRAME, TYPE_DISPLAY_IMAGE

__all__ = [
    "DISPLAY_IMAGE_MIME_TYPES",
    "MAX_IMAGE_BYTES",
    "VIDEO_SOURCES",
    "FrameDescriber",
    "VideoFrame",
    "display_frame",
    "display_image",
    "parse_video_frame",
]

#: The two things a caller can show: their camera, or their screen share.
VIDEO_SOURCES = ("camera", "screenshare")

#: What StandIn will draw on the bot tile. JPEG or PNG, nothing else.
DISPLAY_IMAGE_MIME_TYPES = ("image/jpeg", "image/png")

#: One wire message is bounded at 2 MB by both SDKs, and base64 costs a third
#: on top of the raw bytes. Refusing an oversized image here names the real
#: problem, rather than letting the service close the socket mid-call.
MAX_IMAGE_BYTES = 1_400_000


@dataclass(frozen=True)
class VideoFrame:
    """One sampled frame of what the caller is showing.

    ``participant_id`` and ``participant_name`` are best-effort and absent for
    guest and anonymous participants, so a group-call prompt that says who is
    sharing must tolerate not knowing.
    """

    source: str
    """Which lane this came from: ``"camera"`` or ``"screenshare"``."""

    ts: int
    """Capture time in milliseconds."""

    width: int
    """Pixel width, already downscaled by StandIn before sending."""

    height: int
    """Pixel height."""

    mime: str
    """Image MIME type. StandIn sends ``image/jpeg``."""

    data_base64: str
    """The image, base64-encoded, exactly as it arrived.

    Kept in this form because it is the form most providers want back: a
    ``data:`` URL for a vision model costs one f-string from here, while
    :attr:`data` costs a decode.
    """

    participant_id: str | None = None
    """Whose frame this is, when StandIn could tell."""

    participant_name: str | None = None
    """Display name matching :attr:`participant_id`."""

    @property
    def data(self) -> bytes:
        """The decoded image bytes, for an API that uploads a file.

        Decoded on each access rather than cached: frames arrive sparsely, a
        cache would double the memory a held frame costs, and the dataclass is
        frozen so that the frame handed to two plugins cannot be mutated
        by either.
        """
        return base64.b64decode(self.data_base64)

    @property
    def data_url(self) -> str:
        """The frame as a ``data:`` URL, which is what most vision models take."""
        return f"data:{self.mime};base64,{self.data_base64}"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _decode_strict(data_base64: str) -> bytes | None:
    """Decode canonical standard base64, or None.

    Re-encoding is what makes this agree with the TypeScript half. Python's
    decoder accepts nonzero padding bits that a re-encode rejects, and Node's
    Buffer silently discards what it cannot read, so comparing the round trip
    is the only check both languages can make identically. The same guard
    protects the audio lane in ``_protocol_runtime.decode_pcm``.
    """
    try:
        data = base64.b64decode(data_base64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data or base64.b64encode(data).decode("ascii") != data_base64:
        return None
    return data


def parse_video_frame(msg: dict[str, Any]) -> VideoFrame | None:
    """Read a ``video.frame``, or return ``None`` if it is unusable.

    Never raises. A frame that fails any check is a frame to drop: the call is
    healthy, the caller is still talking, and one malformed image is not worth
    ending a conversation over. That is the same leniency the rest of the wire
    contract is built on, where a receiver ignores what it cannot use.
    """
    source = _text(msg.get("source"))
    if source not in VIDEO_SOURCES:
        return None

    data_base64 = msg.get("dataBase64")
    if not isinstance(data_base64, str) or _decode_strict(data_base64) is None:
        # Proving the payload decodes is the only check that the bytes a
        # plugin is about to hand a provider really are the image that was
        # sent, rather than a truncated frame.
        return None

    width = _positive_int(msg.get("width"))
    height = _positive_int(msg.get("height"))
    if width is None or height is None:
        return None

    ts = msg.get("ts")
    return VideoFrame(
        source=source,
        ts=ts if isinstance(ts, int) and not isinstance(ts, bool) and ts >= 0 else 0,
        width=width,
        height=height,
        mime=_text(msg.get("mime")) or "image/jpeg",
        data_base64=data_base64,
        participant_id=_text(msg.get("participantId")),
        participant_name=_text(msg.get("participantName")),
    )


def _encode_image(image: bytes | str, mime: str, label: str) -> str:
    if mime not in DISPLAY_IMAGE_MIME_TYPES:
        raise ValueError(f"{label} mime must be one of {DISPLAY_IMAGE_MIME_TYPES}, got {mime!r}")
    if isinstance(image, str):
        # Already base64: measure the DECODED size, because that is what the
        # 2 MB envelope actually bounds.
        decoded = _decode_strict(image)
        if decoded is None:
            raise ValueError(f"{label} data is not valid base64")
        size = len(decoded)
        data_base64 = image
    else:
        size = len(image)
        data_base64 = base64.b64encode(image).decode("ascii")
    if size == 0:
        raise ValueError(f"{label} carries no image data")
    if size > MAX_IMAGE_BYTES:
        raise ValueError(f"{label} is {size} bytes, over the {MAX_IMAGE_BYTES} limit")
    return data_base64


def display_image(
    image: bytes | str,
    mime: str = "image/jpeg",
    duration_ms: int | None = None,
    mode: str | None = None,
    caption: str | None = None,
) -> str:
    """Build a ``display.image``: show the caller a still, then return to the avatar.

    ``image`` is raw bytes or an already-base64 string. ``mode`` is
    ``"fullscreen"`` (the default StandIn applies) or ``"overlay"`` for a
    picture-in-picture inset, and ``duration_ms`` falls back to StandIn's own
    default when omitted.
    """
    message: dict[str, Any] = {
        "type": TYPE_DISPLAY_IMAGE,
        "dataBase64": _encode_image(image, mime, "display.image"),
        "mime": mime,
        # The wire reserves a timeline anchor here that this lane does not
        # use. Both SDKs send 0 rather than one omitting it, so a single
        # conformance vector covers both and neither can drift.
        "ts": 0,
    }
    if duration_ms is not None and duration_ms > 0:
        message["durationMs"] = duration_ms
    if mode:
        message["mode"] = mode
    if caption:
        message["caption"] = caption
    return encode(message)


def display_frame(
    seq: int,
    ts: int,
    image: bytes | str,
    mime: str = "image/jpeg",
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Build a ``display.frame``: one frame of continuous avatar video.

    Latest wins. There is no handshake, the first frames start the stream and
    silence ends it, and a sender under backpressure MUST drop frames rather
    than buffer them, exactly as it does for hot-path audio.

    ``ts`` belongs to the sender's own media timeline, the same one its
    outbound audio is stamped on, so the two streams share a clock.
    """
    message: dict[str, Any] = {
        "type": TYPE_DISPLAY_FRAME,
        "seq": seq,
        "ts": ts,
        "mime": mime,
        "dataBase64": _encode_image(image, mime, "display.frame"),
    }
    if width is not None:
        message["width"] = width
    if height is not None:
        message["height"] = height
    return encode(message)


#: Hard bound on the vision round trip. The agent is mid-call waiting on this,
#: and a caller hears the silence.
_DESCRIBE_TIMEOUT_S = 20.0

#: Enough for a sentence or two read aloud. A voice agent cannot relay an essay.
_DESCRIBE_MAX_TOKENS = 300


def frame_digest(data_base64: str) -> str:
    """A short, stable fingerprint of one frame.

    For asking "is this the same screen as last time?" without keeping the
    picture. A hash of the encoded form is enough: two encodes of an unchanged
    screen are byte-identical.
    """
    import hashlib

    return hashlib.sha256((data_base64 or "").encode("ascii", "ignore")).hexdigest()[:32]


def frame_owner(frame: VideoFrame) -> str | None:
    """Who is showing this, when the wire said."""
    name = (frame.participant_name or "").strip()
    return name or None


def fallback_owner(source: str) -> str:
    """Who to say it is when nobody was named.

    Attribution that degrades rather than vanishing: "a participant's screen" is
    worth more to a model than an unlabelled picture.
    """
    return "a participant" if source == "screenshare" else "the caller"


def frame_caption(owner: str) -> str:
    """The sentence that goes beside a frame, so a model knows whose it is."""
    return f"screen shared by {owner}" if owner == "a participant" else f"camera of {owner}"


@dataclass(frozen=True)
class FrameDescriber:
    """Turn a frame into a sentence, using a vision model you choose.

    Most speech-to-speech providers hear but cannot see. This is what lets one
    answer "what is on my screen?": the frame goes to any OpenAI-compatible
    chat-completions endpoint that accepts image input (OpenAI, Azure OpenAI,
    Ollama, vLLM, whatever you run), and what comes back is text the agent can
    say out loud.

    The frame is sent for inference and not stored, which is the difference
    between this and uploading it into a provider's own conversation history.

    Deliberately NOT put through :mod:`standin.fetch`'s guard: this URL is
    yours, set by you in the environment, and a vision model on localhost is a
    normal way to run one. That is the opposite of an image URL a model chose,
    which is untrusted and does go through the guard.
    """

    url: str
    """Chat-completions endpoint, for example
    ``https://api.openai.com/v1/chat/completions``."""

    model: str
    """The vision model to ask."""

    api_key: str | None = None
    """Sent as a bearer token when set. A local endpoint usually needs none."""

    @staticmethod
    def from_env() -> FrameDescriber | None:
        """Build one from ``STANDIN_VISION_API_URL`` and ``STANDIN_VISION_MODEL``.

        Returns ``None`` when they are not set, which is the signal a
        plugin uses to tell an agent that looking is not available here.
        """
        url = os.environ.get("STANDIN_VISION_API_URL", "").strip()
        model = os.environ.get("STANDIN_VISION_MODEL", "").strip()
        if not url or not model:
            return None
        return FrameDescriber(
            url=url,
            model=model,
            api_key=os.environ.get("STANDIN_VISION_API_KEY", "").strip() or None,
        )

    async def describe(self, frame: VideoFrame, question: str) -> str:
        """Ask the model about one frame. Raises on anything that goes wrong,
        so a caller can hand the reason back to the agent that asked."""
        who = frame_owner(frame) or fallback_owner(frame.source)
        seeing = frame_caption(who)
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "max_tokens": _DESCRIBE_MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"This is a live frame from a Microsoft Teams call ({seeing}). "
                                "Answer concisely, for a voice agent to say out loud. "
                                f"Question: {question}"
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": frame.data_url}},
                    ],
                }
            ],
        }
        timeout = aiohttp.ClientTimeout(total=_DESCRIBE_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.url, json=body, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"the vision model returned HTTP {response.status}")
                data = await response.json()
        try:
            text = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            text = ""
        if not text:
            raise RuntimeError("the vision model returned nothing")
        return text
