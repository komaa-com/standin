// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Lip-sync: the viseme timeline that makes the avatar's mouth match the words.
 *
 * A realtime speech-to-speech model streams voice and hands back no phoneme
 * timings, so {@link speechMarks} has nothing to carry and the mouth never moves
 * on the default path. This module closes that gap the only honest way open to a
 * worker: walk the spoken text, turn each character into a mouth shape, and
 * spread those shapes over the duration of the audio the worker ACTUALLY SENT
 * for that turn.
 *
 * Spreading over sent audio is the whole reason the estimate is worth sending.
 * The shapes are approximate either way, but the timeline is pinned to a length
 * the worker measured, byte by byte, as the frames went out: the mouth opens
 * when the voice starts and closes when it stops, on a long sentence and a short
 * one alike. {@link TurnLipSync} is that counter, and it is the piece to reach
 * for first.
 *
 * A duration GUESSED from text length or a words-per-minute rate is a different
 * proposition, and it is still worse than sending nothing. Its error compounds
 * sentence after sentence, so the mouth drifts further from the voice the longer
 * the call runs, and a mouth moving against the voice reads as broken in a way a
 * still mouth never does. Measured beats still; guessed loses to still.
 *
 * Better again, when the speech provider hands back per-character timings, is
 * {@link visemesFromAlignment}: same table, real times, no estimate at all.
 *
 * The table covers Latin and Arabic in one map. Without the Arabic rows an
 * Arabic reply produces no tokens, carries no timeline, and the mouth simply
 * does not move for half the people who will be on these calls.
 *
 * Timing anchor: `tMs` counts from the START of that turn's audio, never from
 * the moment the message reaches the service. Where the duration is known before
 * playback (a text-to-speech path) send the timeline ahead of the first audio
 * frame. On a realtime path it is known only once the turn ends, so the marks
 * necessarily go out after the last chunk was handed over, and that is correct
 * only while the service still holds the turn's audio buffered for playout.
 *
 * Identical in shape to the Python SDK's `standin.lipsync`.
 */

import { BYTES_PER_SAMPLE } from "./audio.js";
import type { SpeechMark } from "./avatar.js";
import { SAMPLE_RATE_HZ } from "./protocol.js";

/**
 * A closed mouth, and what the space between two words becomes. Every other
 * unmapped character is skipped instead: punching silence into "3.5%" would
 * close the mouth in the middle of a spoken number.
 */
export const SILENCE_VISEME = 0;

/**
 * A viseme id no character can carry, so the first token of a turn always counts
 * as a change. Starting the run collapser at 0 instead would swallow a leading
 * silence mark, which is what anchors the mouth shut before the first vowel.
 */
const NO_VISEME = -1;

// Mouth shape to every character that wears it, in the avatar lane's 0 to 21
// numbering. All 26 Latin letters and all 28 Arabic letters are here on purpose:
// one common letter left out thins the timeline unevenly and the mouth stalls on
// that syllable. The Arabic rows carry the eight variant forms too.
const SHAPES: ReadonlyArray<readonly [number, string]> = [
  [2, "a"],
  [2, "اأإآىة"], // alef, with hamza above and below, madda, maqsura, teh marbuta
  [2, "َ"], // fatha
  [4, "e"],
  [6, "iy"],
  [6, "يئ"], // yeh, yeh with hamza
  [6, "ِ"], // kasra
  [7, "uw"],
  [7, "و"], // waw
  [7, "ُ"], // damma
  [8, "o"],
  [12, "h"],
  [12, "هحعءؤ"], // heh, hah, ain, hamza, waw with hamza
  [13, "r"],
  [13, "ر"], // reh
  [14, "l"],
  [14, "ل"], // lam
  [15, "szx"],
  [15, "سصز"], // seen, sad, zain
  [16, "j"],
  [16, "شج"], // sheen, jeem
  [18, "fv"],
  [18, "ف"], // feh
  [19, "tdn"],
  [19, "تدنطضثذظ"], // teh, dal, noon, tah, dad, theh, thal, zah
  [20, "kgcq"],
  [20, "كقغخ"], // kaf, qaf, ghain, khah
  [21, "mbp"],
  [21, "مب"], // meem, beh
];

function buildTable(): Record<string, number> {
  // No prototype, so a lookup of "constructor" or "__proto__" answers undefined
  // the way every other unmapped character does. A plain object literal would
  // hand those two an inherited value instead, and the Python twin returns None
  // for both.
  const table: Record<string, number> = Object.create(null) as Record<
    string,
    number
  >;
  for (const [visemeId, chars] of SHAPES) {
    for (const ch of chars) table[ch] = visemeId;
  }
  return table;
}

/**
 * Character to mouth shape, read-only. Sukun, shadda, tanween, the tatweel and
 * every presentation form are deliberately absent: a stretch mark and a doubling
 * mark carry no mouth shape of their own, and mapping them would insert phantom
 * mouth changes into an otherwise correct timeline.
 */
export const CHAR_VISEMES: Readonly<Record<string, number>> =
  Object.freeze(buildTable());

/**
 * The mouth shape a character wears, or undefined when it has none.
 *
 * The lookup is on the raw character after lowercasing, with no Unicode
 * normalization at all: adding NFKC here would change which characters map and
 * the two SDKs would disagree on the same string. Digits, punctuation, the
 * tatweel and the non-vowel diacritics come back undefined, and every caller
 * skips them rather than holding the mouth closed over them.
 */
export function visemeForChar(ch: string): number | undefined {
  return CHAR_VISEMES[ch.toLowerCase()];
}

/** One character's token: silence for a space, the map for anything else. */
function tokenFor(ch: string): number | undefined {
  return ch === " " ? SILENCE_VISEME : visemeForChar(ch);
}

/**
 * Collapse runs, then make the times strictly increasing.
 *
 * Two rules, both about what a renderer can actually use. A run of one shape
 * ("mmm") is one mouth position, so only a CHANGE earns a mark, timed at the
 * first character of the run; a mark per character would multiply the payload
 * for an identical rendering. And when a long sentence is spread over a very
 * short buffer the step falls under half a millisecond and neighbouring marks
 * round onto the same one: the later shape wins, because a shape held for zero
 * milliseconds is not renderable, and because {@link speechMarks} sorts by
 * (tMs, visemeId) and would otherwise pick a different winner than the one the
 * walk ended on.
 */
function timeline(marks: readonly SpeechMark[]): SpeechMark[] {
  const kept: SpeechMark[] = [];
  let previous = NO_VISEME;
  for (const mark of marks) {
    if (mark.visemeId === previous) continue;
    previous = mark.visemeId;
    const last = kept[kept.length - 1];
    if (last !== undefined && mark.tMs <= last.tMs) {
      kept[kept.length - 1] = { tMs: last.tMs, visemeId: mark.visemeId };
      continue;
    }
    kept.push(mark);
  }
  return kept;
}

/**
 * Spread `text` over `durationMs` as a viseme timeline.
 *
 * Pass the duration of the audio you actually sent for the turn, which
 * {@link TurnLipSync} counts for you. Anything else is a guess, and a guessed
 * timeline is worse than no timeline at all.
 *
 * `text` is lowercased, its whitespace runs collapsed, and trimmed. Anything
 * that is not a positive finite duration returns no marks rather than being
 * divided by, because an infinite or not-a-number timestamp desynchronises the
 * mouth for the rest of the utterance and reaches the wire as a null.
 *
 * Returns `{tMs, visemeId}` marks, strictly increasing in time, ready for
 * {@link speechMarks}. Empty when there is nothing to say: no text, no duration,
 * or nothing in the text that has a mouth shape, which is the right answer for
 * "3.5%" or an emoji on its own.
 *
 * ```ts
 * const marks = estimateVisemes(finalTranscript, lipsync.durationMs);
 * if (marks.length > 0) await session.sendSpeechMarks(marks);
 * ```
 */
export function estimateVisemes(
  text: string | null | undefined,
  durationMs: number,
): SpeechMark[] {
  const normalized = (text ?? "").toLowerCase().replace(/\s+/g, " ").trim();
  if (normalized === "" || !Number.isFinite(durationMs) || durationMs <= 0)
    return [];

  // for..of walks whole characters, so an astral one (an emoji, a rare sign) is
  // skipped once instead of being read as two broken halves.
  const tokens: number[] = [];
  for (const ch of normalized) {
    const token = tokenFor(ch);
    if (token !== undefined) tokens.push(token);
  }
  if (!tokens.some((token) => token !== SILENCE_VISEME)) return [];

  const step = durationMs / tokens.length;
  return timeline(
    tokens.map((visemeId, i) => ({ tMs: Math.round(i * step), visemeId })),
  );
}

/**
 * Build the timeline from per-character timings the speech provider gave you.
 *
 * Real times are strictly better than an estimate and cost nothing when the
 * provider already returns them, so prefer this whenever a synthesis call can
 * hand back an alignment. Core takes the two plain arrays: `characters` as the
 * provider spoke them, and `startTimesSeconds` counting from the start of the
 * utterance. Normalising a vendor's field names is the speech plugin's job.
 *
 * Returns marks, or an empty array when the alignment holds no mouth shape at
 * all (all spaces, all punctuation). Fall back to {@link estimateVisemes} on an
 * EMPTY result rather than on a missing alignment: a provider that returns
 * timings for punctuation only has an alignment and still needs the estimate.
 *
 * Ragged arrays are tolerated: the walk stops at the shorter of the two.
 * Providers do return mismatched lengths, and throwing there would lose the turn
 * over a cosmetic hint.
 */
export function visemesFromAlignment(
  characters: readonly string[],
  startTimesSeconds: readonly number[],
): SpeechMark[] {
  const count = Math.min(characters.length, startTimesSeconds.length);
  const marks: SpeechMark[] = [];
  let spoke = false;

  for (let i = 0; i < count; i += 1) {
    const seconds = startTimesSeconds[i]!;
    // A time that is not a finite number is provider sloppiness of the same
    // class as a ragged array, so it costs its own mark and nothing else.
    if (!Number.isFinite(seconds)) continue;
    const token = tokenFor(characters[i]!);
    if (token === undefined) continue;
    spoke = spoke || token !== SILENCE_VISEME;
    marks.push({
      tMs: Math.max(0, Math.round(seconds * 1000)),
      visemeId: token,
    });
  }
  return spoke ? timeline(marks) : [];
}

/** How {@link TurnLipSync} reads the buffers it is handed. */
export interface TurnLipSyncOptions {
  /**
   * The rate of the PCM16 mono buffers passed to {@link TurnLipSync.audioSent}.
   * Defaults to the wire's own rate, which is what a plugin sending frames to
   * the call is holding.
   */
  sampleRateHz?: number;
}

/**
 * Counts the audio one turn actually sent, then times the mouth to it.
 *
 * Feed it every buffer you hand to the call, and ask it for the timeline when
 * that turn's text is final. It resets itself, so the next turn starts from
 * zero:
 *
 * ```ts
 * const lipsync = new TurnLipSync();
 *
 * // the audio sink
 * await session.sendAudio(chunk);
 * lipsync.audioSent(chunk);
 *
 * // the final transcript only
 * if (isFinal) {
 *   const marks = lipsync.finish(text);
 *   if (marks.length > 0) await session.sendSpeechMarks(marks);
 * }
 *
 * // playback cancelled
 * lipsync.cancel();
 * ```
 *
 * Emit once per turn, on the final transcript. A partial would send an
 * ever-lengthening timeline several times over and the avatar would restart the
 * mouth mid-sentence.
 *
 * {@link cancel} is not optional. On a barge-in the service drops audio the
 * caller never heard, and a counter that keeps those milliseconds spreads the
 * next turn's text over its own audio plus the discarded audio: the mouth runs
 * long for the whole of that turn and every turn after it.
 */
export class TurnLipSync {
  readonly #sampleRateHz: number;
  #durationMs = 0;

  constructor(options: TurnLipSyncOptions = {}) {
    this.#sampleRateHz = options.sampleRateHz ?? SAMPLE_RATE_HZ;
  }

  /** Milliseconds of audio sent for the turn in progress. Starts at 0. */
  get durationMs(): number {
    return this.#durationMs;
  }

  /** Add one PCM16 mono buffer that has gone out to the call. */
  audioSent(pcm: Uint8Array): void {
    this.audioSentMs(
      (pcm.length / BYTES_PER_SAMPLE / this.#sampleRateHz) * 1000,
    );
  }

  /**
   * Add a duration directly, for a sink that hands over encoded audio.
   *
   * Rounded per chunk rather than kept as a running float, so both SDKs
   * accumulate the same integer for the same stream of chunks. A chunk that
   * measures as nothing, or as no number at all, is ignored rather than taking
   * the turn's count with it.
   */
  audioSentMs(ms: number): void {
    if (!Number.isFinite(ms) || ms <= 0) return;
    this.#durationMs += Math.round(ms);
  }

  /** Drop the count on a barge-in or a playback cancel, emitting nothing. */
  cancel(): void {
    this.#durationMs = 0;
  }

  /**
   * Return the turn's timeline and reset the counter.
   *
   * Empty when no audio was sent or the text carries no mouth shape, and a
   * caller sends nothing in that case. The reset happens either way: the next
   * turn must not inherit these milliseconds.
   */
  finish(text: string | null | undefined): SpeechMark[] {
    const marks = estimateVisemes(text, this.#durationMs);
    this.#durationMs = 0;
    return marks;
  }
}
