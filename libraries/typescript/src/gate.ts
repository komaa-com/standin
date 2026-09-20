// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Deterministic verbal interrupts: "stop", "hold on", "never mind".
 *
 * These are handled in code rather than left to the model because the model is
 * mid-generation when they arrive. Waiting for its own interruption handling is
 * what makes a cut feel late; matching the phrase ourselves and flushing the
 * caller's playback queue is what makes it feel instant.
 *
 * Matching is WHOLE-UTTERANCE only, after stripping filler and the configured
 * wake phrases, so "stop by the store on your way" never triggers.
 */

/** Normalized utterances that mean "stop talking / hold on". */
const INTERRUPT_PHRASES = new Set([
  "stop",
  "stop it",
  "stop talking",
  "wait",
  "wait wait",
  "wait a second",
  "wait a minute",
  "hold on",
  "hang on",
  "never mind",
  "nevermind",
  "be quiet",
  "quiet",
  "shut up",
  "enough",
  "thats enough",
  "pause",
  "one second",
  "one sec",
  "give me a second",
  // Arabic: the deterministic cut has to work in both call languages, because the
  // model mirrors the caller's. Whole-utterance matching plus the normalizer's
  // tashkeel stripping keep these as conservative as the English set.
  "توقف", // stop
  "قف", // halt
  "اسكت", // be quiet
  "اصمت", // silence
  "انتظر", // wait
  "انتظر لحظة", // wait a moment
  "استنى", // wait (colloquial)
  "استنا",
  "لحظة", // one moment
  "لحظه",
  "لحظة واحدة", // just a moment
  "ثانية", // one second
  "ثانيه",
  "ثانية واحدة",
  "دقيقة", // one minute
  "دقيقه",
  "مهلا", // hold on
  "خلاص", // enough / that is it
  "كفى", // enough
  "كفاية",
  "كفايه",
  "بس", // enough/stop (colloquial; safe only because matching is whole-utterance)
]);

/**
 * Leading and trailing filler stripped before matching ("ok stop", "no wait",
 * "stop please"; Arabic "طيب توقف" = "ok stop", "يا" = the vocative that precedes
 * a name).
 */
const FILLER_TOKENS = new Set([
  "ok",
  "okay",
  "oh",
  "no",
  "hey",
  "please",
  "now",
  "طيب",
  "لا",
  "يا",
]);

function normalizeWords(text: string | undefined): string[] {
  return (
    (text ?? "")
      .toLowerCase()
      .replace(/['’]/g, "") // "that's" -> "thats", not "that s"
      // Combining marks (Arabic tashkeel, accents) and the Arabic tatweel attach
      // INSIDE a word - delete them outright so "تَوَقَّف" normalizes to "توقف"
      // instead of splitting apart.
      .replace(/[\p{M}ـ]/gu, "")
      // Letters in ANY script survive: an Arabic "توقف" must cut as instantly as
      // "stop". Everything else separates words.
      .replace(/[^\p{L}\s]/gu, " ")
      .split(/\s+/)
      .filter(Boolean)
  );
}

function startsWithSeq(words: string[], seq: string[]): boolean {
  return (
    seq.length > 0 &&
    seq.length <= words.length &&
    seq.every((w, i) => words[i] === w)
  );
}

function endsWithSeq(words: string[], seq: string[]): boolean {
  const offset = words.length - seq.length;
  return (
    seq.length > 0 &&
    offset >= 0 &&
    seq.every((w, i) => words[offset + i] === w)
  );
}

/**
 * True when the utterance, AS A WHOLE, is a verbal interrupt.
 *
 * Deterministic and conservative: lowercase, punctuation stripped, surrounding
 * filler AND the configured wake phrases removed - "hey <name>, stop" must cut as
 * instantly as a bare "stop", because addressing the bot by name is how people
 * interrupt it in a meeting - then an exact phrase-set match capped at four
 * words. A longer sentence that merely contains "stop" does not match.
 */
export const DEFAULT_FOLLOW_UP_WINDOW_MS = 12_000;

/**
 * Microsoft's thread-id prefix for a meeting or channel conversation. A 1:1 call
 * has no such thread at all, so the presence of one is the signal.
 */
const MEETING_THREAD_PREFIX = "19:";

/**
 * Is this call attached to a Microsoft Teams meeting or channel thread?
 *
 * The group signal that actually arrives. The participant count that used to
 * carry this does not reach a bot that joined through the meeting, so a gate
 * keyed on it alone never fires.
 */
export function isMeetingThread(threadId: string | undefined): boolean {
  return (threadId ?? "").trim().startsWith(MEETING_THREAD_PREFIX);
}

/** The outcome for one finished caller turn. */
export interface GateDecision {
  /** Speak an answer to this turn. */
  readonly respond: boolean;
  /**
   * The turn named the assistant. Opens the follow-up window, and is worth
   * knowing separately from `respond`: a turn inside the window is answered
   * without having been addressed.
   */
  readonly addressed: boolean;
}

/**
 * Case-insensitive, word-boundary match of any wake phrase.
 *
 * Boundaries rather than substrings, so "assistant" matches and "assistants"
 * does not. The boundary is expressed as "not a word character" so a non-Latin
 * wake phrase behaves the same as a Latin one. An empty phrase list never
 * matches.
 */
export function isAddressed(
  transcript: string,
  wakePhrases: readonly string[],
): boolean {
  if (!transcript || wakePhrases.length === 0) return false;
  const lowered = transcript.toLowerCase();
  for (const raw of wakePhrases) {
    const phrase = raw.trim().toLowerCase();
    if (phrase === "") continue;
    const escaped = phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (
      new RegExp(`(?<![\\p{L}\\p{N}_])${escaped}(?![\\p{L}\\p{N}_])`, "u").test(
        lowered,
      )
    ) {
      return true;
    }
  }
  return false;
}

/** Options for {@link GroupGate}. */
export interface GroupGateOptions {
  wakePhrases: readonly string[];
  requireAddress?: boolean;
  followUpWindowMs?: number;
  threadId?: string;
}

/**
 * The group-call gate for one call. Holds the follow-up window's state.
 *
 * An agent in a meeting that answers every sentence is worse than one that says
 * nothing. The gate keeps it quiet until somebody names it, then leaves the
 * floor open for a short while so a follow-up does not need the name again.
 */
export class GroupGate {
  readonly wakePhrases: readonly string[];
  readonly requireAddress: boolean;
  readonly followUpWindowMs: number;
  readonly #meetingThread: boolean;
  #humanCount = 0;
  #lastAddressedMs: number | undefined;

  constructor(options: GroupGateOptions) {
    this.wakePhrases = options.wakePhrases;
    this.requireAddress = options.requireAddress ?? true;
    this.followUpWindowMs =
      options.followUpWindowMs ?? DEFAULT_FOLLOW_UP_WINDOW_MS;
    this.#meetingThread = isMeetingThread(options.threadId);
  }

  /** More than one human on this call, by either signal. */
  get isGroup(): boolean {
    return this.#meetingThread || this.#humanCount >= 2;
  }

  /**
   * Is the gate actually muting anything right now?
   *
   * A gate with no wake phrase configured can never be opened, so it would mute
   * the assistant for the whole call. Treat "no trigger configured" as gate off.
   */
  get active(): boolean {
    return (
      this.isGroup &&
      this.requireAddress &&
      this.wakePhrases.some((p) => p.trim() !== "")
    );
  }

  /**
   * Record a participant count from call context.
   *
   * Corroborating, never authoritative: a count that says 1 does not clear a
   * meeting thread. The count is the signal that goes missing, so it may add
   * certainty and must not remove it.
   */
  noteParticipants(count: number): void {
    this.#humanCount = Math.max(this.#humanCount, count);
  }

  /**
   * Give the floor back: the next turn has to name the assistant again.
   *
   * Call this when somebody asks the assistant for silence (see `isDismissal`).
   * Without it "be quiet" only cuts the sentence in progress, the follow-up
   * window it was said inside stays open, and the assistant answers the very
   * next thing anybody says. Not for every interrupt: somebody who says "wait"
   * is about to ask something, and still wants the answer. Harmless on a 1:1
   * call, where there is no window to close.
   */
  closeWindow(): void {
    this.#lastAddressedMs = undefined;
  }

  /** Answer this turn, or stay out of the meeting? */
  decide(transcript: string, nowMs: number): GateDecision {
    const addressed = isAddressed(transcript, this.wakePhrases);
    if (!this.active) return { respond: true, addressed };
    if (addressed) {
      this.#lastAddressedMs = nowMs;
      return { respond: true, addressed: true };
    }
    if (
      this.#lastAddressedMs !== undefined &&
      nowMs - this.#lastAddressedMs <= this.followUpWindowMs
    ) {
      return { respond: true, addressed: false };
    }
    return { respond: false, addressed: false };
  }
}

/** The utterance with filler and wake phrases peeled off both ends. */
function coreOf(text: string | undefined, wakePhrases?: string[]): string[] {
  let core = normalizeWords(text);
  const wake = (wakePhrases ?? [])
    .map(normalizeWords)
    .filter((seq) => seq.length > 0);
  // Strip surrounding wake and filler tokens until stable - they interleave
  // ("ok <name> please stop"). The wake phrase is tried BEFORE filler at each
  // end: a wake phrase may itself begin with a filler word ("hey assistant"),
  // and peeling "hey" first leaves "assistant", which no longer matches the
  // phrase it was part of.
  let changed = true;
  while (changed && core.length > 0) {
    changed = false;
    for (const seq of wake) {
      if (startsWithSeq(core, seq)) {
        core = core.slice(seq.length);
        changed = true;
      } else if (endsWithSeq(core, seq)) {
        core = core.slice(0, core.length - seq.length);
        changed = true;
      }
    }
    if (changed) continue;
    if (core.length > 0 && FILLER_TOKENS.has(core[0] ?? "")) {
      core.shift();
      changed = true;
    } else if (
      core.length > 0 &&
      FILLER_TOKENS.has(core[core.length - 1] ?? "")
    ) {
      core.pop();
      changed = true;
    }
  }
  return core;
}

export function isVerbalInterrupt(
  text: string | undefined,
  wakePhrases?: string[],
): boolean {
  const core = coreOf(text, wakePhrases);
  // The wake word alone ("<name>?") is an address, not an interrupt.
  if (core.length === 0 || core.length > 4) {
    return false;
  }
  return INTERRUPT_PHRASES.has(core.join(" "));
}

/**
 * The interrupts that ask for SILENCE rather than a pause. "wait" and "hold on"
 * come from somebody who is about to say more, and they still want the answer;
 * "be quiet" does not. Always a subset of the interrupt phrases.
 */
const DISMISSAL_PHRASES = new Set([
  "stop talking",
  "be quiet",
  "quiet",
  "shut up",
  "enough",
  "thats enough",
  "اسكت",
  "اصمت",
  "خلاص",
  "كفى",
  "كفاية",
  "كفايه",
  "بس",
]);

/**
 * Is this whole utterance a request for silence, not just a pause?
 *
 * Every dismissal is a verbal interrupt; not every interrupt is a dismissal.
 * Use it to decide whether to call `GroupGate.closeWindow()`: after "be quiet"
 * the assistant should wait to be named again, and after "wait" it should still
 * answer the question that follows.
 */
export function isDismissal(
  text: string | undefined,
  wakePhrases?: string[],
): boolean {
  const core = coreOf(text, wakePhrases);
  if (core.length === 0 || core.length > 4) return false;
  return DISMISSAL_PHRASES.has(core.join(" "));
}
