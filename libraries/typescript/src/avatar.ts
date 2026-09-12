// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The avatar lane: the face the caller sees while your agent talks.
 *
 * StandIn renders the avatar tile. Your worker does not draw it, stream it, or
 * know how it is made - it sends two hints and the service does the rest:
 *
 * - {@link expression} names the emotion the face should wear.
 * - {@link speechMarks} carries the viseme timeline for one utterance, which is
 *   what makes the mouth match the words.
 *
 * Both are **additive and best-effort**. A service that does not implement one
 * ignores it, an unknown emotion falls back to neutral, and neither ever affects
 * the audio the caller hears.
 *
 * Reach for {@link speechMarks} whenever you can put a timeline behind it. Real
 * viseme timings from the provider are best, and an ESTIMATE spread over the
 * audio the turn actually sent is the right default on a realtime path, where no
 * provider offers timings at all: a mouth on a measured clock beats a still one.
 * What is still worse than none is a timeline whose DURATION was guessed, from
 * text length or a words-per-minute rate, because that one drifts away from the
 * voice as the sentence goes on. `lipsync.ts` is where both live.
 *
 * {@link inferEmotion} and {@link ExpressionCue} sit here rather than in their
 * own module because what they produce is {@link expression}'s argument: the
 * first reads an emotion out of the reply text, the second decides when that is
 * worth sending.
 *
 * Identical in shape to the Python SDK's `standin.avatar`, translated to TS
 * naming.
 */

import { encode } from "./protocolRuntime.js";
import { TYPE_EXPRESSION, TYPE_SPEECH_MARKS } from "./protocol.js";

/**
 * The emotions StandIn knows by name. An open set: sending something else is
 * allowed and renders as neutral, so a newer sender and an older service still
 * interoperate.
 */
export const EMOTIONS = [
  "neutral",
  "happy",
  "sad",
  "surprised",
  "thinking",
] as const;

/** A well-known emotion, or any other string the service may learn later. */
export type Emotion = (typeof EMOTIONS)[number] | (string & {});

/**
 * Visemes use the Azure Speech numbering, 0 to 21, which is what the avatar
 * expects. A mark outside that range is dropped rather than sent.
 */
export const MAX_VISEME_ID = 21;

/**
 * An emotion reaches the avatar tile, and the string usually came from a model
 * that whoever is on the call is steering. Bounded here, where the message is
 * built, so a plugin cannot forget to bound it.
 */
export const MAX_EMOTION_CHARS = 40;

/** One viseme mark: milliseconds from the start of the utterance, and which mouth shape to hold. */
export interface SpeechMark {
  /** Time offset in ms, relative to the utterance's audio start. */
  readonly tMs: number;
  /** Viseme id in the Azure Speech numbering, 0-21. */
  readonly visemeId: number;
}

/**
 * Build an `expression`: the emotion the avatar should wear.
 *
 * Affects the video tile only, never the audio. Throws on an empty value or one
 * longer than {@link MAX_EMOTION_CHARS}; everything else is the service's to
 * interpret, and an unknown emotion renders as neutral.
 */
export function expression(emotion: Emotion): string {
  const text = emotion.trim();
  if (text === "") throw new Error("expression needs an emotion");
  if (text.length > MAX_EMOTION_CHARS) {
    throw new Error(
      `an emotion must be at most ${MAX_EMOTION_CHARS} characters`,
    );
  }
  return encode({ type: TYPE_EXPRESSION, emotion: text });
}

/** The emotions {@link inferEmotion} can read out of a reply. */
export type InferredEmotion = "surprised" | "sad" | "happy" | "neutral";

// Two or more of them together. One "?" is a question, "?!" is a reaction.
const SURPRISED_MARKS = /[?!]{2,}/;

// Word tests are \b-bounded throughout, or "nicety" reads as happy and
// "greatly" masks an apology.
const SURPRISED_WORDS =
  /\b(?:wow|whoa|woah|oh no|oh my|no way|unbelievable|incredible|astonish\w*|surpris\w*)\b/;
const SAD_WORDS =
  /\b(?:sorry|apolog\w*|unfortunately|regret\w*|afraid|sadly|bad news|failed|unable to|i can't|i cannot|i'm unable)\b/;
const HAPPY_WORDS =
  /\b(?:glad|great|awesome|wonderful|fantastic|excellent|congrat\w*|happy|love|perfect|good news|success\w*|thank\w*|welcome|nice|well done)\b/;

/**
 * Read an emotion out of a reply, for {@link expression} to carry.
 *
 * A lexicon rather than a model: it runs on every turn of a live call, so it has
 * to cost nothing and add no latency. First match wins in priority order,
 * surprised then sad then happy, which is deliberate - a "wow!" must not be
 * masked by a polite "thanks", and an apology must not be masked by an
 * incidental "nice". A reply that is genuinely mixed resolves to the
 * higher-priority one, and on a streaming path the next chunk re-reads it.
 *
 * **The lexicon is English only.** An Arabic or other non-English reply always
 * infers "neutral". That is by design and not a bug to work around: a language
 * guess that got it wrong would put the wrong face on, and neutral is always a
 * safe face.
 */
export function inferEmotion(text: string | null | undefined): InferredEmotion {
  const raw = (text ?? "").trim();
  if (raw === "") return "neutral";
  if (SURPRISED_MARKS.test(raw)) return "surprised";
  // Models emit the typographic apostrophe routinely, and "I can’t" is the most
  // common apologetic phrasing there is.
  const words = raw.toLowerCase().replace(/’/g, "'");
  if (SURPRISED_WORDS.test(words)) return "surprised";
  if (SAD_WORDS.test(words)) return "sad";
  if (HAPPY_WORDS.test(words)) return "happy";
  return "neutral";
}

/**
 * Decides WHEN an emotion is worth sending, so the face changes and nothing
 * else does.
 *
 * One instance per call. Both methods return the emotion to hand to
 * {@link expression}, or null when nothing should be sent:
 *
 * ```ts
 * const cue = new ExpressionCue();
 * // on every assistant transcript, partial and final alike
 * const emotion = cue.cue(chunk);
 * if (emotion !== null) await call.express(emotion);
 *
 * // around a tool that keeps the caller waiting
 * const waiting = cue.thinking(true);
 * try {
 *   ...
 * } finally {
 *   const done = cue.thinking(false);
 * }
 * ```
 *
 * Re-cue on partials: waiting for the final transcript leaves the face stale
 * for the whole time an apologetic reply is already being spoken. The de-dupe
 * on the last value sent is what keeps a word-by-word stream from sending
 * dozens of identical cues.
 */
export class ExpressionCue {
  #lastSent: Emotion | null = null;
  #thinking = false;

  /** The emotion last handed out, or null while none has been. */
  get lastSent(): Emotion | null {
    return this.#lastSent;
  }

  /**
   * Read `text` and return the emotion to send, or null when it is unchanged.
   *
   * Answers null for everything while {@link thinking} is on, so a transcript
   * chunk arriving mid-tool cannot make the avatar look done while it is still
   * working.
   */
  cue(text: string | null | undefined): Emotion | null {
    if (this.#thinking) return null;
    const emotion = inferEmotion(text);
    if (emotion === this.#lastSent) return null;
    this.#lastSent = emotion;
    return emotion;
  }

  /**
   * Enter or leave the waiting face around a tool call.
   *
   * Only transitions produce anything; setting the same state twice is a no-op.
   * Leaving it returns "neutral" when the thinking face is still the last thing
   * sent, because the model may stay silent after a tool result and there would
   * be no transcript left to re-cue from. Call it from a `finally` so a tool
   * that threw still puts the face back.
   */
  thinking(on: boolean): Emotion | null {
    if (on === this.#thinking) return null;
    this.#thinking = on;
    if (on) {
      this.#lastSent = "thinking";
      return "thinking";
    }
    if (this.#lastSent !== "thinking") return null;
    this.#lastSent = "neutral";
    return "neutral";
  }
}

/**
 * Build a `speech.marks`: the viseme timeline for one utterance.
 *
 * Marks are sorted ascending and marks outside the viseme range are dropped,
 * because the avatar reads the timeline in order and one bad entry would
 * desynchronise the mouth for the rest of the utterance.
 */
export function speechMarks(marks: Iterable<SpeechMark>): string {
  const cleaned = [...marks]
    .filter((m) => m.tMs >= 0 && m.visemeId >= 0 && m.visemeId <= MAX_VISEME_ID)
    .map((m) => ({ tMs: Math.trunc(m.tMs), visemeId: Math.trunc(m.visemeId) }))
    .sort((a, b) => a.tMs - b.tMs || a.visemeId - b.visemeId);
  // A reserved timeline anchor the lane does not use yet. Both SDKs send 0.
  return encode({ type: TYPE_SPEECH_MARKS, ts: 0, marks: cleaned });
}
