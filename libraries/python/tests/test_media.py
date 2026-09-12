# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The MEDIA: marker convention.

A marker is an instruction to the channel. A channel that does not understand it
posts a temporary file path into somebody's chat, or reads it out loud character
by character, which is the bug this exists to prevent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from standin.media import MEDIA_ROOTS_ENV, load_media, media_roots, parse_media

pytestmark = pytest.mark.unit

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20


def test_a_marker_is_taken_out_of_the_text():
    got = parse_media("Here is the chart.\nMEDIA:/tmp/chart.png")
    assert got.text == "Here is the chart."
    assert got.refs == ("/tmp/chart.png",)


def test_a_path_with_spaces_survives():
    """The documented form is backticked precisely because paths have spaces.
    Splitting on whitespace truncates them at the first one."""
    assert parse_media("MEDIA:`/tmp/a b.png`").refs == ("/tmp/a b.png",)


def test_prose_that_merely_starts_with_the_word_is_left_alone():
    """Stripping every such line eats a sentence out of the answer, and whoever
    wrote it never learns why."""
    text = "MEDIA: we should talk to them about it"
    got = parse_media(text)
    assert got.text == text
    assert got.refs == ()


def test_the_blank_line_a_marker_leaves_is_collapsed():
    got = parse_media("one\n\n\n\nMEDIA:/tmp/x.png\n\n\n\ntwo")
    assert got.text == "one\n\ntwo"


def test_a_deliberate_paragraph_break_is_kept():
    assert parse_media("one\n\ntwo").text == "one\n\ntwo"


def test_markers_keep_the_order_they_were_written_in():
    got = parse_media("MEDIA:/tmp/a.png\nMEDIA:/tmp/b.png")
    assert got.refs == ("/tmp/a.png", "/tmp/b.png")


@pytest.mark.parametrize("marker", ["media:/tmp/x.png", "MEDIA:/tmp/x.png", "Media: /tmp/x.png"])
def test_the_marker_is_case_insensitive(marker: str):
    assert parse_media(marker).refs == ("/tmp/x.png",)


def test_a_reply_with_no_marker_comes_back_unchanged():
    assert parse_media("just an answer").text == "just an answer"


# ------------------------------------------------------------------ loading


async def test_a_file_inside_a_named_root_is_sent(tmp_path):
    png = tmp_path / "chart.png"
    png.write_bytes(PNG)
    image = await load_media(str(png), roots=[str(tmp_path)])
    assert image.content_type == "image/png"
    assert image.name == "chart.png"


async def test_a_local_file_is_off_until_a_directory_is_named(tmp_path, monkeypatch):
    monkeypatch.delenv(MEDIA_ROOTS_ENV, raising=False)
    png = tmp_path / "chart.png"
    png.write_bytes(PNG)
    with pytest.raises(ValueError, match=MEDIA_ROOTS_ENV):
        await load_media(str(png))


async def test_a_file_outside_the_roots_is_refused(tmp_path):
    """An agent talked into naming a private file must get a refusal, not a read
    followed by an upload into somebody's chat."""
    root = tmp_path / "shared"
    root.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    with pytest.raises(ValueError, match="outside the directories"):
        await load_media(str(outside), roots=[str(root)])


async def test_a_symlink_is_judged_by_where_it_lands(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    link = root / "innocent.png"
    link.symlink_to(secret)
    with pytest.raises(ValueError, match="outside the directories"):
        await load_media(str(link), roots=[str(root)])


async def test_a_sibling_directory_does_not_pass_for_the_root(tmp_path):
    """Without a separator-terminated compare, /tmp/rootevil passes for
    /tmp/root."""
    root = tmp_path / "root"
    root.mkdir()
    evil = tmp_path / "rootevil"
    evil.mkdir()
    png = evil / "x.png"
    png.write_bytes(PNG)
    with pytest.raises(ValueError, match="outside the directories"):
        await load_media(str(png), roots=[str(root)])


@pytest.mark.parametrize("ref", ["file:///etc/passwd", "data:image/png;base64,AAAA", "s3://b/k"])
async def test_another_scheme_is_refused_by_name(ref: str, tmp_path):
    """A file:// URL handed to a URL fetcher is the usual way round a path
    guard."""
    with pytest.raises(ValueError, match="not allowed here"):
        await load_media(ref, roots=[str(tmp_path)])


async def test_a_file_that_is_not_a_picture_is_refused(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(ValueError, match="not a picture"):
        await load_media(str(path), roots=[str(tmp_path)])


async def test_an_oversized_file_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "big.png"
    path.write_bytes(PNG + b"\x00" * 4096)
    with pytest.raises(ValueError, match="over the"):
        await load_media(str(path), roots=[str(tmp_path)], max_bytes=64)


async def test_nothing_to_send_says_so(tmp_path):
    with pytest.raises(ValueError, match="nothing to send"):
        await load_media("   ", roots=[str(tmp_path)])


def test_roots_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(MEDIA_ROOTS_ENV, f"{tmp_path}{os.pathsep}{tmp_path / 'gone'}")
    roots = media_roots()
    # The one that does not exist cannot contain anything, so it is dropped.
    assert roots == (Path(os.path.realpath(tmp_path)),)


def test_no_roots_by_default(monkeypatch):
    monkeypatch.delenv(MEDIA_ROOTS_ENV, raising=False)
    assert media_roots() == ()
