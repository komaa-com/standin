# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""``MEDIA:`` markers, the convention an agent uses to send a picture.

Some agent frameworks let a reply name a file by writing a line like
``MEDIA:/tmp/chart.png`` and expect the channel to attach it. A channel that
does not understand the convention posts that line as prose, and a caller reads
a temporary file path in their chat. On a call it is worse: text-to-speech reads
the path out, character by character.

So the marker is an instruction to the channel, not something anyone should see.
:func:`parse_media` takes it out of the text and hands back what it referred to.
:func:`load_media` turns one reference into bytes, through the same guards
everything else in this SDK uses.

A local path is read only from a directory an operator named. Default: none.
An agent can be talked into writing ``MEDIA:/etc/passwd``, and the answer to
that has to be a refusal rather than a file read followed by an upload into
somebody's chat.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .chat import OUTBOUND_IMAGE_MAX_BYTES, OutboundImage, outbound_image, sniff_image_type
from .log import logger

__all__ = [
    "MEDIA_ROOTS_ENV",
    "AgentMedia",
    "load_media",
    "media_roots",
    "parse_media",
]

#: Where a local reference may be read from. Nothing until an operator says so.
MEDIA_ROOTS_ENV = "STANDIN_MEDIA_ROOTS"

#: The marker, as the convention defines it: case-insensitive, one per line, and
#: anchored to the END of the line so a path with spaces survives intact.
_MARKER = re.compile(r"\bMEDIA:\s*`?([^\n]+?)`?\s*$", re.IGNORECASE | re.MULTILINE)

#: How long a reference has to look like one before its line is removed.
_PATH_STARTS = ("http://", "https://", "/", "./", "../", "~/")
_MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")

#: A URL reference has the same budget as the picture it becomes.
_FETCH_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class AgentMedia:
    """A reply with its markers taken out, and what they pointed at."""

    text: str
    """What is safe to post and to say out loud."""

    refs: tuple[str, ...] = ()
    """What the markers referred to, in the order they were written."""


def _looks_like_ref(ref: str) -> bool:
    """Whether this is plausibly a file or a URL.

    A deliberate narrowing. Stripping every line that merely begins with
    ``MEDIA:`` eats a sentence like "MEDIA: we should talk to them" out of the
    answer, and the person who wrote it never learns why.
    """
    lowered = ref.lower()
    if lowered.startswith(_PATH_STARTS):
        return True
    if re.match(r"^[a-z]:[\\/]", lowered):  # a Windows drive
        return True
    return lowered.endswith(_MEDIA_SUFFIXES)


def parse_media(reply: str) -> AgentMedia:
    """Take the markers out of a reply and return them separately.

    The text that comes back is what to post AND what to say. Both, always: the
    whole point is that nobody sees or hears the marker.
    """
    refs: list[str] = []

    def take(match: re.Match[str]) -> str:
        ref = match.group(1).strip().strip("`").strip()
        if not ref or not _looks_like_ref(ref):
            # Not a reference, so it was prose. Leave it alone.
            return match.group(0)
        refs.append(ref)
        return ""

    text = _MARKER.sub(take, reply or "")
    # Removing a line leaves the blank line it sat on. Three or more become two,
    # which is a paragraph break; two are left alone, because they already are.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return AgentMedia(text=text, refs=tuple(refs))


def media_roots(roots: Sequence[str | Path] | None = None) -> tuple[Path, ...]:
    """Directories a local reference may be read from.

    Empty by default, which makes local references unavailable until somebody
    opts in. That is the right default for a path chosen by a model.
    """
    if roots is None:
        raw = os.environ.get(MEDIA_ROOTS_ENV, "")
        parts = [part for part in raw.split(os.pathsep) if part.strip()]
    else:
        parts = [str(root) for root in roots]
    out = []
    for part in parts:
        try:
            real = Path(os.path.realpath(os.path.expanduser(part)))
            # A root that does not exist cannot contain anything. Dropped here
            # rather than later so both SDKs resolve the same list: os.path
            # resolves a missing path happily, where the TypeScript twin's
            # realpath refuses it.
            if real.is_dir():
                out.append(real)
        except OSError:
            continue
    return tuple(out)


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    """Whether a real path sits under one of these real roots.

    Both sides are resolved through symlinks first, and the comparison is
    separator-terminated: without that, ``/tmp/rootevil`` passes for ``/tmp/root``.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    for root in roots:
        prefix = str(root).rstrip(os.sep) + os.sep
        if real == str(root) or real.startswith(prefix):
            return True
    return False


async def load_media(
    ref: str,
    *,
    roots: Sequence[str | Path] | None = None,
    max_bytes: int = OUTBOUND_IMAGE_MAX_BYTES,
    name: str | None = None,
) -> OutboundImage:
    """Turn one reference into a picture ready to send.

    Raises :class:`ValueError` with something worth reading, because the caller
    is on a path where the alternative is a dropped answer.

    A URL goes through the SDK's own guard, so a reference pointed at a private
    address is refused rather than fetched. A path is read only from a named
    root. Everything else is refused by name: ``file://`` handed to a URL
    fetcher is the usual way around a path guard.
    """
    target = (ref or "").strip()
    if not target:
        raise ValueError("there was nothing to send")

    lowered = target.lower()
    if lowered.startswith(("http://", "https://")):
        from .fetch import fetch_public_image

        data, declared = await fetch_public_image(target, max_bytes, _FETCH_TIMEOUT_MS)
        suggested = name or target.rsplit("/", 1)[-1].split("?")[0]
    elif "://" in lowered or lowered.startswith("data:"):
        scheme = lowered.split(":", 1)[0]
        raise ValueError(f"{scheme} references are not allowed here")
    else:
        allowed = media_roots(roots)
        if not allowed:
            raise ValueError(
                f"sending a local file is off until a directory is named in {MEDIA_ROOTS_ENV}"
            )
        path = Path(os.path.expanduser(target))
        if not _inside(path, allowed):
            raise ValueError("that file is outside the directories this worker may read")
        try:
            # Checked before it is read: the point of a cap is not to load it.
            size = path.stat().st_size
        except OSError as err:
            raise ValueError(f"no such file: {err.strerror or 'unreadable'}") from None
        if size > max_bytes:
            raise ValueError(f"that file is {size} bytes, over the {max_bytes} limit")
        data = path.read_bytes()
        declared = ""
        suggested = name or path.name

    # Re-checked here whatever anything upstream reported. A content type from a
    # response header or a file extension is a claim; the bytes are the fact.
    actual = sniff_image_type(data)
    if actual is None:
        raise ValueError("that file is not a picture this can send")
    if declared and declared not in ("application/octet-stream", actual):
        raise ValueError(f"that was served as {declared} but the bytes are {actual}")
    logger.info("standin: attaching %s from a MEDIA marker", suggested or "a picture")
    return outbound_image(data, actual, name=suggested)
