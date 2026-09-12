# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Writing the meeting up after it ends.

The recap runs during teardown, so nothing here may raise. And it runs on a
transcript kept for the whole call, so nothing here may grow without limit.
"""

from __future__ import annotations

import zipfile

import pytest

from standin.chat import PersonalChat
from standin.minutes import (
    DOCUMENT_NOT_ATTACHED,
    MAX_TRANSCRIPT_ENTRY_CHARS,
    MAX_TRANSCRIPT_TURNS,
    MINUTES_TOOL,
    DeliveryTarget,
    MinutesSection,
    PostOutcome,
    Transcript,
    Turn,
    has_speaker_prefix,
    is_summary_request,
    minutes_prompt,
    parse_minutes_sections,
    post_minutes,
    resolve_minutes_target,
    write_minutes_docx,
)

pytestmark = pytest.mark.unit

#: Where a recap goes, resolved before any of it runs.
_TARGET = DeliveryTarget(kind="thread", conversation_id="19:thread", tenant_id="tenant-1")


def _transcript() -> Transcript:
    transcript = Transcript()
    transcript.add("Dana", "we should push the launch to March")
    transcript.add("Ali", "agreed, I will tell the field team")
    return transcript


async def _summarise(prompt: str) -> str:
    return "**Decisions**\nThe launch moves to March."


# ------------------------------------------------------------- the transcript


def test_a_turn_records_who_said_it():
    transcript = _transcript()
    assert "Dana: we should push the launch to March" in transcript.render()


def test_an_empty_turn_is_not_recorded_blank():
    transcript = Transcript()
    transcript.add("Dana", "   ")
    assert transcript.empty


def test_a_speaker_always_has_a_name():
    transcript = Transcript()
    transcript.add("", "something")
    assert transcript.render().startswith("Caller:")


def test_the_transcript_is_bounded():
    """A two-hour meeting sits in the memory of a process that is also carrying
    live audio."""
    transcript = Transcript()
    for index in range(MAX_TRANSCRIPT_TURNS + 50):
        # A different speaker each time, so nothing coalesces and the count is
        # the count of turns.
        transcript.add(f"speaker {index}", f"turn {index}")
    assert len(transcript.turns) == MAX_TRANSCRIPT_TURNS
    # The tail is what survives: the end of a meeting is what minutes are about.
    assert f"turn {MAX_TRANSCRIPT_TURNS + 49}" in transcript.render()


def test_rendering_keeps_the_tail_when_it_is_too_long():
    transcript = Transcript()
    transcript.add("Dana", "x" * 500)
    transcript.add("Dana", "the last thing said")
    rendered = transcript.render(max_chars=100)
    assert len(rendered) == 100
    assert "the last thing said" in rendered


# ---------------------------------------------------------------- the visuals


def test_what_was_shown_is_recorded_beside_what_was_said():
    """The half a transcript-first recap structurally cannot have."""
    transcript = _transcript()
    transcript.add_visual("Sara's shared screen: the Q3 revenue dashboard")
    rendered = transcript.render()
    assert "[Shared on screen during the call]" in rendered
    assert "Q3 revenue dashboard" in rendered


def test_an_unchanged_screen_is_recorded_once():
    """The vision lane answers about whatever is on screen each time it is
    asked, so a static slide would otherwise fill the record."""
    transcript = Transcript()
    for _ in range(5):
        transcript.add_visual("the same slide")
    transcript.add_visual("a different slide")
    transcript.add_visual("the same slide")
    assert list(transcript.visuals) == ["the same slide", "a different slide", "the same slide"]


def test_a_call_with_only_visuals_is_not_empty():
    transcript = Transcript()
    transcript.add_visual("a diagram")
    assert transcript.empty is False


# ----------------------------------------------------------------- the prompt


@pytest.mark.parametrize(
    "text",
    [
        "can you summarise the meeting",
        "send me the minutes of this call",
        "give me a recap of the discussion",
        "write up notes from the conversation",
    ],
)
def test_a_request_for_minutes_is_recognised(text: str):
    assert is_summary_request(text) is True


@pytest.mark.parametrize(
    "text",
    ["summarise this document", "what are the minutes in an hour", "recap the article", ""],
)
def test_something_else_is_not_mistaken_for_one(text: str):
    """Summarise alone is asked about a document, an email, or a page the agent
    is looking at."""
    assert is_summary_request(text) is False


def test_the_prompt_forbids_inventing_what_was_on_screen():
    """A model handed "Sara shared a dashboard" will invent the numbers on it,
    and minutes that invent numbers are worse than minutes with a gap."""
    prompt = minutes_prompt("Dana: hello")
    assert "never infer what was on screen" in prompt
    assert "Dana: hello" in prompt


def test_the_tool_is_described_for_a_model():
    assert "use it when" in MINUTES_TOOL.description.lower()
    assert MINUTES_TOOL.required == ()


# ------------------------------------------------------------------ the recap


async def test_the_minutes_are_posted_to_the_chat():
    posted: list[str] = []

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        posted.append(text)
        return True

    result = await post_minutes(_summarise, _transcript(), _TARGET, deliver)
    assert result.delivered is True
    assert "posted the minutes" in result.spoken
    assert "The launch moves to March." in posted[0]


async def test_a_call_with_nothing_said_is_told_apart_from_one_with_no_chat():
    """Conflating them tells people their conversation did not count when it
    did."""

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    nothing = await post_minutes(_summarise, Transcript(), _TARGET, deliver)
    assert "not enough of a conversation" in nothing.spoken

    nowhere = await post_minutes(_summarise, _transcript(), None, deliver)
    assert "no Microsoft Teams chat" in nowhere.spoken


async def test_a_failing_summariser_never_raises_into_teardown():
    async def boom(prompt: str) -> str:
        raise RuntimeError("the agent is down")

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(boom, _transcript(), _TARGET, deliver)
    assert result.spoken == "I could not summarize the meeting."
    assert result.delivered is False


async def test_an_empty_summary_is_reported_rather_than_posted():
    posted: list[str] = []

    async def empty(prompt: str) -> str:
        return "   "

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        posted.append(text)
        return True

    result = await post_minutes(empty, _transcript(), _TARGET, deliver)
    assert "could not summarize" in result.spoken
    assert posted == []


async def test_a_failing_delivery_is_admitted():
    async def deliver(target: DeliveryTarget, text: str) -> bool:
        raise RuntimeError("the chat channel is closed")

    result = await post_minutes(_summarise, _transcript(), _TARGET, deliver)
    assert result.delivered is False
    assert "could not post it" in result.spoken
    # The minutes still came back, so a caller can do something else with them.
    assert result.minutes


async def test_the_document_is_written_when_somewhere_was_named(tmp_path):
    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(
        _summarise, _transcript(), _TARGET, deliver, document_dir=tmp_path / "minutes"
    )
    assert result.document is not None
    assert result.document.exists()


async def test_no_document_is_written_when_none_was_asked_for(tmp_path):
    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(_summarise, _transcript(), _TARGET, deliver)
    assert result.document is None


async def test_a_document_that_could_not_ride_along_is_said_so_in_the_message(tmp_path):
    """A chat reply carries text and cards, not files. Somebody told the minutes
    were coming with a document, who gets text and no explanation, assumes the
    attachment was lost in transit and goes looking for it."""
    posted: list[str] = []

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        posted.append(text)
        return True

    await post_minutes(
        _summarise, _transcript(), _TARGET, deliver, document_dir=tmp_path / "minutes"
    )
    assert DOCUMENT_NOT_ATTACHED in posted[0]

    posted.clear()
    await post_minutes(_summarise, _transcript(), _TARGET, deliver)
    # Nothing was written, so there is nothing whose absence needs explaining.
    assert DOCUMENT_NOT_ATTACHED not in posted[0]


async def test_a_document_that_cannot_be_written_does_not_lose_the_minutes(tmp_path):
    """The document is a convenience. The minutes are the point."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(_summarise, _transcript(), _TARGET, deliver, document_dir=blocker)
    assert result.document is None
    assert result.delivered is True


# ------------------------------------------------------------------ the .docx


def test_the_document_is_a_real_docx(tmp_path):
    """Four parts, or Word and the validators will not have it."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Meeting minutes", "**Decisions**\nThe launch moves to March.", path)

    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert names == {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            # An empty relationships part: a validator refuses a part that has
            # none at all.
            "word/_rels/document.xml.rels",
        }
        document = archive.read("word/document.xml").decode()
        rels = archive.read("word/_rels/document.xml.rels").decode()
    assert "The launch moves to March." in document
    assert "<w:b/>" in document  # the heading kept its emphasis
    assert "<Relationships" in rels


def test_the_document_opens_at_a4_with_margins(tmp_path):
    """Letter at zero margins is the first thing a person notices about a
    document they are meant to keep."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Minutes", "something", path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert '<w:pgSz w:w="11906" w:h="16838"/>' in document
    assert 'w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"' in document
    assert 'w:header="708" w:footer="708" w:gutter="0"' in document


def test_the_title_and_headings_are_sized(tmp_path):
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        sections=[MinutesSection("Decisions", ["The launch moves to March."])],
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert '<w:sz w:val="40"/>' in document  # the title
    assert '<w:sz w:val="28"/>' in document  # the heading
    assert '<w:spacing w:after="120"/>' in document
    assert '<w:spacing w:before="200" w:after="80"/>' in document


def test_a_heading_is_sized_the_same_whether_the_sections_were_parsed_or_not(tmp_path):
    """A document whose headings are set on one path and left as plain bold on
    the other is two documents, and the other SDK writes one of them."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Meeting minutes", "**Decisions**\nThe launch moves to March.", path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert '<w:spacing w:before="200" w:after="80"/>' in document
    assert '<w:sz w:val="28"/>' in document


def test_a_quote_in_the_minutes_is_escaped_too(tmp_path):
    """Five entities, not three: a quote or an apostrophe reaches an attribute
    in some readers."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Minutes", 'Dana said "we ship" and Ali\'s team agreed', path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "&quot;we ship&quot;" in document
    assert "Ali&apos;s team" in document


def test_an_ampersand_is_not_escaped_twice(tmp_path):
    """The ampersand has to go first, or the other four escapes get escaped."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Minutes", "sales & marketing <met>", path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "sales &amp; marketing &lt;met&gt;" in document
    assert "&amp;lt;" not in document


def test_the_document_escapes_what_would_break_the_xml(tmp_path):
    """Minutes come from a model summarising whatever people said."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Minutes", "Ali said <b>go</b> & Dana agreed", path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "&lt;b&gt;go&lt;/b&gt; &amp; Dana" in document


def test_blank_lines_do_not_become_empty_paragraphs(tmp_path):
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Minutes", "one\n\n\ntwo", path)

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert document.count("<w:p>") == 3  # the title plus two lines


# ------------------------------------------------------------- both languages


def test_the_typescript_sdk_writes_a_document_this_one_can_open(tmp_path):
    """The TypeScript half hand-rolls its zip container, because Node ships
    deflate and no zip. Its own tests can only check it against itself, so the
    proof that a real reader accepts it is a real reader: this one."""
    import json
    import shutil
    import subprocess
    import zipfile
    from pathlib import Path

    node = shutil.which("node")
    dist = Path(__file__).resolve().parents[2] / "typescript" / "dist" / "minutes.js"
    if node is None or not dist.exists():
        pytest.skip("needs node and a built TypeScript half")

    out = tmp_path / "from-typescript.docx"
    script = (
        f"import {{ writeMinutesDocx }} from {json.dumps(dist.as_uri())};"
        f"writeMinutesDocx('Meeting minutes', '**Decisions**\\nThe launch moves to March.',"
        f" {json.dumps(str(out))});"
    )
    subprocess.run([node, "--input-type=module", "-e", script], check=True, timeout=60)

    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None  # every checksum checks out
        # The same four parts this SDK writes. A document that carries three in
        # one language and four in the other is two document formats.
        assert set(archive.namelist()) == {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/_rels/document.xml.rels",
        }
        document = archive.read("word/document.xml").decode()
    assert "The launch moves to March." in document
    assert "<w:b/>" in document


# ---------------------------------------------------------------- the sections


@pytest.mark.parametrize(
    "line",
    [
        "# Key points",
        "## Key points",
        "###### Key points",
        "   ### Key points   ",
        "**Key points**",
        "**Key points:**",
        "**Key points**:",
    ],
)
def test_a_heading_is_recognised_in_every_form_a_model_writes(line: str):
    """Asked for "### Key points", a model answers with whichever of these it
    feels like today, and accepting one form produced an unheaded blob."""
    sections = parse_minutes_sections(f"{line}\n- something")
    assert [section.heading for section in sections] == ["Key points"]


@pytest.mark.parametrize(
    "line",
    [
        "#Key points",  # no whitespace after the hashes
        "####### Key points",  # seven hashes is not a heading
        "**Key points** and then some prose",  # the bold run has to end the line
        "###",  # hashes and nothing else
    ],
)
def test_a_line_that_only_looks_like_a_heading_is_kept_as_text(line: str):
    sections = parse_minutes_sections(line)
    assert [section.heading for section in sections] == ["Summary"]
    assert sections[0].items == [line.strip()]


def test_content_before_any_heading_opens_a_summary_section():
    """A model that ignores the format instruction would otherwise parse to zero
    sections, and the caller sees an empty recap with no error anywhere."""
    sections = parse_minutes_sections("the team agreed to ship in March\n\n### Decisions\n- ship")
    assert sections[0].heading == "Summary"
    assert sections[0].items == ["the team agreed to ship in March"]
    assert sections[1].heading == "Decisions"


@pytest.mark.parametrize(
    "line",
    [
        "- ship in March",
        "* ship in March",
        "• ship in March",
        "1. ship in March",
        "2) ship in March",
    ],
)
def test_every_bullet_marker_loses_its_marker_and_keeps_its_text(line: str):
    sections = parse_minutes_sections(f"### Decisions\n{line}")
    assert sections[0].items == ["ship in March"]


def test_prose_with_no_bullet_is_an_item_anyway():
    """Treating unbulleted prose as "not an item" dropped whole sections with no
    trace."""
    sections = parse_minutes_sections("### Decisions\nThe team agreed to ship in March.")
    assert sections[0].items == ["The team agreed to ship in March."]


def test_a_repeated_heading_is_a_new_section_not_a_merge():
    sections = parse_minutes_sections("### Decisions\n- one\n### Decisions\n- two")
    assert [section.items for section in sections] == [["one"], ["two"]]


def test_a_line_that_is_only_a_bullet_marker_is_dropped():
    sections = parse_minutes_sections("### Decisions\n-\n- real one\n•  \n")
    assert sections[0].items == ["real one"]


def test_the_parser_keeps_a_section_that_has_nothing_in_it():
    """Lossless on purpose: the WRITER is what omits an empty section, which
    keeps this function round-trippable and testable."""
    sections = parse_minutes_sections("### Decisions\n\n### Action items\n- call Dana")
    assert [(section.heading, section.items) for section in sections] == [
        ("Decisions", []),
        ("Action items", ["call Dana"]),
    ]


def test_parsing_nothing_yields_nothing():
    assert parse_minutes_sections("") == []
    assert parse_minutes_sections("   \n\n  ") == []


# -------------------------------------------------------- the written sections


def test_an_empty_section_is_not_printed_at_all(tmp_path):
    """A bare "Decisions" over white space reads as a section the agent failed to
    fill, rather than one that had nothing in it."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        sections=[
            MinutesSection("Decisions", ["  ", ""]),
            MinutesSection("Action items", ["call Dana"]),
        ],
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Decisions" not in document
    assert "Action items" in document


def test_a_bullet_is_a_plain_paragraph_with_a_bullet_in_front(tmp_path):
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes", "", path, sections=[MinutesSection("Decisions", ["ship in March"])]
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert '<w:t xml:space="preserve">• ship in March</w:t>' in document
    assert "numbering" not in document


def test_the_subtitle_is_written_under_the_title(tmp_path):
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        subtitle="Call with Dana - ~12 min, 2 human participants.",
        sections=[],
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Call with Dana - ~12 min, 2 human participants." in document


# --------------------------------------------------- the attributed transcript


def test_the_document_says_who_said_what(tmp_path):
    """The half a transcript-only recap tool cannot produce: unmixed audio gave
    a real speaker per utterance."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        sections=[],
        transcript=[
            Turn("Dana", "we should push the launch"),
            Turn("Assistant", "noted", role="assistant"),
        ],
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Attributed transcript" in document
    assert "Dana: we should push the launch" in document
    assert "Assistant: noted" in document


def test_a_turn_that_already_names_its_speaker_is_not_prefixed_twice(tmp_path):
    """ "Caller: Sara: ..." reads as a transcription error, and re-labelling it
    "Caller" would destroy the attribution outright."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        sections=[],
        transcript=[Turn("Caller", "Sara: we ship in March")],
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Sara: we ship in March" in document
    assert "Caller: Sara:" not in document


def test_a_turn_with_no_speaker_of_its_own_takes_the_caller_label(tmp_path):
    """A name that is only white space is no name: "   : we ship in March" reads
    as a document that lost the speaker rather than one that never had it."""
    path = tmp_path / "minutes.docx"
    write_minutes_docx(
        "Meeting minutes",
        "",
        path,
        sections=[],
        transcript=[Turn("   ", "we ship in March")],
        caller_label="Caller",
    )

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Caller: we ship in March" in document


def test_an_empty_turn_is_left_out_of_the_transcript(tmp_path):
    path = tmp_path / "minutes.docx"
    write_minutes_docx("Meeting minutes", "", path, sections=[], transcript=[Turn("Dana", "   ")])

    with zipfile.ZipFile(path) as archive:
        document = archive.read("word/document.xml").decode()
    # Nothing survived, so the heading is not printed over white space either.
    assert "Attributed transcript" not in document


@pytest.mark.parametrize("text", ["Sara: we ship", "Sara Ahmed: we ship"])
def test_an_attribution_is_recognised(text: str):
    assert has_speaker_prefix(text) is True


@pytest.mark.parametrize("text", [": ok", " Sara: ok", "no colon here", "Sara:no space", ""])
def test_something_that_is_not_an_attribution_is_not_mistaken_for_one(text: str):
    assert has_speaker_prefix(text) is False


# ------------------------------------------------------------ turn coalescing


def test_fragments_from_one_speaker_become_one_entry():
    """Feeding a model half-sentences as separate turns makes the minutes read
    like a stutter."""
    transcript = Transcript()
    transcript.add("Dana", "we should push")
    transcript.add("Dana", "the launch to March")
    assert len(transcript.turns) == 1
    assert transcript.turns[0].text == "we should push the launch to March"


def test_a_second_speaker_never_joins_the_entry_before_it():
    """The dangerous case: every later person's words filed under the first
    speaker's name is worse than no attribution, because it is confidently
    wrong."""
    transcript = Transcript()
    transcript.add("Dana", "we should push the launch")
    transcript.add("Ali", "I disagree")
    assert [turn.speaker for turn in transcript.turns] == ["Dana", "Ali"]
    assert transcript.turns[1].text == "I disagree"


def test_the_two_sides_are_never_merged_into_one_entry():
    transcript = Transcript()
    transcript.add("Assistant", "shall I write that up", role="assistant")
    transcript.add("Assistant", "yes please")
    assert [turn.role for turn in transcript.turns] == ["assistant", "caller"]


def test_one_long_speaker_run_stops_growing_at_the_cap():
    """Without the per-entry cap, an hour heard as one stream becomes a single
    ever-growing entry that the entry count can never trim."""
    transcript = Transcript()
    for _ in range(40):
        transcript.add("Dana", "x" * 100)
    assert len(transcript.turns) > 1
    assert all(len(turn.text) < MAX_TRANSCRIPT_ENTRY_CHARS for turn in transcript.turns)


# --------------------------------------------------------- where the minutes go


def _caller_chat(aad_id: str = "caller-1", tenant_id: str = "tenant-1") -> PersonalChat:
    return PersonalChat(
        conversation_id="a:1abcdef",
        tenant_id=tenant_id,
        aad_id=aad_id,
        display_name="Dana",
        at_ms=0,
    )


def test_a_meeting_thread_wins_even_when_the_count_says_one():
    """human_count arrives only where a participants frame does; where it does
    not it stays pinned at 1, and a count-only test delivered the minutes of a
    group call into one attendee's DM."""
    target = resolve_minutes_target(
        thread_id="19:meeting_x@thread.v2",
        human_count=1,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="tenant-1",
    )
    assert target == DeliveryTarget(
        kind="thread", conversation_id="19:meeting_x@thread.v2", tenant_id="tenant-1"
    )


def test_a_real_participant_count_is_enough_on_its_own():
    """The non-meeting group case, where a count does arrive."""
    target = resolve_minutes_target(
        thread_id="a:group-chat",
        human_count=3,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="tenant-1",
    )
    assert target is not None
    assert target.kind == "thread"


def test_a_one_to_one_call_goes_to_the_callers_own_chat():
    target = resolve_minutes_target(
        thread_id="",
        human_count=1,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="tenant-1",
    )
    assert target == DeliveryTarget(
        kind="caller-dm", conversation_id="a:1abcdef", tenant_id="tenant-1"
    )


def test_a_blank_thread_id_is_never_a_group_target():
    target = resolve_minutes_target(
        thread_id="   ",
        human_count=4,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="tenant-1",
    )
    assert target is not None
    assert target.kind == "caller-dm"


def test_a_call_that_identifies_nobody_gets_no_target():
    """An unidentified caller has no chat that can be asserted as theirs, so the
    recap has nowhere safe to go."""
    assert (
        resolve_minutes_target(
            thread_id="",
            human_count=1,
            caller_aad_id=None,
            caller_chat=None,
            session_tenant_id="tenant-1",
        )
        is None
    )


def test_a_remembered_chat_belonging_to_somebody_else_is_refused():
    assert (
        resolve_minutes_target(
            thread_id="",
            human_count=1,
            caller_aad_id="caller-2",
            caller_chat=_caller_chat(aad_id="caller-1"),
            session_tenant_id="tenant-1",
        )
        is None
    )


def test_the_tenant_is_the_one_this_worker_is_bound_to():
    """session.start first, then the configured tenant, then the remembered
    sender. The caller's own tenant is absent or foreign for a guest and is
    deliberately not a source at all."""
    from_session = resolve_minutes_target(
        thread_id="19:meeting_x@thread.v2",
        human_count=2,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="tenant-session",
        config_tenant_id="tenant-config",
    )
    assert from_session is not None
    assert from_session.tenant_id == "tenant-session"

    from_config = resolve_minutes_target(
        thread_id="19:meeting_x@thread.v2",
        human_count=2,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(),
        session_tenant_id="",
        config_tenant_id="tenant-config",
    )
    assert from_config is not None
    assert from_config.tenant_id == "tenant-config"

    # The same three, in the same order, for a meeting thread. The order is a
    # property of the post, not of which conversation it lands in.
    thread_from_chat = resolve_minutes_target(
        thread_id="19:meeting_x@thread.v2",
        human_count=2,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(tenant_id="tenant-remembered"),
    )
    assert thread_from_chat is not None
    assert thread_from_chat.tenant_id == "tenant-remembered"

    from_chat = resolve_minutes_target(
        thread_id="",
        human_count=1,
        caller_aad_id="caller-1",
        caller_chat=_caller_chat(tenant_id="tenant-remembered"),
    )
    assert from_chat is not None
    assert from_chat.tenant_id == "tenant-remembered"


# ------------------------------------------------------------ the target walk


_SECOND = DeliveryTarget(kind="caller-dm", conversation_id="a:1abcdef", tenant_id="tenant-1")


async def test_the_next_target_is_tried_when_the_first_answers_404():
    """A meeting joined over the calling path never produced a conversation
    reference, so the thread answers 404 while the caller's own chat is
    perfectly reachable."""
    tried: list[str] = []

    async def deliver(target: DeliveryTarget, text: str) -> PostOutcome:
        tried.append(target.conversation_id)
        return PostOutcome(ok=False, status=404) if target is _TARGET else PostOutcome(ok=True)

    result = await post_minutes(_summarise, _transcript(), [_TARGET, _SECOND], deliver)
    assert tried == ["19:thread", "a:1abcdef"]
    assert result.delivered is True
    assert result.target == _SECOND


@pytest.mark.parametrize("status", [401, 500, 503])
async def test_nothing_else_is_tried_on_any_other_status(status: int):
    """401 is our signing and 5xx is the gateway: both would fail identically at
    the next target, and only a 404 proves nothing was delivered."""
    tried: list[str] = []

    async def deliver(target: DeliveryTarget, text: str) -> PostOutcome:
        tried.append(target.conversation_id)
        return PostOutcome(ok=False, status=status)

    result = await post_minutes(_summarise, _transcript(), [_TARGET, _SECOND], deliver)
    assert tried == ["19:thread"]
    assert result.delivered is False
    assert result.target is None


async def test_a_rejected_post_is_never_read_as_a_delivered_one():
    """An outcome object is always truthy, so a recap the gateway rejected would
    otherwise be logged and spoken as delivered."""

    async def deliver(target: DeliveryTarget, text: str) -> PostOutcome:
        return PostOutcome(ok=False, status=404)

    result = await post_minutes(_summarise, _transcript(), _TARGET, deliver)
    assert result.delivered is False
    assert "could not post it" in result.spoken


async def test_a_send_that_reports_in_its_own_shape_is_read_and_not_guessed_at():
    """The same trap one step out: a lane that answers with an outcome of its
    own is asked for its ok, never tested for truth. Every object is true, so a
    rejected post would otherwise come back as delivered."""

    class _TheirOutcome:
        ok = False
        status = 404

    tried: list[str] = []

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        tried.append(target.conversation_id)
        return _TheirOutcome()  # type: ignore[return-value]

    result = await post_minutes(_summarise, _transcript(), [_TARGET, _SECOND], deliver)
    assert result.delivered is False
    # The 404 was read too, so the second target was still tried.
    assert tried == ["19:thread", "a:1abcdef"]


async def test_the_caller_names_the_call_on_the_document_it_keeps(tmp_path):
    """The subtitle names who was on the call and for how long, and only the
    caller knows that, so it is passed in rather than guessed at here."""

    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(
        _summarise,
        _transcript(),
        _TARGET,
        deliver,
        document_dir=tmp_path / "minutes",
        subtitle="Call with Dana - ~12 min, 3 human participants.",
        caller_label="Attendee",
    )
    assert result.document is not None
    with zipfile.ZipFile(result.document) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Call with Dana - ~12 min, 3 human participants." in document


async def test_the_document_carries_the_sections_and_the_transcript(tmp_path):
    async def deliver(target: DeliveryTarget, text: str) -> bool:
        return True

    result = await post_minutes(
        _summarise, _transcript(), _TARGET, deliver, document_dir=tmp_path / "minutes"
    )
    assert result.document is not None
    with zipfile.ZipFile(result.document) as archive:
        document = archive.read("word/document.xml").decode()
    assert "Decisions" in document
    assert "Attributed transcript" in document
    assert "Dana: we should push the launch to March" in document


def test_both_sdks_offer_the_minutes_tool_under_one_name():
    """The name is what a model reads before deciding to reach for it, so the
    two SDKs naming it differently would give one of them an agent that never
    writes minutes at all."""
    from pathlib import Path

    ts = Path(__file__).resolve().parents[2] / "typescript" / "src" / "minutes.ts"
    assert f'name: "{MINUTES_TOOL.name}"' in ts.read_text()
    # Not the same as the function that does the work: a model handed two
    # things called post_minutes cannot tell which one it is calling.
    assert MINUTES_TOOL.name == "post_meeting_minutes"
