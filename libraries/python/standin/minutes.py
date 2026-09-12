# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""What the meeting was about, written down after it ends.

A recap is the one thing people ask an agent for that it cannot do while the
call is happening. It needs the whole conversation, so it happens at the end, and
by then the caller has usually gone. That shapes everything here:

**The transcript is kept as it goes, and bounded.** A two-hour meeting is a lot
of turns, and a call that holds all of them holds them in the memory of a process
that is also carrying live audio. :class:`Transcript` keeps a rolling window and
renders the TAIL, because the end of a meeting is what the minutes are mostly
about.

**It records what was shown, not just what was said.** Every transcript-first
recap tool on the market is blind to the screen share. This one is not, because
your agent was on the call and could see it. That is the part worth having.

**It records WHO said it.** Each turn keeps its speaker separately from its
words, so the document can attribute a statement to the person who made it
instead of filing the whole call under one name.

**Nothing here raises.** A recap runs during teardown, and an exception there
takes the teardown with it.

Delivery is TEXT, to ONE conversation resolved before any of this starts. A
meeting recap goes to the meeting's own thread; a one-to-one recap goes to the
caller's personal chat with this bot, when the caller is identified and has one.
:func:`resolve_minutes_target` decides that once and every later step is handed
the answer, because the worst outcome this feature has is a customer's minutes
posted into somebody else's conversation. The Word document is written to disk
beside the message, for whoever keeps the record: a chat cannot be sent a file
by a bot the way a person can, so a document promised into the chat would be a
promise that quietly fails.
"""

from __future__ import annotations

import re
import uuid
import zipfile
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .calltools import ToolSpec
from .chat import PersonalChat
from .gate import is_meeting_thread
from .log import logger

__all__ = [
    "DOCUMENT_NOT_ATTACHED",
    "MAX_TRANSCRIPT_CHARS",
    "MAX_TRANSCRIPT_ENTRIES",
    "MAX_TRANSCRIPT_ENTRY_CHARS",
    "MAX_TRANSCRIPT_TURNS",
    "MAX_TRANSCRIPT_VISUALS",
    "MINUTES_TOOL",
    "RECAP_MIN_TURNS",
    "DeliveryTarget",
    "MinutesSection",
    "PostOutcome",
    "RecapResult",
    "Transcript",
    "Turn",
    "has_speaker_prefix",
    "is_summary_request",
    "minutes_prompt",
    "parse_minutes_sections",
    "post_minutes",
    "resolve_minutes_target",
    "write_minutes_docx",
]

#: Turns kept. A long meeting must not grow without limit inside a process that
#: is also carrying live audio.
MAX_TRANSCRIPT_TURNS = 600

#: Things shown. Far fewer than turns, because a screen changes slowly.
MAX_TRANSCRIPT_VISUALS = 60

#: What the summarising model is given. The tail, not the head: the end of a
#: meeting is what the minutes are mostly about.
MAX_TRANSCRIPT_CHARS = 12_000

#: How long one entry may grow before the next turn from the same speaker starts
#: a fresh one. Without it, an hour of one person talking coalesces into a single
#: ever-growing entry that the entry count cap can never trim.
MAX_TRANSCRIPT_ENTRY_CHARS = 1000

#: Entries a recap is written from. :data:`MAX_TRANSCRIPT_TURNS` is the hard
#: bound on what is held; this is the window that reaches the model.
MAX_TRANSCRIPT_ENTRIES = 40

#: Below this there is no meeting to summarise, only a greeting.
RECAP_MIN_TURNS = 4

#: How many of the visual observations reach the prompt.
_VISUALS_IN_PROMPT = 30


@dataclass(frozen=True)
class Turn:
    """One entry in the transcript: who spoke, what they said, which side.

    ``role`` is ``"assistant"`` for the agent's own words and ``"caller"`` for
    everyone else. It is kept apart from ``speaker`` because the document labels
    the two sides differently, and because coalescing must never merge across
    either of them.
    """

    speaker: str
    text: str
    role: Literal["assistant", "caller"] = "caller"

    def __iter__(self) -> Iterator[str]:
        """Unpack as ``speaker, text``, which is what a turn used to be."""
        yield self.speaker
        yield self.text


@dataclass
class Transcript:
    """What was said, and what was shown, in the order it happened.

    The audio track records who SAID what. The visual track records who SHOWED
    what, and it is the half a transcript-first recap structurally cannot have:
    the agent was on the call and looked at the screen.

    Both are bounded. Feed it as the call runs::

        transcript.add(caller_name, "we should push the launch to March")
        transcript.add("Assistant", "noted", role="assistant")
        transcript.add_visual("Sara's shared screen: the Q3 revenue dashboard")
    """

    turns: deque[Turn] = field(default_factory=lambda: deque(maxlen=MAX_TRANSCRIPT_TURNS))
    visuals: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_TRANSCRIPT_VISUALS))

    def add(
        self, speaker: str, text: str, *, role: Literal["assistant", "caller"] = "caller"
    ) -> None:
        """Record one turn. Empty text is ignored rather than recorded blank.

        A live transcript arrives as fragments, so a turn that continues the one
        before it is merged into it: same role, same speaker, and short enough
        to stay under :data:`MAX_TRANSCRIPT_ENTRY_CHARS`. Half-sentences fed to a
        model as separate turns make the minutes read like a stutter.

        Merging across SPEAKERS is the case worth being strict about: it files
        every later person's words under the first speaker's name, which is
        worse than no attribution because it is confidently wrong.
        """
        said = (text or "").strip()
        if not said:
            return
        who = speaker or "Caller"
        if self.turns:
            last = self.turns[-1]
            merged = f"{last.text} {said}".strip()
            fits = len(merged) < MAX_TRANSCRIPT_ENTRY_CHARS
            if last.role == role and last.speaker == who and fits:
                self.turns[-1] = Turn(who, merged, role)
                return
        self.turns.append(Turn(who, said, role))

    def add_visual(self, what: str) -> None:
        """Record something shown, for example a slide or a shared screen.

        Consecutive repeats are collapsed. The vision lane describes whatever is
        on screen each time it is asked, and a screen that has not changed would
        otherwise fill the record with the same line.
        """
        shown = (what or "").strip()
        if shown and (not self.visuals or self.visuals[-1] != shown):
            self.visuals.append(shown)

    @property
    def empty(self) -> bool:
        return not self.turns and not self.visuals

    def render(
        self,
        max_chars: int = MAX_TRANSCRIPT_CHARS,
        max_entries: int = MAX_TRANSCRIPT_ENTRIES,
    ) -> str:
        """The transcript as the summarising model sees it.

        The last ``max_entries`` entries, tailed again to ``max_chars``. Both
        ends of that are deliberate: the recap window is small because a summary
        is about how the meeting ENDED, and the character tail is what keeps one
        long entry from crowding out the rest.
        """
        recent = list(self.turns)[-max_entries:] if max_entries > 0 else list(self.turns)
        body = "\n".join(f"{turn.speaker}: {turn.text}" for turn in recent)
        if self.visuals:
            shown = "\n".join(f"- {item}" for item in list(self.visuals)[-_VISUALS_IN_PROMPT:])
            body += f"\n\n[Shared on screen during the call]\n{shown}"
        return body[-max_chars:] if len(body) > max_chars else body


def is_summary_request(text: str) -> bool:
    """Whether somebody just asked for the meeting to be written up.

    Both halves are needed. "Summarise" alone is asked about a document, an
    email, or a page the agent is looking at; only paired with a word for the
    meeting itself does it mean minutes.
    """
    lowered = (text or "").lower()
    asked_to_write = any(
        word in lowered for word in ("summarize", "summarise", "minutes", "recap", "notes")
    )
    about_the_meeting = any(
        word in lowered for word in ("meeting", "call", "conversation", "discussion")
    )
    return asked_to_write and about_the_meeting


def minutes_prompt(transcript: str) -> str:
    """Ask a model for minutes, and only minutes.

    The instruction not to infer what was on screen is the load-bearing one. A
    model handed "Sara shared a dashboard" will happily invent the numbers on it,
    and minutes that invent numbers are worse than minutes with a gap.
    """
    return (
        "Summarize the transcript of this Microsoft Teams meeting into concise minutes with "
        "these sections: Key Points, Decisions, Action Items (name owners where stated), and, "
        "when the transcript includes a [Shared on screen during the call] block, Presented. "
        "In Presented, list only what that block states; never infer what was on screen. "
        f"Output only the minutes, briefly and factually.\n\nTranscript:\n{transcript}"
    )


#: The tool a model calls to write the meeting up mid-call. Registered by a
#: plugin that has somewhere to post it, which is why it is not a built-in: an
#: agent on a one-to-one call has no chat to post minutes into.
MINUTES_TOOL = ToolSpec(
    name="post_meeting_minutes",
    description=(
        "Write up the meeting so far and post the minutes to the Microsoft Teams chat. "
        "Use it when somebody asks for a summary, minutes, notes or a recap of the call."
    ),
)


# ------------------------------------------------------------- reading minutes

#: A heading, hash form. The whitespace after the hashes is required, so a line
#: like "#hashtag" is text and not a heading.
_HEADING_HASHES = re.compile(r"^#{1,6}\s+(.*\S)\s*$")

#: A heading, bold form. The bold run has to END the line: "**A** and text" is a
#: sentence that begins in bold, not a heading.
_HEADING_BOLD = re.compile(r"^\*\*(.+?)\*\*:?\s*$")

_TRAILING_COLON = re.compile(r":$")

#: A bullet in any of the shapes a model reaches for, with the marker dropped.
_BULLET = re.compile(r"^(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")

#: A line that is a marker and nothing else. It says nothing, so it is dropped
#: rather than kept as the literal text "-".
_BARE_MARKER = re.compile(r"^(?:[-*•]|\d+[.)])$")

#: What content before the first heading is filed under.
_SYNTHETIC_HEADING = "Summary"


@dataclass(frozen=True)
class MinutesSection:
    """One headed block of minutes: a heading and the lines under it."""

    heading: str
    items: list[str]


def _heading_of(line: str) -> str | None:
    """The heading this line declares, or ``None`` if it declares none."""
    match = _HEADING_HASHES.match(line) or _HEADING_BOLD.match(line)
    if match is None:
        return None
    return _TRAILING_COLON.sub("", match.group(1)).strip()


def parse_minutes_sections(text: str) -> list[MinutesSection]:
    """Split model-written minutes into sections. Pure, total, no I/O.

    Both heading forms are accepted because a model asked for "### Key points"
    freely answers with "## Key points", "# Key points" or "**Key points:**"
    depending on the model and the day. Accepting one form only produced a single
    unheaded blob and a document with no section breaks, and the document still
    built, so nothing failed loudly.

    Every bullet marker a model mixes into one answer is accepted, and a line
    under a heading that carries no marker at all is kept verbatim as an item:
    models write whole sections as one prose paragraph, and calling that "not an
    item" dropped the section without a trace.

    Nothing is lost here. A section with no items survives parsing and is dropped
    by the WRITER, which keeps this function round-trippable and testable while
    still never printing a bare heading over white space.

    Args:
        text: the minutes as the model wrote them.

    Returns:
        The sections in source order. A repeated heading opens a new section
        rather than merging into the earlier one, because that is what the model
        wrote. Content before any heading opens a synthetic "Summary" section, so
        a model that ignored the format instruction still yields a document with
        something in it.
    """
    sections: list[tuple[str, list[str]]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _heading_of(line)
        if heading is not None:
            sections.append((heading, []))
            continue
        if _BARE_MARKER.match(line):
            continue
        bullet = _BULLET.match(line)
        item = (bullet.group(1) if bullet else line).strip()
        if not item:
            continue
        if not sections:
            sections.append((_SYNTHETIC_HEADING, []))
        sections[-1][1].append(item)
    return [MinutesSection(heading=heading, items=items) for heading, items in sections]


#: An attribution a turn already carries. The leading character may be neither
#: whitespace nor a colon, so ": ok" and " Sara: ok" are text, not attribution.
_SPEAKER_PREFIX = re.compile(r"^[^\s:][^:]*:\s")


def has_speaker_prefix(text: str) -> bool:
    """Does this line already name its speaker, as "Sara: we ship in March"?

    For the compatibility path only: a :class:`Turn` keeps its speaker in its own
    field, and that is better than a prefix embedded in the words. This test is
    what stops a caller who hands in pre-prefixed text from getting
    "Caller: Sara: we ship in March", which reads as a transcription error.
    """
    return bool(_SPEAKER_PREFIX.match(text or ""))


# ------------------------------------------------------------ where it is sent


@dataclass(frozen=True)
class DeliveryTarget:
    """The ONE conversation a recap may be posted into.

    Resolved once, before any model runs, and then passed to every later step:
    the summarising run, the document write and the send. No step derives a
    recipient of its own. This is the highest-consequence rule in the feature,
    because a send with no pinned target falls back to whatever conversation the
    sending code last saw, and a customer's minutes are the most sensitive thing
    this product produces.

    Attributes:
        kind: ``"thread"`` for the meeting's own chat, ``"caller-dm"`` for the
            caller's personal chat with this bot.
        conversation_id: the conversation to post into.
        tenant_id: the tenant that conversation lives in.
    """

    kind: Literal["thread", "caller-dm"]
    conversation_id: str
    tenant_id: str


def resolve_minutes_target(
    *,
    thread_id: str | None,
    human_count: int,
    caller_aad_id: str | None,
    caller_chat: PersonalChat | None,
    session_tenant_id: str | None = None,
    config_tenant_id: str | None = None,
) -> DeliveryTarget | None:
    """Decide where this call's minutes go, once, before anything else runs.

    A meeting recap goes to the meeting thread. Everything else goes to the
    caller's own personal chat, when the caller is identified and has one. A call
    that identifies nobody gets no target, and therefore no minutes: an
    unidentified caller has no conversation that can be asserted as theirs.

    Args:
        thread_id: the call's Microsoft Teams thread, from ``session.start``.
        human_count: humans on the call, when a participants frame said so.
        caller_aad_id: the caller's directory id, when the call names one.
        caller_chat: the caller's remembered personal chat, from
            :meth:`~standin.chat.PersonalChats.for_caller`, which is where the
            narrowing rules for a personal chat live.
        session_tenant_id: the tenant from ``session.start``.
        config_tenant_id: the tenant this worker is configured for.

    Returns:
        The target, or ``None`` when there is nowhere this recap may safely go.
    """
    # The tenant this WORKER is bound to, in descending order of authority: the
    # session, then configuration, then the sender of the remembered chat, all
    # three of which describe the tenant the worker is bound to. The caller's own
    # tenant id is deliberately not a parameter: per the session contract it
    # describes whoever is on the phone, and it is absent or foreign for a guest,
    # so addressing with it points at an organisation this worker has no business
    # posting into. It is the one plausible-looking source that is actively
    # wrong.
    tenant = (
        (session_tenant_id or "").strip()
        or (config_tenant_id or "").strip()
        or (caller_chat.tenant_id.strip() if caller_chat is not None else "")
    )

    thread = (thread_id or "").strip()
    # Two independent signals, not one with a fallback. human_count only arrives
    # on topologies that send a participants frame; where it does not it stays
    # pinned at 1, and a count-only test sent every MEETING recap to one
    # attendee's private chat instead of to the meeting it summarised. The thread
    # id is on session.start already and needs no roster. Keeping the count as an
    # alternative preserves the group call that is not a meeting thread.
    if thread and (human_count >= 2 or is_meeting_thread(thread)):
        return DeliveryTarget(kind="thread", conversation_id=thread, tenant_id=tenant)

    if caller_chat is None:
        return None
    named = (caller_aad_id or "").strip()
    # Belt and braces over for_caller's own identity rule: if the call names
    # somebody and the remembered chat belongs to somebody else, this is not
    # their chat and the minutes do not go there.
    if named and caller_chat.aad_id and caller_chat.aad_id != named:
        return None
    return DeliveryTarget(
        kind="caller-dm",
        conversation_id=caller_chat.conversation_id,
        tenant_id=tenant,
    )


@dataclass(frozen=True)
class PostOutcome:
    """What the gateway said about one attempted post.

    Branch on :attr:`ok`. Never test the outcome itself for truth: an object is
    always true, so a recap the gateway rejected would be logged as delivered.

    Attributes:
        ok: whether the message actually landed.
        status: the HTTP status behind it, when there was one. 404 is the only
            status that means "this conversation cannot be reached", which is the
            only case a second target is tried.
    """

    ok: bool
    status: int = 0


@dataclass(frozen=True)
class RecapResult:
    """What happened when the meeting was written up."""

    spoken: str
    """One sentence for the agent to say. Always present, including on failure."""

    minutes: str = ""
    """The minutes themselves, empty when none were produced."""

    document: Path | None = None
    """Where the Word document was written, when one was."""

    delivered: bool = False
    """Whether the minutes actually reached the chat."""

    target: DeliveryTarget | None = None
    """Where they landed, when they did."""


#: Turn a transcript into minutes. Normally a :class:`~standin.Consultant`.
Summariser = Callable[[str], Awaitable[str]]

#: Post the minutes into ONE named conversation. Return a :class:`PostOutcome`,
#: or a bare bool when no status is available.
Poster = Callable[[DeliveryTarget, str], Awaitable["PostOutcome | bool"]]

#: Said in the message when a document was written but could not ride along. A
#: chat reply carries text and cards, not files, and somebody who was told the
#: minutes were coming with a document and then gets text with no explanation
#: assumes the attachment was lost in transit and goes looking for it.
DOCUMENT_NOT_ATTACHED = (
    "(Minutes document is not attached on a StandIn managed connection - the text "
    "above is the full record.)"
)


async def post_minutes(
    summarise: Summariser,
    transcript: Transcript,
    target: DeliveryTarget | Sequence[DeliveryTarget] | None,
    deliver: Poster,
    document_dir: Path | None = None,
    *,
    subtitle: str | None = None,
    assistant_label: str = "Assistant",
    caller_label: str = "Caller",
) -> RecapResult:
    """Write the meeting up and post it to the resolved target. Never raises.

    This normally runs during teardown, where an exception takes the whole
    teardown with it, so every failure here comes back as a sentence instead.
    Every step degrades on its own: the document failing still sends the text,
    the send failing still returns the minutes.

    Args:
        summarise: turns the transcript into minutes.
        transcript: the call as it was recorded.
        target: where the minutes go, from :func:`resolve_minutes_target`. Pass
            several, best first, when more than one conversation is admissible;
            the next is tried ONLY when the gateway answers 404. No target at all
            is a call with nowhere to post, which is said out loud rather than
            treated as a call with nothing to say.
        deliver: performs the send for one target.
        document_dir: where to keep a Word copy, when one is wanted.
        subtitle: the line under the document's title, naming the call. The
            caller supplies it because only the caller knows who was on the call
            and for how long. House style: a hyphen, never an em dash.
        assistant_label: how the agent's own turns are attributed.
        caller_label: how a caller turn with no speaker of its own is attributed.
    """
    if transcript.empty:
        return RecapResult("There was not enough of a conversation to summarize.")
    targets = _as_targets(target)
    if not targets:
        logger.info("standin: no minutes posted; this call has no Microsoft Teams chat")
        return RecapResult(
            "I can summarize this call, but it has no Microsoft Teams chat for me to post "
            "the minutes to."
        )

    try:
        minutes = (await summarise(minutes_prompt(transcript.render()))).strip()
    except Exception as err:
        logger.warning("standin: summarising the meeting failed: %s", err)
        return RecapResult("I could not summarize the meeting.")
    if not minutes:
        return RecapResult("I could not summarize the meeting.")

    document = _save_document(
        minutes,
        transcript,
        document_dir,
        subtitle=subtitle,
        assistant_label=assistant_label,
        caller_label=caller_label,
    )
    body = f"Meeting minutes\n\n{minutes}"
    if document is not None:
        body += f"\n\n{DOCUMENT_NOT_ATTACHED}"
    landed = await _deliver_to_first_reachable(targets, body, deliver)

    return RecapResult(
        spoken=(
            "I have posted the minutes to your Microsoft Teams chat."
            if landed is not None
            else "I summarized the meeting but could not post it to the chat."
        ),
        minutes=minutes,
        document=document,
        delivered=landed is not None,
        target=landed,
    )


def _as_targets(
    target: DeliveryTarget | Sequence[DeliveryTarget] | None,
) -> list[DeliveryTarget]:
    if target is None:
        return []
    if isinstance(target, DeliveryTarget):
        return [target]
    return [candidate for candidate in target if candidate is not None]


def _as_outcome(result: object) -> PostOutcome:
    """Whatever the send returned, in one shape.

    Only a real bool is read as one. An outcome OBJECT is never tested for
    truth, and that is the whole point of this function: every object is true,
    so a post the gateway rejected with a 404 comes back as delivered and the
    log line written to catch exactly that says the minutes were posted. An
    object that carries its own ``ok`` is asked for it instead.
    """
    if isinstance(result, PostOutcome):
        return result
    if isinstance(result, bool):
        return PostOutcome(ok=result)
    ok = getattr(result, "ok", None)
    status = getattr(result, "status", 0)
    return PostOutcome(ok=ok is True, status=status if isinstance(status, int) else 0)


async def _deliver_to_first_reachable(
    targets: Sequence[DeliveryTarget], text: str, deliver: Poster
) -> DeliveryTarget | None:
    """Post to the best target, and on a 404 only, to the next one.

    404 is the one answer that proves nothing was delivered, so it is the only
    one where trying again cannot duplicate a message. It is also the answer a
    meeting thread gives when the gateway holds no conversation reference for it,
    which is normal for a meeting joined over the calling path: stopping there
    meant every in-meeting post failed with a perfectly good fallback unused.
    A 401 is our own signing and a 5xx is the gateway, and both would fail
    identically at the next target, so those stop here.

    Walking the list changes WHICH already-permitted conversation receives,
    never WHO may receive: every entry was admitted by the resolver before any
    of this ran.
    """
    last = len(targets) - 1
    for index, candidate in enumerate(targets):
        try:
            result = await deliver(candidate, text)
        except Exception as err:
            logger.warning("standin: posting the minutes failed: %s", err)
            return None
        outcome = _as_outcome(result)
        if outcome.ok:
            return candidate
        if outcome.status != 404 or index == last:
            logger.warning(
                "standin: the minutes were not posted to %s (status %s)",
                candidate.conversation_id,
                outcome.status or "unknown",
            )
            return None
        logger.info(
            "standin: %s cannot be reached; trying the next delivery target",
            candidate.conversation_id,
        )
    return None


def _save_document(
    minutes: str,
    transcript: Transcript,
    document_dir: Path | None,
    *,
    subtitle: str | None = None,
    assistant_label: str = "Assistant",
    caller_label: str = "Caller",
) -> Path | None:
    """Keep a Word copy, if somewhere was named. Never fails the recap."""
    if document_dir is None:
        return None
    try:
        document_dir.mkdir(parents=True, exist_ok=True)
        # The unique name is what stops two concurrent calls overwriting each
        # other's document.
        path = document_dir / f"minutes-{uuid.uuid4().hex[:8]}.docx"
        write_minutes_docx(
            "Meeting minutes",
            minutes,
            path,
            subtitle=subtitle,
            # The model supplies the prose and code supplies the file, so the
            # same minutes always yield the same document.
            sections=parse_minutes_sections(minutes),
            transcript=list(transcript.turns),
            assistant_label=assistant_label,
            caller_label=caller_label,
        )
    except Exception as err:
        logger.warning("standin: the minutes document could not be written: %s", err)
        return None
    logger.info("standin: minutes document saved to %s", path)
    return path


# ---------------------------------------------------------------- the .docx

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
    'document.main+xml"/>'
    "</Types>"
)

_RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)

#: The document's own relationships, of which it has none. Written anyway:
#: validators refuse a part that has no rels part at all.
_DOCUMENT_RELATIONSHIPS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
)

#: A4 and real margins. Without them Word opens the file at Letter with no
#: margins, which is the first thing a person notices about a document they are
#: meant to keep.
_SECTION_PROPERTIES = (
    "<w:sectPr>"
    '<w:pgSz w:w="11906" w:h="16838"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
    'w:header="708" w:footer="708" w:gutter="0"/>'
    "</w:sectPr>"
)

_TITLE_SIZE = 40
_HEADING_SIZE = 28
#: Bullets are typed, not numbered: a numbering part is a second XML file and a
#: relationship for a character this reads perfectly well without.
_BULLET_PREFIX = "• "
_TRANSCRIPT_HEADING = "Attributed transcript"


def _escape(text: str) -> str:
    """The five XML entities. The ampersand goes first, or the other four
    escapes get their own ampersands escaped a second time."""
    escaped = text.replace("&", "&amp;")
    for character, entity in (
        ("<", "&lt;"),
        (">", "&gt;"),
        ('"', "&quot;"),
        ("'", "&apos;"),
    ):
        escaped = escaped.replace(character, entity)
    return escaped


def _paragraph(
    text: str,
    *,
    bold: bool = False,
    size: int | None = None,
    space_before: int | None = None,
    space_after: int | None = None,
) -> str:
    spacing = ""
    if space_before is not None or space_after is not None:
        before = f' w:before="{space_before}"' if space_before is not None else ""
        after = f' w:after="{space_after}"' if space_after is not None else ""
        spacing = f"<w:pPr><w:spacing{before}{after}/></w:pPr>"
    run = "<w:b/>" if bold else ""
    if size is not None:
        run += f'<w:sz w:val="{size}"/>'
    if run:
        run = f"<w:rPr>{run}</w:rPr>"
    # xml:space is what keeps the bullet prefix and any indentation from being
    # collapsed away by the reader.
    return f'<w:p>{spacing}<w:r>{run}<w:t xml:space="preserve">{_escape(text)}</w:t></w:r></w:p>'


def _heading_paragraph(text: str) -> str:
    return _paragraph(text, bold=True, size=_HEADING_SIZE, space_before=200, space_after=80)


def _section_paragraphs(section: MinutesSection) -> list[str]:
    """A heading and its bullets, or nothing at all.

    A section whose items are all blank prints neither, because a bare
    "Decisions" over white space reads as a section the agent failed to fill
    rather than one that had nothing in it.
    """
    items = [item.strip() for item in section.items if item and item.strip()]
    if not items:
        return []
    return [_heading_paragraph(section.heading)] + [
        _paragraph(f"{_BULLET_PREFIX}{item}") for item in items
    ]


def _transcript_paragraphs(
    turns: Iterable[Turn], assistant_label: str, caller_label: str
) -> list[str]:
    """Who said what, attributed line by line.

    This is the half a transcript-only recap tool cannot produce: unmixed audio
    gave a real speaker per utterance, so the document says so. A turn that
    already carries its own attribution is emitted exactly as it came, because
    re-labelling it would destroy the one thing worth keeping and prefixing it
    again would read as a transcription error.
    """
    lines: list[str] = []
    for turn in turns:
        said = (turn.text or "").strip()
        if not said:
            continue
        who = (turn.speaker or "").strip()
        if turn.role == "assistant":
            lines.append(f"{assistant_label}: {said}")
        elif has_speaker_prefix(said):
            lines.append(said)
        else:
            lines.append(f"{who or caller_label}: {said}")
    if not lines:
        return []
    return [_heading_paragraph(_TRANSCRIPT_HEADING)] + [_paragraph(line) for line in lines]


def _flat_paragraphs(minutes: str) -> list[str]:
    """The line-by-line rendering, for a caller that parsed nothing.

    A line that is bold end to end is the heading a model reaches for, and it is
    set as one: a heading is a heading whether the sections were parsed first or
    not, and a document whose headings are sized on one path and not the other
    is two documents.
    """
    paragraphs: list[str] = []
    for raw in minutes.splitlines():
        line = raw.strip()
        if not line:
            continue
        text = line.strip("*").strip()
        heading = line.startswith("**") and line.endswith("**") and len(line) > 4
        paragraphs.append(_heading_paragraph(text) if heading else _paragraph(text))
    return paragraphs


def write_minutes_docx(
    title: str,
    minutes: str,
    path: Path | str,
    *,
    subtitle: str | None = None,
    sections: Sequence[MinutesSection] | None = None,
    transcript: Iterable[Turn] | None = None,
    assistant_label: str = "Assistant",
    caller_label: str = "Caller",
) -> None:
    """Write minutes to a Word-openable document, with no dependencies.

    A .docx is a zip of four XML parts, and emitting them directly is a few
    lines. A document format library would be a dependency every install pays
    for so that the small fraction who ask for minutes get a file, which is the
    wrong trade for an SDK.

    Args:
        title: the document's first line.
        minutes: the minutes as text. Rendered line by line, with markdown
            emphasis around a whole line becoming a bold paragraph, unless
            ``sections`` is given.
        path: where to write it.
        subtitle: one line under the title, naming the call. House style, and
            this string is copied verbatim into a file customers keep: use a
            hyphen, never an em dash.
        sections: parsed sections, from :func:`parse_minutes_sections`, rendered
            instead of ``minutes``. A section with nothing in it is omitted
            entirely, heading and all.
        transcript: the call's turns. Given, an "Attributed transcript" section
            is appended after the minutes, one line per turn.
        assistant_label: how the agent's own turns are attributed.
        caller_label: how a caller turn with no speaker of its own is attributed.
    """
    paragraphs = [_paragraph(title, bold=True, size=_TITLE_SIZE, space_after=120)]
    if subtitle:
        paragraphs.append(_paragraph(subtitle))
    if sections is None:
        paragraphs.extend(_flat_paragraphs(minutes))
    else:
        for section in sections:
            paragraphs.extend(_section_paragraphs(section))
    if transcript is not None:
        paragraphs.extend(_transcript_paragraphs(transcript, assistant_label, caller_label))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(paragraphs) + _SECTION_PROPERTIES + "</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELATIONSHIPS)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", _DOCUMENT_RELATIONSHIPS)
