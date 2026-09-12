// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Moving the mouth with no help from the provider.
 *
 * Two things decide whether this is worth sending at all: the timeline is spread
 * over audio that was MEASURED, and the table covers the languages people
 * actually speak on these calls. Both are asserted here, and so is the parity of
 * the table with the Python SDK's, because a mouth that moves differently in two
 * SDKs is a bug nobody sees until a customer switches.
 *
 * The Python twin is `tests/test_lipsync.py`.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import type { SpeechMark } from "./avatar.js";
import { MAX_VISEME_ID, speechMarks } from "./avatar.js";
import {
  CHAR_VISEMES,
  SILENCE_VISEME,
  TurnLipSync,
  estimateVisemes,
  visemeForChar,
  visemesFromAlignment,
} from "./lipsync.js";

const LATIN = "abcdefghijklmnopqrstuvwxyz";
const ARABIC = "ابتثجحخدذرزسشصضطظعغفقكلمنهوي";
const ARABIC_VARIANTS = "أإآىةئؤء";

function mark(tMs: number, visemeId: number): SpeechMark {
  return { tMs, visemeId };
}

describe("the estimate", () => {
  it("spreads the shapes over the audio that was actually sent", () => {
    // Five characters, one second of voice: the mouth opens with the voice and
    // closes with it, which is the only claim this estimate makes.
    expect(estimateVisemes("hello", 1000)).toEqual([
      mark(0, 12),
      mark(200, 4),
      mark(400, 14),
      mark(800, 8),
    ]);
  });

  it("normalizes case and whitespace before it walks", () => {
    expect(estimateVisemes("  HEL\tLO\n", 1000)).toEqual(
      estimateVisemes("hel lo", 1000),
    );
  });

  it("sends nothing when there is no text", () => {
    expect(estimateVisemes("", 1000)).toEqual([]);
    expect(estimateVisemes("   \n ", 1000)).toEqual([]);
    expect(estimateVisemes(null, 1000)).toEqual([]);
  });

  it("sends nothing when no audio was sent", () => {
    // Dividing by it would put Infinity on every mark and desynchronise the
    // mouth for the rest of the utterance.
    expect(estimateVisemes("hello", 0)).toEqual([]);
    expect(estimateVisemes("hello", -20)).toEqual([]);
  });

  it("sends nothing for a duration that is not a finite number", () => {
    // An infinite duration makes every mark time NaN or Infinity, and the
    // builder serialises those as null: a malformed timeline on the wire is
    // worse than the still mouth this returns instead.
    expect(estimateVisemes("hello", Number.POSITIVE_INFINITY)).toEqual([]);
    expect(estimateVisemes("hello", Number.NaN)).toEqual([]);
  });

  it("holds one shape for a run instead of one mark per letter", () => {
    // "mmm" is one mouth position. Three marks would render identically and
    // cost three times the payload.
    expect(estimateVisemes("mmm", 300)).toEqual([mark(0, 21)]);
  });

  it("closes the mouth between words", () => {
    expect(estimateVisemes("a b", 300)).toEqual([
      mark(0, 2),
      mark(100, SILENCE_VISEME),
      mark(200, 21),
    ]);
  });

  it("skips a digit rather than closing the mouth over it", () => {
    // Silence on every unmapped character punches a hole of closed-mouth frames
    // into the middle of a spoken number.
    expect(estimateVisemes("a3e", 200)).toEqual([mark(0, 2), mark(100, 4)]);
  });

  it("sends nothing for text that has no mouth shape at all", () => {
    expect(estimateVisemes("3.5%", 1000)).toEqual([]);
    expect(estimateVisemes("...", 1000)).toEqual([]);
  });

  it("gives an emoji no shape and no time of its own", () => {
    // Walking by code unit would hand the table half a surrogate pair twice.
    expect(estimateVisemes("a\u{1F600}e", 200)).toEqual([
      mark(0, 2),
      mark(100, 4),
    ]);
    expect(estimateVisemes("\u{1F600}", 200)).toEqual([]);
  });

  it("never puts two marks on the same millisecond", () => {
    // A long sentence over a very short buffer: the step falls below half a
    // millisecond and neighbouring marks round together. The later shape wins,
    // because a shape held for zero milliseconds is not renderable.
    const marks = estimateVisemes("abcdefghij", 1);
    expect(marks).toEqual([mark(0, 4), mark(1, 16)]);
    const times = marks.map((m) => m.tMs);
    expect(times).toEqual([...times].sort((a, b) => a - b));
    expect(new Set(times).size).toBe(times.length);
  });

  it("reaches the wire in the order it was estimated", () => {
    // The point of the strictly increasing rule. The builder re-sorts by
    // (tMs, visemeId), so two marks sharing a millisecond would come out in id
    // order and the mouth would hold a shape the walk never ended on.
    const marks = estimateVisemes("hello world", 3);
    const sent = JSON.parse(speechMarks(marks)).marks as SpeechMark[];
    expect(sent).toEqual(marks);
  });
});

describe("the table", () => {
  it("has a shape for every Latin letter", () => {
    // One common letter missing thins the timeline unevenly and the mouth
    // stalls on that syllable.
    for (const ch of LATIN) expect(visemeForChar(ch)).toBeTypeOf("number");
  });

  it("has a shape for every Arabic letter", () => {
    for (const ch of ARABIC) expect(visemeForChar(ch)).toBeTypeOf("number");
  });

  it("has a shape for the variant forms real Arabic text is written with", () => {
    // Hamza carriers and teh marbuta end a large share of ordinary words, so
    // leaving them out thins an Arabic timeline exactly where it is busiest.
    for (const ch of ARABIC_VARIANTS)
      expect(visemeForChar(ch)).toBeTypeOf("number");
  });

  it("keeps every shape inside the range the avatar accepts", () => {
    // One id out of range is dropped at the builder, which leaves a hole in the
    // timeline rather than an error anyone would notice.
    for (const visemeId of Object.values(CHAR_VISEMES)) {
      expect(visemeId).toBeGreaterThanOrEqual(SILENCE_VISEME);
      expect(visemeId).toBeLessThanOrEqual(MAX_VISEME_ID);
    }
  });

  it("cannot be edited at runtime", () => {
    // Two SDKs agree on this map byte for byte. A caller that could mutate it
    // would make one call's mouth disagree with every other call's.
    expect(() => {
      (CHAR_VISEMES as Record<string, number>)["a"] = 9;
    }).toThrow();
    expect(CHAR_VISEMES["a"]).toBe(2);
  });

  it("hands out no shape for a name borrowed from the language itself", () => {
    // The table carries no prototype, so these answer like any other unmapped
    // input. Inheriting one would hand a caller a function where the signature
    // promises a viseme id, and the Python twin answers None for both.
    expect(visemeForChar("constructor")).toBeUndefined();
    expect(visemeForChar("__proto__")).toBeUndefined();
  });

  it("moves the mouth for an Arabic reply", () => {
    // Without the Arabic rows this is an empty timeline and a still face for
    // half the people who will be on these calls.
    expect(estimateVisemes("مرحبا", 500)).toEqual([
      mark(0, 21),
      mark(100, 13),
      mark(200, 12),
      mark(300, 21),
      mark(400, 2),
    ]);
  });

  it("reads the short vowels and ignores the marks that carry no shape", () => {
    expect(visemeForChar("َ")).toBe(2); // fatha
    expect(visemeForChar("ُ")).toBe(7); // damma
    expect(visemeForChar("ِ")).toBe(6); // kasra
    for (const ch of "ًٌٍّْ") expect(visemeForChar(ch)).toBeUndefined(); // sukun, shadda, tanween
  });

  it("skips the tatweel, which is a stretch and not a sound", () => {
    expect(estimateVisemes("بـ__ـب", 200)).toEqual([mark(0, 21)]);
  });

  it("reads a character as written, with no Unicode normalization", () => {
    // Folding presentation forms onto their base letter would change which
    // characters map, and the two SDKs would disagree on the same string.
    expect(visemeForChar("ﺑ")).toBeUndefined();
    expect(visemeForChar("ﻻ")).toBeUndefined();
  });

  it("lowercases before the lookup", () => {
    expect(visemeForChar("A")).toBe(2);
    expect(visemeForChar("M")).toBe(21);
  });

  it("hands out no shape for a character that has none", () => {
    expect(visemeForChar("7")).toBeUndefined();
    expect(visemeForChar("?")).toBeUndefined();
  });
});

describe("timings from the provider", () => {
  it("puts each shape on the provider's own time", () => {
    expect(
      visemesFromAlignment(["h", "e", "l", "l", "o"], [0, 0.1, 0.2, 0.25, 0.4]),
    ).toEqual([mark(0, 12), mark(100, 4), mark(200, 14), mark(400, 8)]);
  });

  it("walks the shorter array when the two do not match", () => {
    // Providers do return ragged arrays, and throwing there would lose the turn
    // over a cosmetic hint.
    expect(visemesFromAlignment(["h", "e", "l", "l", "o"], [0, 0.1])).toEqual([
      mark(0, 12),
      mark(100, 4),
    ]);
    expect(visemesFromAlignment(["h"], [0, 0.1, 0.2])).toEqual([mark(0, 12)]);
  });

  it("keeps the silence a leading space stands for", () => {
    // The run collapser starts on a sentinel no character can equal, or this
    // mark - the one that anchors the mouth shut - would be swallowed.
    expect(visemesFromAlignment([" ", "h", "i"], [0, 0.1, 0.2])).toEqual([
      mark(0, SILENCE_VISEME),
      mark(100, 12),
      mark(200, 6),
    ]);
  });

  it("sends nothing when the alignment is only punctuation", () => {
    // The empty result is the signal to fall back to the estimate, which is why
    // it has to be empty rather than a list of silences.
    expect(visemesFromAlignment(["!", ".", ","], [0, 0.1, 0.2])).toEqual([]);
    expect(visemesFromAlignment([" ", " "], [0, 0.1])).toEqual([]);
  });

  it("never times a mark before the utterance started", () => {
    expect(visemesFromAlignment(["h", "a"], [-0.5, 0.2])).toEqual([
      mark(0, 12),
      mark(200, 2),
    ]);
  });

  it("holds a run from the provider's first timing for it", () => {
    expect(visemesFromAlignment(["m", "m", "m"], [0, 0.1, 0.2])).toEqual([
      mark(0, 21),
    ]);
  });

  it("loses only its own mark when a timing is not a finite number", () => {
    // Provider sloppiness of the same class as a ragged array. Rounding it
    // instead would put a null on the wire in the middle of a good timeline.
    expect(visemesFromAlignment(["h", "a", "l"], [0, Number.NaN, 0.2])).toEqual(
      [mark(0, 12), mark(200, 14)],
    );
  });

  it("skips an unmapped character instead of closing the mouth on it", () => {
    // A digit inside a spoken number is not a pause, and silencing it here
    // would be the same hole the estimate is careful not to punch.
    expect(visemesFromAlignment(["a", "3", "b"], [0, 0.1, 0.2])).toEqual([
      mark(0, 2),
      mark(200, 21),
    ]);
  });
});

describe("the turn counter", () => {
  it("starts at nothing", () => {
    expect(new TurnLipSync().durationMs).toBe(0);
  });

  it("counts the audio it was handed", () => {
    // 640 bytes of PCM16 mono at the wire's rate is 20 ms, and the count is the
    // only duration a realtime worker genuinely knows.
    const lipsync = new TurnLipSync();
    lipsync.audioSent(Buffer.alloc(640));
    lipsync.audioSent(Buffer.alloc(640));
    expect(lipsync.durationMs).toBe(40);
  });

  it("counts a duration measured elsewhere", () => {
    const lipsync = new TurnLipSync();
    lipsync.audioSentMs(30.4);
    lipsync.audioSentMs(-5);
    expect(lipsync.durationMs).toBe(30);
  });

  it("ignores a chunk that measures as no number at all", () => {
    // One bad measurement would otherwise poison the counter for the rest of
    // the call, not just for the turn it arrived on.
    const lipsync = new TurnLipSync();
    lipsync.audioSentMs(40);
    lipsync.audioSentMs(Number.NaN);
    lipsync.audioSentMs(Number.POSITIVE_INFINITY);
    expect(lipsync.durationMs).toBe(40);
    expect(lipsync.finish("ab")).toEqual([mark(0, 2), mark(20, 21)]);
  });

  it("reads a buffer at the rate it was told about", () => {
    const lipsync = new TurnLipSync({ sampleRateHz: 24_000 });
    lipsync.audioSent(Buffer.alloc(960));
    expect(lipsync.durationMs).toBe(20);
  });

  it("emits the turn once and starts the next one from zero", () => {
    const lipsync = new TurnLipSync();
    lipsync.audioSentMs(60);
    expect(lipsync.finish("ab")).toEqual([mark(0, 2), mark(30, 21)]);
    expect(lipsync.durationMs).toBe(0);
    expect(lipsync.finish("ab")).toEqual([]);
  });

  it("forgets audio the caller never heard", () => {
    // On a barge-in the service drops queued audio. Keeping those milliseconds
    // stretches the NEXT turn's mouth over audio nobody heard.
    const lipsync = new TurnLipSync();
    lipsync.audioSentMs(500);
    lipsync.cancel();
    expect(lipsync.durationMs).toBe(0);
    expect(lipsync.finish("hello")).toEqual([]);
  });

  it("resets even when the turn produced no marks", () => {
    const lipsync = new TurnLipSync();
    lipsync.audioSentMs(200);
    expect(lipsync.finish("3.5%")).toEqual([]);
    expect(lipsync.durationMs).toBe(0);
  });
});

describe("parity with the Python SDK", () => {
  it("maps every character to the same shape the Python SDK does", () => {
    // Read from the Python source so the two tables cannot drift apart without
    // this failing. A mouth that moves differently in the two SDKs is a bug a
    // customer finds by switching language, not one CI finds by itself.
    const here = dirname(fileURLToPath(import.meta.url));
    const source = readFileSync(
      join(here, "..", "..", "python", "standin", "lipsync.py"),
      "utf8",
    );

    const python: Record<string, number> = {};
    // The twin groups its rows as (viseme, "characters") tuples; a plain
    // "c": viseme literal is the other shape that file could take.
    for (const [, visemeId, chars] of source.matchAll(
      /\(\s*(\d+)\s*,\s*"([^"]+)"\s*\)/g,
    )) {
      for (const ch of chars!) python[ch] = Number(visemeId);
    }
    for (const [, ch, visemeId] of source.matchAll(/"(\S)"\s*:\s*(\d+)/g)) {
      python[ch!] = Number(visemeId);
    }

    expect(Object.keys(python).length).toBeGreaterThan(60);
    expect(python).toEqual({ ...CHAR_VISEMES });
  });
});
