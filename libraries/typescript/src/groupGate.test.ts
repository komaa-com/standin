// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The group-call gate.
 *
 * An agent in a meeting that answers every sentence is worse than one that says
 * nothing. The gate keeps it quiet until somebody names it, then leaves the
 * floor open briefly so a follow-up does not need the name again.
 *
 * The Python twin is `tests/test_plugin_hermes_agent.py`.
 */

import { describe, expect, it } from "vitest";

import {
  DEFAULT_FOLLOW_UP_WINDOW_MS,
  GroupGate,
  isAddressed,
  isDismissal,
  isMeetingThread,
  isVerbalInterrupt,
} from "./gate.js";

const MEETING = "19:meeting_abc@thread.v2";

function gate(over: Record<string, unknown> = {}): GroupGate {
  return new GroupGate({
    wakePhrases: ["assistant"],
    threadId: MEETING,
    ...over,
  });
}

describe("who is on the call", () => {
  it("reads a meeting thread as a group", () => {
    // The participant count does not reach a bot that joined through the
    // meeting, so a gate keyed on it alone never fires.
    expect(isMeetingThread(MEETING)).toBe(true);
    expect(isMeetingThread("")).toBe(false);
    expect(isMeetingThread(undefined)).toBe(false);
    expect(isMeetingThread("8:orgid:someone")).toBe(false);
  });

  it("lets a count corroborate but never clear a meeting thread", () => {
    // The count is the signal that goes missing, so it may add certainty and
    // must not remove it.
    const g = gate();
    g.noteParticipants(1);
    expect(g.isGroup).toBe(true);
  });

  it("lets a count alone make it a group", () => {
    const g = gate({ threadId: "" });
    expect(g.isGroup).toBe(false);
    g.noteParticipants(3);
    expect(g.isGroup).toBe(true);
  });
});

describe("being addressed", () => {
  it("matches on a word boundary, not a substring", () => {
    expect(isAddressed("assistant, what is the plan?", ["assistant"])).toBe(
      true,
    );
    expect(isAddressed("the assistants are ready", ["assistant"])).toBe(false);
  });

  it("is case insensitive", () => {
    expect(isAddressed("ASSISTANT, hello", ["assistant"])).toBe(true);
  });

  it("never matches with no phrase configured", () => {
    expect(isAddressed("assistant", [])).toBe(false);
    expect(isAddressed("assistant", ["  "])).toBe(false);
  });

  it("handles a non-Latin phrase the same way", () => {
    expect(isAddressed("مرحبا مساعد كيف حالك", ["مساعد"])).toBe(true);
  });
});

describe("deciding a turn", () => {
  it("stays out of a meeting it was not addressed in", () => {
    expect(gate().decide("what do you all think?", 1000).respond).toBe(false);
  });

  it("answers when named, and opens the floor", () => {
    const g = gate();
    expect(g.decide("assistant, summarise that", 1000)).toEqual({
      respond: true,
      addressed: true,
    });
    // A follow-up does not need the name again.
    expect(g.decide("and the second point?", 1000 + 5_000)).toEqual({
      respond: true,
      addressed: false,
    });
  });

  it("closes the floor again by the clock", () => {
    // The window is a timestamp, never a latched boolean, so a missed phrase
    // self-heals instead of stranding the agent silent for the meeting.
    const g = gate();
    g.decide("assistant, hello", 1000);
    expect(
      g.decide("unrelated", 1000 + DEFAULT_FOLLOW_UP_WINDOW_MS + 1).respond,
    ).toBe(false);
  });

  it("gives the floor back when it is told to stop", () => {
    // A request for silence used to cut one sentence and leave the follow-up
    // window open, so the agent answered the next thing anybody said.
    const g = gate();
    expect(g.decide("assistant, summarise that", 1000).respond).toBe(true);
    g.closeWindow();
    expect(g.decide("right, where were we", 2000).respond).toBe(false);
    // Naming it again is all it takes to bring it back.
    expect(g.decide("assistant, carry on", 3000).respond).toBe(true);
  });

  it("changes nothing on a one-to-one call when the window is closed", () => {
    const g = gate({ threadId: "" });
    g.closeWindow();
    expect(g.decide("hello", 1000).respond).toBe(true);
  });

  it("answers everything on a one-to-one call", () => {
    expect(gate({ threadId: "" }).decide("hello", 1000).respond).toBe(true);
  });

  it("answers everything when the gate is turned off", () => {
    expect(gate({ requireAddress: false }).decide("hello", 1000).respond).toBe(
      true,
    );
  });

  it("is off when nothing could ever open it", () => {
    // A gate with no wake phrase can never be opened, so it would mute the
    // assistant for the whole call.
    const g = gate({ wakePhrases: [] });
    expect(g.active).toBe(false);
    expect(g.decide("hello", 1000).respond).toBe(true);
  });
});

describe("being asked to stop", () => {
  it("peels a wake phrase that starts with a filler word", () => {
    // "hey" is filler. Peeled first, it left "assistant", which no longer
    // matched the wake phrase it was part of, so "hey assistant, be quiet" was
    // read as an address and answered.
    for (const wake of ["hey assistant", "ok assistant"]) {
      const said = `${wake}, be quiet`;
      expect(isVerbalInterrupt(said, [wake])).toBe(true);
      expect(isDismissal(said, [wake])).toBe(true);
    }
    expect(
      isVerbalInterrupt("hey assistant, what is the plan", ["hey assistant"]),
    ).toBe(false);
  });

  it("tells a request for silence apart from a pause", () => {
    // "wait" comes from somebody who is about to ask something and still wants
    // the answer. "be quiet" does not.
    for (const said of ["be quiet", "assistant, shut up", "enough", "اسكت"]) {
      expect(isDismissal(said, ["assistant"])).toBe(true);
      expect(isVerbalInterrupt(said, ["assistant"])).toBe(true);
    }
    for (const said of ["wait", "hold on", "stop", "never mind"]) {
      expect(isDismissal(said, ["assistant"])).toBe(false);
      expect(isVerbalInterrupt(said, ["assistant"])).toBe(true);
    }
    expect(isDismissal("be quiet about the budget", ["assistant"])).toBe(false);
  });
});
