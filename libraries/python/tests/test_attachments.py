# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What somebody attached to a chat message.

The fetch posture is the point. An attachment URL arrives inside a message
somebody else wrote, so the origin pin is the only thing bounding where this
worker can be told to go, and every cap has to hold while reading rather than
after.
"""

from __future__ import annotations

import base64
import contextlib
import json
from typing import Any

import pytest

from standin.attachments import (
    MAX_IMAGES,
    ChatAudio,
    attachment_origin,
    attachments_note,
    build_chat_turn,
    card_action_note,
    fetch_chat_audio,
    fetch_chat_images,
    spool_clip,
    transcribe_voice_messages,
)
from standin.chat import InboundMessage

pytestmark = pytest.mark.unit

ORIGIN = "https://teams.standin.komaa.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None):
        self.status = status
        self.headers = headers or {}
        self._body = body

    @property
    def content(self) -> Any:
        body = self._body

        class Reader:
            async def iter_chunked(self, size: int):
                for at in range(0, len(body), size):
                    yield body[at : at + size]

        return Reader()


def opener(responses: dict[str, FakeResponse], seen: list[str] | None = None):
    """An injected opener, so every edge is reachable without a socket."""

    @contextlib.asynccontextmanager
    async def _get(url: str, timeout_s: float):
        if seen is not None:
            seen.append(url)
        response = responses.get(url)
        if response is None:
            raise RuntimeError("nothing there")
        yield response

    return _get


def image_attachment(name: str = "shot.png", url: str | None = None, **extra: Any) -> dict:
    return {
        "kind": "image",
        "name": name,
        "contentType": "image/png",
        "url": url or f"{ORIGIN}/relay/{name}",
        **extra,
    }


# ------------------------------------------------------------------ the pin


def test_the_origin_comes_from_the_channel_the_messages_arrived_on(monkeypatch):
    monkeypatch.delenv("STANDIN_CHAT_URL", raising=False)
    assert attachment_origin() == ORIGIN
    assert attachment_origin("wss://gateway.example/api/chat/channel") == "https://gateway.example"
    assert attachment_origin("ws://127.0.0.1:9444/x") == "http://127.0.0.1:9444"
    # A default port is not spelled out, so the two forms compare equal.
    assert attachment_origin("wss://host:443/x") == "https://host"


@pytest.mark.parametrize("url", ["", "not a url", "file:///etc/passwd", "ftp://host/x"])
def test_an_origin_that_cannot_be_resolved_is_none(url: str):
    """None means fetch nothing, which is the safe direction."""
    assert attachment_origin(url) is None


async def test_nothing_is_fetched_when_there_is_no_origin():
    """An unset origin read as "anywhere" turns a configuration typo into a
    fetcher a message can point at any address it likes."""
    seen: list[str] = []
    got = await fetch_chat_images([image_attachment()], origin=None, get=opener({}, seen))
    assert got == []
    assert seen == []


async def test_an_attachment_from_another_origin_is_refused():
    other = image_attachment(url="http://169.254.169.254/latest/meta-data/")
    seen: list[str] = []
    assert await fetch_chat_images([other], origin=ORIGIN, get=opener({}, seen)) == []
    assert seen == []


# ------------------------------------------------------------- what to try


async def test_a_pasted_screenshot_is_fetched():
    item = image_attachment()
    got = await fetch_chat_images(
        [item], origin=ORIGIN, get=opener({item["url"]: FakeResponse(PNG)})
    )
    assert len(got) == 1
    assert got[0].data == PNG
    assert got[0].data_url.startswith("data:image/png;base64,")


async def test_an_image_dragged_in_as_a_file_is_fetched_too():
    """The same png dragged from disk arrives as a file whose declared type is a
    bare extension, so gating on the kind alone makes it invisible while the
    note says one was sent."""
    item = {
        "kind": "file",
        "name": "diagram.png",
        "contentType": "png",
        "url": f"{ORIGIN}/relay/diagram",
    }
    got = await fetch_chat_images(
        [item],
        origin=ORIGIN,
        get=opener({item["url"]: FakeResponse(PNG, headers={"Content-Type": "image/png"})}),
    )
    assert len(got) == 1
    # The real type comes off the response, not the bare extension.
    assert got[0].mime == "image/png"


async def test_a_voice_note_arriving_as_a_file_is_fetched():
    """The audio kind is documented but a voice note relays as a file, so a
    plugin that gates on the kind transcribes nothing, ever."""
    item = {"kind": "file", "name": "audio.wav", "contentType": "wav", "url": f"{ORIGIN}/v"}
    got = await fetch_chat_audio(
        [item],
        origin=ORIGIN,
        get=opener({item["url"]: FakeResponse(b"RIFF", headers={"content-type": "audio/wav"})}),
    )
    assert len(got) == 1
    assert got[0].mime == "audio/wav"


async def test_a_video_container_is_accepted_as_audio():
    """Some clients label a voice note with a container type that
    speech-to-text reads perfectly well."""
    item = {"kind": "audio", "name": "note.mp4", "url": f"{ORIGIN}/v"}
    got = await fetch_chat_audio(
        [item],
        origin=ORIGIN,
        get=opener({item["url"]: FakeResponse(b"ftyp", headers={"content-type": "video/mp4"})}),
    )
    assert len(got) == 1


async def test_an_attachment_marked_unrelayable_is_not_requested():
    item = image_attachment(relayable=False)
    seen: list[str] = []
    assert await fetch_chat_images([item], origin=ORIGIN, get=opener({}, seen)) == []
    assert seen == []


# ------------------------------------------------------------- the posture


async def test_something_that_is_not_an_image_is_refused_before_it_is_read():
    """An error page would otherwise be base64'd in front of a model as though
    it were a picture."""
    item = image_attachment()
    response = FakeResponse(b"<html>gone</html>", headers={"content-type": "text/html"})
    assert await fetch_chat_images([item], origin=ORIGIN, get=opener({item["url"]: response})) == []


async def test_a_body_over_the_cap_is_refused_while_it_is_read():
    """A content-length that lies, or is simply absent, otherwise allocates
    whatever it likes before a later check objects."""
    item = image_attachment()
    big = FakeResponse(b"\x89PNG" + b"x" * 200, headers={"content-type": "image/png"})
    assert (
        await fetch_chat_images([item], origin=ORIGIN, max_bytes=32, get=opener({item["url"]: big}))
        == []
    )
    # And the same body passes under a cap that fits it.
    assert (
        len(
            await fetch_chat_images(
                [item], origin=ORIGIN, max_bytes=1024, get=opener({item["url"]: big})
            )
        )
        == 1
    )


async def test_a_declared_length_over_the_cap_is_refused_without_reading():
    item = image_attachment()
    response = FakeResponse(
        b"\x89PNG", headers={"content-type": "image/png", "content-length": "99999999"}
    )
    assert (
        await fetch_chat_images(
            [item], origin=ORIGIN, max_bytes=1024, get=opener({item["url"]: response})
        )
        == []
    )


@pytest.mark.parametrize("status", [301, 302, 401, 404, 500])
async def test_anything_but_a_success_drops_that_attachment(status: int):
    item = image_attachment()
    response = FakeResponse(PNG, status=status, headers={"content-type": "image/png"})
    assert await fetch_chat_images([item], origin=ORIGIN, get=opener({item["url"]: response})) == []


async def test_one_unreachable_attachment_costs_only_itself():
    good = image_attachment("good.png")
    bad = image_attachment("bad.png")
    got = await fetch_chat_images(
        [bad, good], origin=ORIGIN, get=opener({good["url"]: FakeResponse(PNG)})
    )
    assert [image.name for image in got] == ["good.png"]


async def test_only_so_many_images_are_kept():
    items = [image_attachment(f"{i}.png") for i in range(MAX_IMAGES + 3)]
    responses = {item["url"]: FakeResponse(PNG) for item in items}
    got = await fetch_chat_images(items, origin=ORIGIN, get=opener(responses))
    assert len(got) == MAX_IMAGES


async def test_attempts_are_capped_not_just_successes():
    """The accept cap counts only successes, so a message naming fifty
    attachments that all fail still costs fifty timeouts and blows the turn."""
    items = [image_attachment(f"{i}.png") for i in range(50)]
    seen: list[str] = []
    assert await fetch_chat_images(items, origin=ORIGIN, get=opener({}, seen)) == []
    assert len(seen) == 8


# ----------------------------------------------------------- transcription


async def test_a_voice_note_becomes_words():
    item = {"kind": "audio", "name": "note.wav", "url": f"{ORIGIN}/v"}
    response = FakeResponse(b"RIFF", headers={"content-type": "audio/wav"})

    async def transcribe(data: bytes, mime: str) -> str:
        return f"heard {len(data)} bytes of {mime}"

    said = await transcribe_voice_messages(
        [item], transcribe=transcribe, origin=ORIGIN, get=opener({item["url"]: response})
    )
    assert said == "heard 4 bytes of audio/wav"


async def test_no_transcriber_means_no_transcription_and_no_fetch():
    seen: list[str] = []
    item = {"kind": "audio", "name": "note.wav", "url": f"{ORIGIN}/v"}
    assert (
        await transcribe_voice_messages(
            [item], transcribe=None, origin=ORIGIN, get=opener({}, seen)
        )
        == ""
    )
    assert seen == []


async def test_a_transcriber_that_fails_costs_that_clip_only():
    item = {"kind": "audio", "name": "note.wav", "url": f"{ORIGIN}/v"}
    response = FakeResponse(b"RIFF", headers={"content-type": "audio/wav"})

    async def boom(data: bytes, mime: str) -> str:
        raise RuntimeError("the speech endpoint is down")

    assert (
        await transcribe_voice_messages(
            [item], transcribe=boom, origin=ORIGIN, get=opener({item["url"]: response})
        )
        == ""
    )


# -------------------------------------------------------------- the notes


def test_the_note_names_what_came_with_the_message():
    note = attachments_note([image_attachment("plan.png"), {"kind": "file", "name": "q3.xlsx"}])
    assert "plan.png" in note and "q3.xlsx" in note


def test_the_note_says_which_ones_could_not_be_read():
    """A model told a picture was attached and could not be opened says
    something useful. A model told nothing answers as if the message were
    empty."""
    note = attachments_note([image_attachment("plan.png")], status={0: "unreadable"})
    assert "unreadable" in note


def test_the_note_is_bounded():
    note = attachments_note([image_attachment(f"{i}.png") for i in range(40)])
    assert "more" in note
    assert len(note.splitlines()) <= 12


def test_no_attachments_means_no_note():
    assert attachments_note([]) == ""


def test_a_button_press_reaches_the_model():
    """A card message arrives with empty text, so without this the agent is
    asked nothing at all."""
    note = card_action_note({"action": "approve", "id": "42"})
    assert "approve" in note and "42" in note


def test_no_card_action_means_no_note():
    assert card_action_note(None) == ""


def test_a_card_payload_is_bounded():
    note = card_action_note({"blob": "x" * 90_000})
    assert len(note) < 5_000


# ----------------------------------------------------------------- the turn


async def test_the_turn_reads_in_the_order_a_person_would_say_it():
    item = image_attachment("plan.png")
    message = InboundMessage(
        tenant_id="t",
        conversation_id="c",
        activity_id="a",
        scope="personal",
        text="what do you make of this?",
        attachments=[item],
        card_action={"action": "approve"},
    )

    async def transcribe(data: bytes, mime: str) -> str:
        return "and here is what I said"

    audio = {"kind": "audio", "name": "note.wav", "url": f"{ORIGIN}/v"}
    message = InboundMessage(**{**message.__dict__, "attachments": [item, audio]})
    responses = {
        item["url"]: FakeResponse(PNG),
        audio["url"]: FakeResponse(b"RIFF", headers={"content-type": "audio/wav"}),
    }

    turn = await build_chat_turn(
        message, origin=ORIGIN, transcribe=transcribe, get=opener(responses)
    )
    assert len(turn.images) == 1
    assert turn.voice_note == "and here is what I said"
    order = [
        turn.query.index("what do you make of this?"),
        turn.query.index("approve"),
        turn.query.index("and here is what I said"),
        turn.query.index("plan.png"),
    ]
    assert order == sorted(order)


async def test_a_turn_with_nothing_attached_is_just_the_text():
    message = InboundMessage(
        tenant_id="t", conversation_id="c", activity_id="a", scope="personal", text="hello"
    )
    turn = await build_chat_turn(message, origin=ORIGIN)
    assert turn.query == "hello"
    assert turn.images == []


async def test_a_turn_never_raises_when_everything_fails():
    message = InboundMessage(
        tenant_id="t",
        conversation_id="c",
        activity_id="a",
        scope="personal",
        text="look at this",
        attachments=[image_attachment()],
    )
    turn = await build_chat_turn(message, origin=ORIGIN, get=opener({}))
    assert "look at this" in turn.query
    assert "unreadable" in turn.query


# ------------------------------------------------------------- the spool


def test_a_spooled_clip_is_removed_afterwards():
    """A transcription engine is handed somebody's voice, and leaving it in a
    temporary directory is a copy nobody decided to keep."""
    from pathlib import Path

    clip = ChatAudio(data=b"RIFF", mime="audio/wav", name="note.wav")
    with spool_clip(clip) as path:
        assert Path(path).read_bytes() == b"RIFF"
        assert path.endswith(".wav")
    assert not Path(path).exists()


def test_a_spooled_clip_is_removed_even_when_the_body_raises():
    from pathlib import Path

    clip = ChatAudio(data=b"RIFF", mime="audio/wav")
    with pytest.raises(RuntimeError):
        with spool_clip(clip) as path:
            kept = path
            raise RuntimeError("the engine failed")
    assert not Path(kept).exists()


def test_the_image_round_trips_through_base64():
    from standin.attachments import ChatImage

    image = ChatImage(data_base64=base64.b64encode(PNG).decode(), mime="image/png")
    assert image.data == PNG
    assert json.loads(json.dumps({"u": image.data_url}))["u"].startswith("data:image/png")


# ------------------------------------------------------- what goes back out


def test_a_reply_echoes_which_connection_it_is_from():
    """One tenant can have several connections, so the tenant alone no longer
    says who a reply is from."""
    from standin.chat import build_reply

    message = InboundMessage(
        tenant_id="t",
        conversation_id="c",
        activity_id="a",
        scope="personal",
        text="hi",
        binding_id="binding-2",
    )
    assert build_reply(message, "hello")["bindingId"] == "binding-2"


def test_a_reply_without_a_binding_sends_no_binding():
    from standin.chat import build_reply

    message = InboundMessage(
        tenant_id="t", conversation_id="c", activity_id="a", scope="personal", text="hi"
    )
    assert "bindingId" not in build_reply(message, "hello")


def test_the_binding_is_read_off_the_wire():
    from standin.chat import parse_inbound

    body = json.dumps(
        {
            "tenantId": "t",
            "conversationId": "c",
            "activityId": "a",
            "text": "hi",
            "bindingId": "binding-7",
        }
    )
    assert parse_inbound(body).binding_id == "binding-7"


def test_an_image_is_checked_against_its_own_bytes():
    """A declared type is a claim. Without the signature check an HTML document
    labelled image/png is posted into somebody's chat under this bot's name."""
    from standin.chat import outbound_image

    assert outbound_image(PNG, "image/png").content_type == "image/png"
    with pytest.raises(ValueError, match="not image/png"):
        outbound_image(b"<html>hello</html>", "image/png")
    with pytest.raises(ValueError, match="not image/png"):
        outbound_image(b"GIF89a" + b"\x00" * 10, "image/png")


def test_a_scriptable_image_is_refused_by_type():
    """SVG is scriptable XML, which is precisely what an image must not be."""
    from standin.chat import outbound_image

    with pytest.raises(ValueError, match="must be one of"):
        outbound_image(b"<svg/>", "image/svg+xml")


def test_a_common_misspelling_of_jpeg_is_accepted():
    from standin.chat import outbound_image

    assert outbound_image(b"\xff\xd8\xff" + b"\x00" * 10, "image/jpg").content_type == "image/jpeg"


def test_an_oversized_image_is_refused():
    from standin.chat import OUTBOUND_IMAGE_MAX_BYTES, outbound_image

    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * OUTBOUND_IMAGE_MAX_BYTES
    with pytest.raises(ValueError, match="over the"):
        outbound_image(big, "image/png")


def test_a_riff_container_is_only_a_webp_when_it_says_so():
    from standin.chat import sniff_image_type

    assert sniff_image_type(b"RIFF____WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"RIFF____WAVEfmt ") is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("../../etc/passwd.png", "passwd.png"),
        ("C:\\\\Users\\\\x\\\\plan.png", "plan.png"),
        ("  ", None),
        ("..", None),
        (None, None),
    ],
)
def test_a_filename_cannot_escape_or_be_empty(given, expected):
    """It reaches a chat as a download, chosen by a model somebody is steering."""
    from standin.chat import sanitize_image_name

    assert sanitize_image_name(given) == expected


def test_a_long_filename_keeps_its_extension():
    from standin.chat import sanitize_image_name

    got = sanitize_image_name("x" * 400 + ".png")
    assert got.endswith(".png") and len(got) <= 200


def test_a_typing_indicator_carries_neither_text_nor_image():
    """It is a state, not a message."""
    from standin.chat import build_reply, outbound_image

    message = InboundMessage(
        tenant_id="t", conversation_id="c", activity_id="a", scope="personal", text="hi"
    )
    reply = build_reply(message, "hello", kind="typing", image=outbound_image(PNG, "image/png"))
    assert "text" not in reply and "image" not in reply
