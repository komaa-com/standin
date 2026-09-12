// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What the meeting was about, written down after it ends.
 *
 * A recap is the one thing people ask an agent for that it cannot do while the
 * call is happening. It needs the whole conversation, so it happens at the end,
 * and by then the caller has usually gone. That shapes everything here:
 *
 * **The transcript is kept as it goes, and bounded.** A two-hour meeting is a
 * lot of turns, and a call that holds all of them holds them in the memory of a
 * process that is also carrying live audio. {@link Transcript} keeps a rolling
 * window and renders the TAIL, because the end of a meeting is what the minutes
 * are mostly about.
 *
 * **It records what was shown, not just what was said.** Every transcript-first
 * recap tool on the market is blind to the screen share. This one is not,
 * because your agent was on the call and could see it. That is the part worth
 * having.
 *
 * **Nothing here throws.** A recap runs during teardown, and an exception there
 * takes the teardown with it.
 *
 * Delivery is TEXT into the Microsoft Teams chat. The Word document is written
 * to disk beside it, for whoever keeps the record. A meeting chat cannot be sent
 * a file by a bot the way a person can, so a document promised into the chat
 * would be a promise that quietly fails.
 *
 * Identical in shape to the Python SDK's `standin.minutes`.
 */

import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { deflateRawSync } from "node:zlib";

import type { ToolSpec } from "./callTools.js";
import type { PersonalChat } from "./chat.js";
import { isMeetingThread } from "./gate.js";
import { logger } from "./log.js";

/**
 * Turns kept. A long meeting must not grow without limit inside a process that
 * is also carrying live audio.
 */
export const MAX_TRANSCRIPT_TURNS = 600;

/** Things shown. Far fewer than turns, because a screen changes slowly. */
export const MAX_TRANSCRIPT_VISUALS = 60;

/**
 * What the summarising model is given. The tail, not the head: the end of a
 * meeting is what the minutes are mostly about.
 */
export const MAX_TRANSCRIPT_CHARS = 12_000;

/**
 * How long one entry may grow before the next fragment starts a new one.
 *
 * Streaming transcripts arrive as fragments and {@link Transcript.add} joins
 * them back up. Without this cap, one long same-speaker run - an hour of a
 * group call heard as a single stream - becomes one ever-growing entry that the
 * entry count can never trim.
 */
export const MAX_TRANSCRIPT_ENTRY_CHARS = 1000;

/**
 * Entries a recap is written from. {@link MAX_TRANSCRIPT_TURNS} is the hard
 * bound on what is HELD; this is the window that reaches the model, and
 * {@link Transcript.render} applies it.
 */
export const MAX_TRANSCRIPT_ENTRIES = 40;

/**
 * Entries below which a recap is not worth running: under four turns there is
 * no meeting to summarise, only a greeting.
 */
export const RECAP_MIN_TURNS = 4;

/** How many of the visual observations reach the prompt. */
const VISUALS_IN_PROMPT = 30;

/** Which side of the call a turn came from. */
export type TurnRole = "assistant" | "caller";

/** One turn: who said it, what they said, and which side they are on. */
export interface Turn {
  readonly speaker: string;
  readonly text: string;
  /**
   * Kept apart from {@link Turn.speaker} because the document labels the two
   * sides differently, and because a fragment must never continue an entry from
   * the other side. Absent on a turn built by hand, and absent means "caller".
   */
  readonly role?: TurnRole;
}

/** Options for {@link Transcript}. */
export interface TranscriptOptions {
  /**
   * Entries HELD before the oldest is discarded. Defaults to the hard bound,
   * {@link MAX_TRANSCRIPT_TURNS}. Lower it only to hold less of a long call in
   * memory: what reaches the model is {@link Transcript.render}'s own window.
   */
  maxEntries?: number;
}

/**
 * What was said, and what was shown, in the order it happened.
 *
 * The audio track records who SAID what. The visual track records who SHOWED
 * what, and it is the half a transcript-first recap structurally cannot have:
 * the agent was on the call and looked at the screen.
 *
 * Both are bounded. Feed it as the call runs:
 *
 * ```ts
 * transcript.add(callerName, "we should push the launch to March");
 * transcript.addVisual("Sara's shared screen: the Q3 revenue dashboard");
 * ```
 */
export class Transcript {
  readonly #turns: Turn[] = [];
  readonly #visuals: string[] = [];
  readonly #maxEntries: number;

  constructor(options: TranscriptOptions = {}) {
    this.#maxEntries = options.maxEntries ?? MAX_TRANSCRIPT_TURNS;
  }

  /** What was said, oldest first. */
  get turns(): readonly Turn[] {
    return this.#turns;
  }

  /** What was shown, oldest first. */
  get visuals(): readonly string[] {
    return this.#visuals;
  }

  /**
   * Record one turn. Empty text is ignored rather than recorded blank.
   *
   * A fragment continues the entry before it when the SAME speaker is still
   * talking and the entry has room. Speech arrives in pieces, and a model fed
   * half-sentences as separate turns writes minutes that read like a stutter.
   *
   * Merging across speakers is the case worth being careful about: every later
   * person's words would be filed under the first speaker's name, which is
   * worse than no attribution because it is confidently wrong. The role is
   * checked with it, so the agent's own words never continue a caller's entry
   * even on a call where both are recorded under one name.
   */
  add(speaker: string, text: string, role: TurnRole = "caller"): void {
    const said = (text ?? "").trim();
    if (said === "") return;
    const who = speaker || "Caller";

    const last = this.#turns.length - 1;
    const previous = this.#turns[last];
    if (
      previous !== undefined &&
      previous.speaker === who &&
      (previous.role ?? "caller") === role
    ) {
      const merged = `${previous.text} ${said}`.trim();
      if (merged.length < MAX_TRANSCRIPT_ENTRY_CHARS) {
        this.#turns[last] = { speaker: who, text: merged, role };
        return;
      }
    }

    this.#turns.push({ speaker: who, text: said, role });
    if (this.#turns.length > this.#maxEntries) {
      // From the front: the end of a meeting is what minutes are mostly about.
      this.#turns.splice(0, this.#turns.length - this.#maxEntries);
    }
  }

  /**
   * Record something shown, for example a slide or a shared screen.
   *
   * Consecutive repeats are collapsed. The vision lane describes whatever is on
   * screen each time it is asked, and a screen that has not changed would
   * otherwise fill the record with the same line.
   */
  addVisual(what: string): void {
    const shown = (what ?? "").trim();
    if (shown === "" || this.#visuals[this.#visuals.length - 1] === shown)
      return;
    this.#visuals.push(shown);
    if (this.#visuals.length > MAX_TRANSCRIPT_VISUALS) {
      this.#visuals.splice(0, this.#visuals.length - MAX_TRANSCRIPT_VISUALS);
    }
  }

  get empty(): boolean {
    return this.#turns.length === 0 && this.#visuals.length === 0;
  }

  /**
   * The transcript as the summarising model sees it.
   *
   * The last `maxEntries` entries, tailed again to `maxChars`. Both ends are
   * deliberate: the recap window is small because a summary is mostly about how
   * the meeting ENDED, and the character tail is what stops one long entry
   * crowding out everything before it.
   */
  render(
    maxChars: number = MAX_TRANSCRIPT_CHARS,
    maxEntries: number = MAX_TRANSCRIPT_ENTRIES,
  ): string {
    const recent =
      maxEntries > 0 ? this.#turns.slice(-maxEntries) : this.#turns;
    let body = recent.map((turn) => `${turn.speaker}: ${turn.text}`).join("\n");
    if (this.#visuals.length > 0) {
      const shown = this.#visuals
        .slice(-VISUALS_IN_PROMPT)
        .map((item) => `- ${item}`)
        .join("\n");
      body += `\n\n[Shared on screen during the call]\n${shown}`;
    }
    return body.length > maxChars ? body.slice(-maxChars) : body;
  }
}

/**
 * Whether somebody just asked for the meeting to be written up.
 *
 * Both halves are needed. "Summarise" alone is asked about a document, an email,
 * or a page the agent is looking at; only paired with a word for the meeting
 * itself does it mean minutes.
 */
export function isSummaryRequest(text: string): boolean {
  const lowered = (text ?? "").toLowerCase();
  const askedToWrite = [
    "summarize",
    "summarise",
    "minutes",
    "recap",
    "notes",
  ].some((word) => lowered.includes(word));
  const aboutTheMeeting = [
    "meeting",
    "call",
    "conversation",
    "discussion",
  ].some((word) => lowered.includes(word));
  return askedToWrite && aboutTheMeeting;
}

/**
 * Ask a model for minutes, and only minutes.
 *
 * The instruction not to infer what was on screen is the load-bearing one. A
 * model handed "Sara shared a dashboard" will happily invent the numbers on it,
 * and minutes that invent numbers are worse than minutes with a gap.
 */
export function minutesPrompt(transcript: string): string {
  return (
    "Summarize the transcript of this Microsoft Teams meeting into concise minutes with " +
    "these sections: Key Points, Decisions, Action Items (name owners where stated), and, " +
    "when the transcript includes a [Shared on screen during the call] block, Presented. " +
    "In Presented, list only what that block states; never infer what was on screen. " +
    `Output only the minutes, briefly and factually.\n\nTranscript:\n${transcript}`
  );
}

/**
 * The tool a model calls to write the meeting up mid-call. Registered by a
 * plugin that has somewhere to post it, which is why it is not a built-in: an
 * agent on a one-to-one call has no chat to post minutes into.
 */
export const MINUTES_TOOL: ToolSpec = {
  name: "post_meeting_minutes",
  description:
    "Write up the meeting so far and post the minutes to the Microsoft Teams chat. " +
    "Use it when somebody asks for a summary, minutes, notes or a recap of the call.",
};

/**
 * Where the minutes go. One value, decided once, before anything is written.
 *
 * Resolve it with {@link resolveMinutesTarget} at the start of a recap and pass
 * this same object to every step after it: the summarising run, the document
 * write and the send. No step downstream may work out a recipient of its own.
 *
 * That belt-and-braces reads as overkill until it happens: a message tool with
 * no pinned target falls back to the operator's own chat when a reference is
 * missing, and a customer's meeting minutes - the most sensitive thing this
 * feature produces - are then delivered to the vendor. When the pinned target
 * cannot be reached, not sending is the correct outcome. Sending somewhere else
 * is not.
 */
export interface DeliveryTarget {
  /** The meeting chat, or the caller's own 1:1 chat with this bot. */
  readonly kind: "thread" | "caller-dm";
  readonly conversationId: string;
  readonly tenantId: string;
}

/** What {@link resolveMinutesTarget} needs to decide where minutes go. */
export interface MinutesTargetOptions {
  /** The call's thread id, as `session.start` gave it. */
  threadId?: string;
  /** Humans on the call, when a participants frame carried one. */
  humanCount?: number;
  /** The caller's AAD object id. A call that names nobody gets no target. */
  callerAadId?: string;
  /**
   * The caller's remembered 1:1 chat, from `PersonalChats.forCaller()`, which
   * is where the four narrowing rules live.
   */
  callerChat?: PersonalChat;
  /** The tenant from `session.start`. The first choice, and normally the one. */
  sessionTenantId?: string;
  /** The tenant this worker is configured for. */
  configTenantId?: string;
}

/**
 * Decide where a recap should be posted, before a single token is generated.
 *
 * ```ts
 * const target = resolveMinutesTarget({
 *   threadId: session.threadId,
 *   humanCount: session.humanCount,
 *   callerAadId: session.caller.aadId,
 *   callerChat: chats.forCaller({ callerAadId, tenantId }),
 *   sessionTenantId: session.tenantId,
 * });
 * ```
 *
 * A group call is minuted into the meeting it summarises. Two signals say it is
 * one, and either will do: a human count of two or more, and a meeting thread
 * id. The count only arrives on topologies that send a participants frame - on
 * a hosted worker it stays pinned at 1 - so a count-only test sent every
 * MEETING recap to the caller's private chat instead, which is the minutes of a
 * group call landing in one attendee's DM. The thread id is on `session.start`
 * already and needs no roster.
 *
 * Anything else is a 1:1 call, and the target is the caller's own chat with
 * this bot, which is admitted by `PersonalChats.forCaller()` and its four
 * narrowing rules. The first of those is worth restating here: a chat counts as
 * personal because its SCOPE says so, never because of how its conversation id
 * is spelled. A bot's personal chat is addressed `a:1...`, while `19:...` is
 * precisely the group and channel shape the rule exists to exclude, so an
 * id-prefix test admits nothing at all.
 *
 * The tenant is taken from `session.start`, then from configuration, then from
 * the remembered chat's sender. All three describe the tenant this worker is
 * bound to. The caller's own tenant id is deliberately not one of them and is
 * not even accepted here: it describes whoever is on the phone, and for a guest
 * it is foreign or absent, so addressing a conversation with it reaches into an
 * organisation this worker was never bound to. It is the one plausible-looking
 * source that is actively wrong.
 *
 * Returns undefined when there is nowhere safe to post, which is a real answer:
 * a call that identifies nobody and has no thread gets no minutes rather than
 * minutes in a stranger's chat.
 *
 * One target comes back, the best one. When a caller keeps more than one
 * admissible target - the thread first, the caller's chat behind it - the rule
 * for walking to the next is: advance on an HTTP 404 and on nothing else. A
 * gateway posts through a stored conversation reference and holds one only for
 * conversations it has seen an activity from, so a meeting joined over the
 * calling path answers 404 while the caller's own chat is perfectly reachable.
 * A 401 is our signing and a 5xx is the gateway, and both would fail the same
 * way at the next target; 404 is also the only status that proves nothing was
 * delivered, so it is the only one where trying again cannot double-post.
 */
export function resolveMinutesTarget(
  options: MinutesTargetOptions,
): DeliveryTarget | undefined {
  const threadId = (options.threadId ?? "").trim();
  const group =
    ((options.humanCount ?? 0) >= 2 || isMeetingThread(threadId)) &&
    threadId !== "";

  const chat = options.callerChat;
  // The tenant this WORKER is bound to, in descending order of authority. The
  // remembered sender's tenant is last and only ever confirms what the worker is
  // already bound to.
  const tenantId =
    (options.sessionTenantId ?? "").trim() ||
    (options.configTenantId ?? "").trim() ||
    (chat?.tenantId ?? "").trim();

  if (group) return { kind: "thread", conversationId: threadId, tenantId };
  if (chat === undefined) return undefined;

  const callerAadId = (options.callerAadId ?? "").trim();
  // The chat was admitted for one person and the call names another, so this
  // refuses rather than posting one caller's minutes into another one's chat.
  // The two ids arrive from different places and have to agree.
  if (callerAadId !== "" && chat.aadId !== "" && callerAadId !== chat.aadId)
    return undefined;

  return { kind: "caller-dm", conversationId: chat.conversationId, tenantId };
}

/** What happened when the meeting was written up. */
export interface RecapResult {
  /** One sentence for the agent to say. Always present, including on failure. */
  readonly spoken: string;
  /** The minutes themselves, empty when none were produced. */
  readonly minutes: string;
  /** Where the Word document was written, when one was. */
  readonly document?: string;
  /** Whether the minutes actually reached the chat. */
  readonly delivered: boolean;
  /** Which target took them, when one did. */
  readonly target?: DeliveryTarget;
}

/** Turn a transcript into minutes. Normally a {@link Consultant}. */
export type Summariser = (prompt: string) => Promise<string>;

/**
 * What the gateway said about one attempted post.
 *
 * Branch on {@link PostOutcome.ok}, and never test the outcome itself for
 * truth: an object is always truthy, so a recap the gateway rejected with a 404
 * or a 401 reads as delivered, which is the very failure the log line exists to
 * catch.
 */
export interface PostOutcome {
  /** Whether the message actually landed. */
  readonly ok: boolean;
  /**
   * The HTTP status behind it, when there was one. 404 is the only status that
   * means this conversation cannot be reached, and the only one on which a
   * second target is tried.
   */
  readonly status?: number;
}

/**
 * Post the minutes into ONE named conversation.
 *
 * The target is handed over with the text rather than looked up again, because
 * the recipient was settled before the summarising run and nothing downstream
 * may choose another one. Return a {@link PostOutcome}, or a bare boolean where
 * no status is available.
 */
export type Poster = (
  target: DeliveryTarget,
  text: string,
) => Promise<PostOutcome | boolean>;

/**
 * Said in the message when a document was written but could not ride along.
 *
 * A chat reply carries text and cards, not files. Somebody who was told the
 * minutes were coming with a document, and gets text with no explanation,
 * assumes the attachment was lost in transit and goes looking for it.
 */
export const DOCUMENT_NOT_ATTACHED =
  "(Minutes document is not attached on a StandIn managed connection - the text " +
  "above is the full record.)";

/** Options for {@link postMinutes}. */
export interface PostMinutesOptions {
  /** Where to keep the Word document. Omit and none is written. */
  documentDir?: string;
  /** The line under the document title, naming the call. */
  subtitle?: string;
  /** What the agent is called in the attributed transcript. */
  assistantLabel?: string;
  /** What an unnamed caller is called in the attributed transcript. */
  callerLabel?: string;
}

/**
 * Write the meeting up and post it. Never throws.
 *
 * This normally runs during teardown, where an exception takes the whole
 * teardown with it, so every failure here comes back as a sentence instead.
 *
 * A call with nowhere to post is told apart from a call with nothing to say.
 * Conflating them tells people their conversation did not count when it did.
 *
 * The target comes in already resolved, by {@link resolveMinutesTarget}, and is
 * passed on to the delivery unchanged. A 1:1 call is no longer a refusal: it
 * has a caller with their own chat, and that chat is a real target. Only an
 * undefined target, which means nowhere safe was found, still says so out loud.
 *
 * Pass several targets, best first, when more than one conversation is
 * admissible. The next is tried ONLY when the gateway answers 404.
 */
export async function postMinutes(
  summarise: Summariser,
  transcript: Transcript,
  target: DeliveryTarget | readonly DeliveryTarget[] | undefined,
  deliver: Poster,
  options: PostMinutesOptions = {},
): Promise<RecapResult> {
  if (transcript.empty) {
    return {
      spoken: "There was not enough of a conversation to summarize.",
      minutes: "",
      delivered: false,
    };
  }
  const targets = asTargets(target);
  if (targets.length === 0) {
    logger.info(
      "standin: no minutes posted; this call has no Microsoft Teams chat",
    );
    return {
      spoken:
        "I can summarize this call, but it has no Microsoft Teams chat for me to post " +
        "the minutes to.",
      minutes: "",
      delivered: false,
    };
  }

  let minutes: string;
  try {
    minutes = (await summarise(minutesPrompt(transcript.render()))).trim();
  } catch (err) {
    logger.warn(`standin: summarising the meeting failed: ${String(err)}`);
    return {
      spoken: "I could not summarize the meeting.",
      minutes: "",
      delivered: false,
    };
  }
  if (minutes === "") {
    return {
      spoken: "I could not summarize the meeting.",
      minutes: "",
      delivered: false,
    };
  }

  const document = saveDocument(minutes, transcript, options);

  let body = `Meeting minutes\n\n${minutes}`;
  if (document !== undefined) body += `\n\n${DOCUMENT_NOT_ATTACHED}`;

  const landed = await deliverToFirstReachable(targets, body, deliver);

  return {
    spoken:
      landed !== undefined
        ? "I have posted the minutes to your Microsoft Teams chat."
        : "I summarized the meeting but could not post it to the chat.",
    minutes,
    document,
    delivered: landed !== undefined,
    target: landed,
  };
}

/** One target or several, as one list. */
function asTargets(
  target: DeliveryTarget | readonly DeliveryTarget[] | undefined,
): readonly DeliveryTarget[] {
  if (target === undefined) return [];
  if (!Array.isArray(target)) return [target as DeliveryTarget];
  // A caller assembling the list from two lookups leaves a hole in it whenever
  // one of them found nothing, and a hole must not reach a teardown as a thrown
  // property access.
  return (target as readonly (DeliveryTarget | undefined)[]).filter(
    (candidate): candidate is DeliveryTarget =>
      candidate !== undefined && candidate !== null,
  );
}

/**
 * Whatever the send returned, in one shape.
 *
 * Only a real boolean is read as one, and an outcome object is never tested for
 * truth: every object is truthy, so a post the gateway rejected with a 404 came
 * back as delivered, and the log line written to catch exactly that said the
 * minutes had been posted. An object that carries its own `ok` is asked for it
 * instead, and anything else did not land.
 */
function asOutcome(result: PostOutcome | boolean): PostOutcome {
  if (typeof result === "boolean") return { ok: result };
  const ok = (result as PostOutcome | undefined)?.ok;
  const status = (result as PostOutcome | undefined)?.status;
  return {
    ok: ok === true,
    status: typeof status === "number" ? status : undefined,
  };
}

/**
 * Post to the best target, and on a 404 only, to the next one.
 *
 * A gateway posts through a stored conversation reference and holds one only
 * for conversations it has seen an activity from, so a meeting joined over the
 * calling path answers 404 while the caller's own chat is perfectly reachable:
 * stopping at the thread meant every in-meeting post failed with a good
 * fallback sitting unused. A 401 is our own signing and a 5xx is the gateway,
 * and both would fail identically at the next target. 404 is also the only
 * answer that proves nothing was delivered, so it is the only one where trying
 * again cannot post the same minutes twice.
 *
 * Walking the list changes WHICH already-permitted conversation receives, never
 * WHO may receive: every entry was admitted by the resolver before any of this
 * ran.
 */
async function deliverToFirstReachable(
  targets: readonly DeliveryTarget[],
  text: string,
  deliver: Poster,
): Promise<DeliveryTarget | undefined> {
  const last = targets.length - 1;
  for (let index = 0; index <= last; index += 1) {
    const candidate = targets[index]!;
    let outcome: PostOutcome;
    try {
      outcome = asOutcome(await deliver(candidate, text));
    } catch (err) {
      logger.warn(`standin: posting the minutes failed: ${String(err)}`);
      return undefined;
    }
    if (outcome.ok) return candidate;
    if (outcome.status !== 404 || index === last) {
      logger.warn(
        `standin: the minutes were not posted to ${candidate.conversationId} ` +
          `(status ${outcome.status ?? "unknown"})`,
      );
      return undefined;
    }
    logger.info(
      `standin: ${candidate.conversationId} cannot be reached; trying the next delivery target`,
    );
  }
  return undefined;
}

/** Keep a Word copy, if somewhere was named. Never fails the recap. */
function saveDocument(
  minutes: string,
  transcript: Transcript,
  options: PostMinutesOptions,
): string | undefined {
  const documentDir = options.documentDir;
  if (documentDir === undefined) return undefined;
  try {
    mkdirSync(documentDir, { recursive: true });
    const path = join(documentDir, `minutes-${randomUUID().slice(0, 8)}.docx`);
    writeMinutesDocx("Meeting minutes", minutes, path, {
      subtitle: options.subtitle,
      // The model supplies the prose and code supplies the file, so the same
      // minutes always yield the same document.
      sections: parseMinutesSections(minutes),
      transcript: transcript.turns,
      assistantLabel: options.assistantLabel,
      callerLabel: options.callerLabel,
    });
    logger.info(`standin: minutes document saved to ${path}`);
    return path;
  } catch (err) {
    logger.warn(
      `standin: the minutes document could not be written: ${String(err)}`,
    );
    return undefined;
  }
}

// ----------------------------------------------- what the model wrote back

/** One headed block of minutes: a heading, and the lines under it. */
export interface MinutesSection {
  readonly heading: string;
  readonly items: readonly string[];
}

/**
 * A heading, form one: one to six hashes FOLLOWED BY whitespace. A model writes
 * "#launch" as a tag and "# Launch" as a heading, and the space is the only
 * thing that tells the two apart.
 */
const HEADING_HASHES = /^#{1,6}\s+(.*\S)\s*$/;

/** A heading, form two: a line that is bold end to end, colon optional. */
const HEADING_BOLD = /^\*\*(.+?)\*\*:?\s*$/;

/** Every bullet marker a model reaches for, in one expression. */
const BULLET = /^(?:[-*•]|\d+[.)])\s+(.*\S)\s*$/;

/** A marker with nothing after it. Not an item, and not a heading either. */
const BARE_MARKER = /^(?:[-*•]|\d+[.)])$/;

/** Where content that arrived before any heading is filed. */
const SYNTHETIC_HEADING = "Summary";

/**
 * Read a model's markdown minutes into sections, for the document writer.
 *
 * Pure and total: every line of the input reaches the output, no line is
 * dropped silently, and nothing here reads or writes anything.
 *
 * ```ts
 * parseMinutesSections("### Decisions\n- the launch moves to March");
 * // [{ heading: "Decisions", items: ["the launch moves to March"] }]
 * ```
 *
 * Every form a summarising model actually emits is accepted. Asked for
 * "### Key points" it returns "## Key points", "# Key points" or
 * "**Key points:**" depending on the model and the day, and accepting one form
 * only produced a single unheaded blob: the document still built, with every
 * section break gone and nothing raised anywhere.
 *
 * The same goes for bullets. Models mix "- ", "* ", "• " and "1. " inside one
 * answer, and often write a whole section as one prose paragraph with no bullet
 * at all, so a line under a heading that carries no marker is kept as written
 * rather than discarded.
 *
 * Content that arrives before any heading opens a section called "Summary",
 * because a model that ignores the format instruction and answers in one
 * paragraph would otherwise parse to nothing and produce a document with a
 * title and no body.
 *
 * Sections with no items survive here on purpose. Omitting them is the
 * DOCUMENT's job ({@link writeMinutesDocx}), which keeps this function
 * round-trippable and leaves one place that decides what is worth printing.
 */
export function parseMinutesSections(text: string): MinutesSection[] {
  const sections: MinutesSection[] = [];
  let heading: string | undefined;
  let items: string[] = [];

  const open = (next: string): void => {
    // A repeated heading opens a second section rather than merging into the
    // first: source order is the only order a reader can check against.
    if (heading !== undefined) sections.push({ heading, items });
    heading = next;
    items = [];
  };

  for (const raw of (text ?? "").split("\n")) {
    const line = raw.trim();
    if (line === "") continue;

    const found = HEADING_HASHES.exec(line) ?? HEADING_BOLD.exec(line);
    const title = found?.[1];
    if (title !== undefined) {
      open(title.replace(/:$/, "").trim());
      continue;
    }

    if (BARE_MARKER.test(line)) continue;
    const item = (BULLET.exec(line)?.[1] ?? line).trim();
    if (item === "") continue;
    if (heading === undefined) open(SYNTHETIC_HEADING);
    items.push(item);
  }

  if (heading !== undefined) sections.push({ heading, items });
  return sections;
}

/**
 * Speaker attribution the text already carries: a name, a colon and a space.
 *
 * ```ts
 * hasSpeakerPrefix("Sara: we should ship on Friday"); // true
 * ```
 *
 * Only for the compatibility case where a caller hands in turns with the name
 * baked into the text. A {@link Turn} carries its speaker in its own field,
 * which is better, and needs no test.
 *
 * A leading colon and a leading space are both rejected, so ": ok" and
 * " Sara: ok" are not mistaken for attribution.
 */
export function hasSpeakerPrefix(text: string): boolean {
  return /^[^\s:][^:]*:\s/.test(text ?? "");
}

// ------------------------------------------------------------------ the .docx

const CONTENT_TYPES =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
  '<Default Extension="rels" ' +
  'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
  '<Default Extension="xml" ContentType="application/xml"/>' +
  '<Override PartName="/word/document.xml" ' +
  'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.' +
  'document.main+xml"/>' +
  "</Types>";

const RELATIONSHIPS =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  '<Relationship Id="rId1" ' +
  'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" ' +
  'Target="word/document.xml"/></Relationships>';

function escapeXml(text: string): string {
  // The ampersand goes first, or the four replacements after it are escaped a
  // second time and the document reads "&amp;lt;".
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

/**
 * The document's own relationships part, with no relationships in it.
 *
 * Nothing in these minutes points at anything - no images, no hyperlinks, no
 * styles part - but validators refuse a part that has no rels part at all, and
 * a document Word repairs on open is a document nobody trusts again.
 */
const DOCUMENT_RELATIONSHIPS =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>';

/**
 * A4, with margins a person would recognise.
 *
 * Without it Word opens the file at Letter with no margins, which is the first
 * thing anyone notices about a document they were asked to keep.
 */
const A4_SECTION =
  "<w:sectPr>" +
  '<w:pgSz w:w="11906" w:h="16838"/>' +
  '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" ' +
  'w:header="708" w:footer="708" w:gutter="0"/>' +
  "</w:sectPr>";

/** No numbering part is worth carrying for one glyph. */
const BULLET_PREFIX = "• ";

/** The transcript's own heading in the document. */
const TRANSCRIPT_HEADING = "Attributed transcript";

/**
 * One paragraph. `xml:space` is on every run, or Word collapses the bullet's
 * own space and every indent with it.
 */
function paragraph(text: string, spacing = "", runProperties = ""): string {
  const pPr = spacing === "" ? "" : `<w:pPr>${spacing}</w:pPr>`;
  const rPr = runProperties === "" ? "" : `<w:rPr>${runProperties}</w:rPr>`;
  return `<w:p>${pPr}<w:r>${rPr}<w:t xml:space="preserve">${escapeXml(text)}</w:t></w:r></w:p>`;
}

function titleParagraph(text: string): string {
  return paragraph(
    text,
    '<w:spacing w:after="120"/>',
    '<w:b/><w:sz w:val="40"/>',
  );
}

function headingParagraph(text: string): string {
  return paragraph(
    text,
    '<w:spacing w:before="200" w:after="80"/>',
    '<w:b/><w:sz w:val="28"/>',
  );
}

function bodyParagraph(text: string): string {
  return paragraph(text);
}

/** Options for {@link writeMinutesDocx}. All optional: old calls still hold. */
export interface WriteMinutesDocxOptions {
  /** One line under the title, naming the call. */
  subtitle?: string;
  /**
   * Sections from {@link parseMinutesSections}. Given these, the `minutes`
   * string is not read line by line: these are what gets written.
   */
  sections?: readonly MinutesSection[];
  /**
   * Turns to write up as an attributed transcript after the sections. A turn is
   * named by its own speaker; one recorded as "assistant" takes
   * `assistantLabel`, and one with no speaker at all takes `callerLabel`.
   */
  transcript?: Iterable<Turn>;
  /** What the agent is called. Default "Assistant". */
  assistantLabel?: string;
  /** What an unnamed caller is called. Default "Caller". */
  callerLabel?: string;
}

/**
 * Write minutes to a Word-openable document, with no dependencies.
 *
 * ```ts
 * writeMinutesDocx("Meeting minutes", minutes, path, {
 *   subtitle: "Call with Dana - ~12 min, 3 human participants.",
 *   sections: parseMinutesSections(minutes),
 *   transcript: transcript.turns,
 * });
 * ```
 *
 * A .docx is a zip of four XML parts, and emitting them directly is a few dozen
 * lines. A document format library would be a dependency every install pays for
 * so that the small fraction who ask for minutes get a file, which is the wrong
 * trade for an SDK.
 *
 * With no options it behaves as it always has: markdown emphasis around a whole
 * line becomes a bold heading, because that is what a model reaches for.
 *
 * A section whose items are all blank is left out entirely, heading and all. A
 * bare "Decisions" over white space reads as a section the agent failed to
 * fill, rather than one that had nothing in it.
 */
export function writeMinutesDocx(
  title: string,
  minutes: string,
  path: string,
  options: WriteMinutesDocxOptions = {},
): void {
  const paragraphs = [titleParagraph(title)];
  if (options.subtitle) paragraphs.push(bodyParagraph(options.subtitle));

  if (options.sections === undefined) {
    for (const raw of minutes.split("\n")) {
      const line = raw.trim();
      if (line === "") continue;
      // A line that is bold end to end is the heading a model reaches for, and
      // it is set as one: a document whose headings are sized on one path and
      // not on the other is two documents.
      const heading =
        line.startsWith("**") && line.endsWith("**") && line.length > 4;
      const text = line.replaceAll(/^\*+|\*+$/g, "").trim();
      paragraphs.push(heading ? headingParagraph(text) : bodyParagraph(text));
    }
  } else {
    for (const section of options.sections) {
      const items = section.items
        .map((item) => (item ?? "").trim())
        .filter((item) => item !== "");
      if (items.length === 0) continue;
      paragraphs.push(headingParagraph(section.heading));
      for (const item of items)
        paragraphs.push(bodyParagraph(`${BULLET_PREFIX}${item}`));
    }
  }

  const said = attributedTranscript(
    options.transcript,
    options.assistantLabel ?? "Assistant",
    options.callerLabel ?? "Caller",
  );
  if (said.length > 0) {
    paragraphs.push(headingParagraph(TRANSCRIPT_HEADING));
    for (const line of said) paragraphs.push(bodyParagraph(line));
  }

  const document =
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">' +
    `<w:body>${paragraphs.join("")}${A4_SECTION}</w:body></w:document>`;

  writeFileSync(
    path,
    zip([
      ["[Content_Types].xml", CONTENT_TYPES],
      ["_rels/.rels", RELATIONSHIPS],
      ["word/document.xml", document],
      ["word/_rels/document.xml.rels", DOCUMENT_RELATIONSHIPS],
    ]),
  );
}

/**
 * Who said what, one line per turn.
 *
 * This is the half a transcript-only recap tool cannot produce: unmixed audio
 * gave a real speaker per utterance, so the document can say who spoke. A turn
 * that already carries its own "Name: " prefix is written exactly as it came,
 * because re-labelling it would destroy the attribution and prefixing it again
 * ("Caller: Sara: ...") reads as a transcription error.
 */
function attributedTranscript(
  turns: Iterable<Turn> | undefined,
  assistantLabel: string,
  callerLabel: string,
): string[] {
  if (turns === undefined) return [];
  const lines: string[] = [];
  for (const turn of turns) {
    const said = (turn.text ?? "").trim();
    if (said === "") continue;
    const speaker = (turn.speaker ?? "").trim();
    if (turn.role === "assistant") {
      lines.push(`${assistantLabel}: ${said}`);
    } else if (hasSpeakerPrefix(said)) {
      lines.push(said);
    } else {
      lines.push(`${speaker === "" ? callerLabel : speaker}: ${said}`);
    }
  }
  return lines;
}

/**
 * The smallest zip that Word will open: deflated entries, local headers, a
 * central directory, an end record.
 *
 * Written out rather than reached for, because Node ships deflate but no zip
 * container, and the alternative is a dependency in the base install for the
 * sake of one optional document.
 */
function zip(entries: Array<[string, string]>): Buffer {
  const locals: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;

  for (const [name, content] of entries) {
    const nameBytes = Buffer.from(name, "utf8");
    const raw = Buffer.from(content, "utf8");
    const deflated = deflateRawSync(raw);
    const crc = crc32(raw);

    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0); // local file header
    local.writeUInt16LE(20, 4); // version needed
    local.writeUInt16LE(0, 6); // flags
    local.writeUInt16LE(8, 8); // deflate
    local.writeUInt16LE(0, 10); // time: fixed, so the same minutes zip byte for byte
    local.writeUInt16LE(33, 12); // date: 1980-01-01, the zip epoch
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(deflated.length, 18);
    local.writeUInt32LE(raw.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28); // no extra field
    locals.push(local, nameBytes, deflated);

    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0); // central directory header
    entry.writeUInt16LE(20, 4); // version made by
    entry.writeUInt16LE(20, 6); // version needed
    entry.writeUInt16LE(0, 8);
    entry.writeUInt16LE(8, 10);
    entry.writeUInt16LE(0, 12);
    entry.writeUInt16LE(33, 14);
    entry.writeUInt32LE(crc, 16);
    entry.writeUInt32LE(deflated.length, 20);
    entry.writeUInt32LE(raw.length, 24);
    entry.writeUInt16LE(nameBytes.length, 28);
    entry.writeUInt16LE(0, 30); // extra
    entry.writeUInt16LE(0, 32); // comment
    entry.writeUInt16LE(0, 34); // disk
    entry.writeUInt16LE(0, 36); // internal attributes
    entry.writeUInt32LE(0, 38); // external attributes
    entry.writeUInt32LE(offset, 42);
    central.push(entry, nameBytes);

    offset += local.length + nameBytes.length + deflated.length;
  }

  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); // end of central directory
  end.writeUInt16LE(0, 4); // this disk
  end.writeUInt16LE(0, 6); // directory's disk
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20); // no comment

  return Buffer.concat([...locals, directory, end]);
}

/** The zip checksum. A table would be faster; four small parts do not need it. */
function crc32(data: Buffer): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}
