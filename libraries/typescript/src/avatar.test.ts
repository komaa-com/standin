// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The face, and when it is worth changing.
 *
 * The reading is a lexicon rather than a model, because it runs on every chunk
 * of every turn of a live call. What matters is that it never costs latency,
 * that priority beats scoring so an apology is not masked by an incidental
 * "nice", and that the cue holds the thinking face while a tool is still
 * working.
 *
 * The Python twin is `tests/test_lipsync.py`, which tests the reading alongside
 * the lip-sync half it was specified with.
 */

import { describe, expect, it } from "vitest";

import { EMOTIONS, ExpressionCue, expression, inferEmotion } from "./avatar.js";

describe("reading the emotion", () => {
  it("wears a neutral face when there are no words to read", () => {
    expect(inferEmotion("")).toBe("neutral");
    expect(inferEmotion("   \n ")).toBe("neutral");
    expect(inferEmotion(null)).toBe("neutral");
  });

  it("hears astonishment in the punctuation", () => {
    // A model writes a startled reply with punctuation far more often than it
    // writes "wow".
    expect(inferEmotion("It finished already?!")).toBe("surprised");
    expect(inferEmotion("Twelve thousand!!")).toBe("surprised");
  });

  it("leaves one exclamation mark alone", () => {
    expect(inferEmotion("Done!")).toBe("neutral");
  });

  it("reads the words when the punctuation is plain", () => {
    expect(inferEmotion("Wow, that was quick")).toBe("surprised");
    expect(inferEmotion("Sorry, the room is booked")).toBe("sad");
    expect(inferEmotion("That is wonderful")).toBe("happy");
  });

  it("takes the stem, so the tense does not matter", () => {
    expect(inferEmotion("that is surprising")).toBe("surprised");
    expect(inferEmotion("my apologies for the delay")).toBe("sad");
    expect(inferEmotion("congratulations to the team")).toBe("happy");
  });

  it("lets the stronger reading win instead of averaging them", () => {
    // An apology masked by an incidental "nice" is the failure a score would
    // produce and a priority order cannot.
    expect(inferEmotion("Sorry, that is great news otherwise")).toBe("sad");
    expect(inferEmotion("Wow, sorry, I misread that")).toBe("surprised");
  });

  it("does not fire on a word that merely contains one", () => {
    expect(inferEmotion("a nicety of the format")).toBe("neutral");
    expect(inferEmotion("greatly reduced")).toBe("neutral");
    expect(inferEmotion("lovely weather aside")).toBe("neutral");
  });

  it("matches the apostrophe a model actually types", () => {
    // The typographic one is what models emit, and "I can't" is the most common
    // apologetic phrasing there is.
    expect(inferEmotion("I can’t reach that calendar")).toBe("sad");
    expect(inferEmotion("I can't reach that calendar")).toBe("sad");
    expect(inferEmotion("I’m unable to do that")).toBe("sad");
  });

  it("stays neutral in a language the lexicon does not cover", () => {
    // A documented gap, not a bug: the viseme table is bilingual and this one is
    // not, and a wrong guess would put a confident wrong face on the tile.
    expect(inferEmotion("عذرا، لا استطيع فعل ذلك")).toBe("neutral");
  });

  it("only ever names an emotion the avatar knows", () => {
    for (const text of ["wow!!", "sorry", "great", "the third item"]) {
      const emotion = inferEmotion(text);
      expect(EMOTIONS).toContain(emotion);
      expect(() => expression(emotion)).not.toThrow();
    }
  });
});

describe("cueing the face", () => {
  it("sends the first reading and then only the changes", () => {
    // A partial-per-word stream would otherwise send dozens of identical cues.
    const cue = new ExpressionCue();
    expect(cue.cue("Sorry about that")).toBe("sad");
    expect(cue.cue("Sorry about that delay")).toBeNull();
    expect(cue.lastSent).toBe("sad");
  });

  it("corrects itself as more of the reply arrives", () => {
    // Re-reading each chunk is what lets the face follow the sentence instead
    // of landing on it as it ends.
    const cue = new ExpressionCue();
    expect(cue.cue("Let me check")).toBe("neutral");
    expect(cue.cue("Let me check, that is wonderful")).toBe("happy");
  });

  it("holds the thinking face while a tool runs", () => {
    // A transcript chunk arriving mid-tool would otherwise make the avatar look
    // finished while it is still working.
    const cue = new ExpressionCue();
    expect(cue.thinking(true)).toBe("thinking");
    expect(cue.cue("that is wonderful")).toBeNull();
    expect(cue.lastSent).toBe("thinking");
  });

  it("puts the face back when the tool is done", () => {
    // The model may say nothing at all after a tool result, and with no
    // transcript to re-read the face would stick mid-thought for the rest of
    // the call.
    const cue = new ExpressionCue();
    cue.thinking(true);
    expect(cue.thinking(false)).toBe("neutral");
    expect(cue.cue("that is wonderful")).toBe("happy");
  });

  it("acts on a transition and nothing else", () => {
    const cue = new ExpressionCue();
    expect(cue.thinking(true)).toBe("thinking");
    expect(cue.thinking(true)).toBeNull();
    expect(cue.thinking(false)).toBe("neutral");
    expect(cue.thinking(false)).toBeNull();
  });

  it("sends nothing when a tool that never waited finishes", () => {
    const cue = new ExpressionCue();
    expect(cue.cue("Sorry about that")).toBe("sad");
    expect(cue.thinking(false)).toBeNull();
    expect(cue.lastSent).toBe("sad");
  });
});
