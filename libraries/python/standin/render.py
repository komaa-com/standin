# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Turning a document into something the bot's tile can show.

An agent asked to "show me that contract" has a path, and the tile takes a JPEG
or a PNG. This is the step in between: an image passes through, a PDF page is
rasterised, and an Office document is converted to PDF first.

Every renderer here is **optional**. Most deployments never show a file, and
making every install carry a PDF engine to support the ones that do is the wrong
trade. A missing renderer produces a sentence saying which one is missing, which
:mod:`standin.vision_tools` hands back to the model.

The containment rule matters more than the rendering. A model asked to show a
file was handed that path by whoever is on the call, so :func:`render_file`
refuses anything outside the roots you allow. Without that, "show me
/etc/passwd" is a working feature.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ._exceptions import StandInError
from .log import logger

__all__ = ["SHOWABLE_SUFFIXES", "allowed_roots", "render_file"]

#: What can be put on the tile, and how each one gets there.
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_PDF_SUFFIXES = {".pdf"}
_OFFICE_SUFFIXES = {".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".odt", ".odp", ".ods"}

#: Everything :func:`render_file` will attempt.
SHOWABLE_SUFFIXES = frozenset(_IMAGE_SUFFIXES | _PDF_SUFFIXES | _OFFICE_SUFFIXES)

#: How long a headless Office conversion gets before it is abandoned. It is a
#: whole process starting up, and a caller is listening to silence while it runs.
_OFFICE_TIMEOUT_S = 30.0

#: Rasterised page width. The tile is 640 wide; rendering larger and letting the
#: encoder shrink it keeps small text legible.
_PAGE_WIDTH = 1280


def allowed_roots() -> list[Path]:
    """Directories a file may be shown from.

    ``STANDIN_SHOW_ROOTS``, colon-separated. Empty means showing files is off,
    which is the right default: the paths reaching this function were chosen by
    a model that a caller is steering.
    """
    raw = os.environ.get("STANDIN_SHOW_ROOTS", "")
    return [Path(part).expanduser().resolve() for part in raw.split(os.pathsep) if part.strip()]


def _contain(path: str) -> Path:
    """Resolve a path and refuse anything outside the allowed roots."""
    roots = allowed_roots()
    if not roots:
        raise StandInError(
            "showing files is off: set STANDIN_SHOW_ROOTS to the directories a file may be "
            "shown from"
        )
    # Resolved BEFORE the comparison, so "../" and a symlink are both judged by
    # where they actually land rather than by how they are spelled.
    resolved = Path(path).expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if not resolved.is_file():
            raise StandInError("there is no such file")
        return resolved
    raise StandInError("that file is outside the directories this agent may show from")


async def render_file(path: str, page: int = 1) -> tuple[bytes, str]:
    """Render one page of a document. Returns ``(bytes, mime)``.

    Raises :class:`~standin.StandInError` with a sentence a model can read out.
    """
    resolved = _contain(path)
    suffix = resolved.suffix.lower()
    if suffix not in SHOWABLE_SUFFIXES:
        raise StandInError(
            f"{suffix or 'that file'} cannot be shown; it must be one of "
            f"{', '.join(sorted(SHOWABLE_SUFFIXES))}"
        )

    if suffix in _IMAGE_SUFFIXES:
        data = resolved.read_bytes()
        return data, "image/png" if suffix == ".png" else "image/jpeg"

    if suffix in _OFFICE_SUFFIXES:
        resolved = await _office_to_pdf(resolved)

    return await asyncio.get_running_loop().run_in_executor(None, _pdf_page_to_png, resolved, page)


def _pdf_page_to_png(pdf: Path, page: int) -> tuple[bytes, str]:
    """Rasterise one page. Needs the ``render`` extra."""
    try:
        import pypdfium2
    except ImportError as err:
        raise StandInError(
            'showing a PDF needs the render extra: pip install "standin-sdk[render]"'
        ) from err

    document = pypdfium2.PdfDocument(str(pdf))
    try:
        count = len(document)
        if page < 1 or page > count:
            raise StandInError(f"that document has {count} pages; page {page} does not exist")
        rendered = document[page - 1].render(scale=_PAGE_WIDTH / 612)
        image = rendered.to_pil()
    finally:
        document.close()

    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


async def _office_to_pdf(source: Path) -> Path:
    """Convert through headless LibreOffice, into a directory we own.

    A whole process, which is why it is bounded and why the output goes to a
    temp directory rather than beside the original: the original may be
    somewhere this worker should not be writing.
    """
    import shutil
    import tempfile

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise StandInError(
            "showing an Office document needs LibreOffice on PATH (the `soffice` command)"
        )
    out_dir = Path(tempfile.mkdtemp(prefix="standin-render-"))
    process = await asyncio.create_subprocess_exec(
        soffice,
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(source),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), timeout=_OFFICE_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError) as err:
        process.kill()
        raise StandInError("converting that document took too long") from err

    converted = out_dir / f"{source.stem}.pdf"
    if not converted.is_file():
        logger.warning("standin: LibreOffice produced no PDF for %s", source.name)
        raise StandInError("that document could not be converted")
    return converted
