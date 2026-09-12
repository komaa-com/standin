// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Placing a call, saying the thing, and what to do when nobody answers.
 *
 * The three are one capability, and splitting them is how the answer gets lost.
 * A call is placed because somebody is owed something; if they do not pick up,
 * they are still owed it.
 *
 * The Python twin is `tests/test_outbound_lane.py`.
 */

import { mkdtempSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CALL_BACK_TOOL,
  CHAT_CALLBACK_TOOL,
  MAX_PENDING_TEXT_CHARS,
  OutboundError,
  OutboundLane,
  OutboundPolicy,
  PendingMessages,
  callThreadIsPostable,
  type OutboundSession,
  type PendingMessage,
  type PlacedCall,
} from "./outbound.js";

const TARGET = "aad-callee";

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "standin-lane-"));
}

class FakeCaller {
  placed: Array<{ userObjectId: string; tenantId: string }> = [];
  cancelled: string[] = [];

  async placeCall(opts: {
    userObjectId: string;
    tenantId: string;
  }): Promise<PlacedCall> {
    this.placed.push(opts);
    return { callId: `call-${this.placed.length}` };
  }

  async cancelCall(callId: string): Promise<boolean> {
    this.cancelled.push(callId);
    return true;
  }
}

class FakeChat {
  sent: Array<Record<string, unknown>> = [];
  ok = true;

  async send(options: {
    tenantId: string;
    conversationId: string;
    text: string;
    idempotencyKey?: string;
  }): Promise<boolean> {
    this.sent.push(options);
    return this.ok;
  }
}

function session(callId = "call-1", direction = "outbound", recording = false) {
  const ended: string[] = [];
  const s: OutboundSession & { recordingActive: boolean; ended: string[] } = {
    callId,
    start: { direction },
    recordingActive: recording,
    ended,
    async end(reason: string) {
      ended.push(reason);
    },
  };
  return s;
}

function lane(dir: string, over: Record<string, unknown> = {}) {
  const caller = new FakeCaller();
  const chat = new FakeChat();
  const l = new OutboundLane({
    caller: caller as unknown as never,
    policy: new OutboundPolicy({ allowed: [TARGET] }),
    pending: new PendingMessages(dir),
    chat,
    tenantId: "tenant-1",
    ...over,
  });
  return { lane: l, caller, chat };
}

describe("placing a call", () => {
  it("parks what to say", async () => {
    const dir = scratch();
    const { lane: l, caller } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "the report is ready",
      threadId: "19:chat@thread.v2",
    });
    expect(caller.placed).toEqual([
      { userObjectId: TARGET, tenantId: "tenant-1" },
    ]);
    const held = new PendingMessages(dir).waiting();
    expect(held.map((m) => m.text)).toEqual(["the report is ready"]);
    expect(held[0]!.callId).toBe(placed.callId);
    expect(held[0]!.tenantId).toBe("tenant-1");
  });

  it("does not call when there is nothing to say", async () => {
    // Refused before the ring, so nobody's phone goes off for a message that
    // was never going to be sent.
    const { lane: l, caller } = lane(scratch());
    await expect(
      l.place({ userObjectId: TARGET, text: "   " }),
    ).rejects.toThrow(/nothing to say/);
    expect(caller.placed).toEqual([]);
  });

  it("refuses a message too long to say", async () => {
    const { lane: l, caller } = lane(scratch());
    await expect(
      l.place({
        userObjectId: TARGET,
        text: "x".repeat(MAX_PENDING_TEXT_CHARS + 1),
      }),
    ).rejects.toThrow(/too long/);
    expect(caller.placed).toEqual([]);
  });

  it("refuses somebody not on the list before the ring", async () => {
    const { lane: l, caller } = lane(scratch());
    await expect(
      l.place({ userObjectId: "aad-stranger", text: "hello" }),
    ).rejects.toThrow(/allowlist/);
    expect(caller.placed).toEqual([]);
  });

  it("refuses a second call to the same person", async () => {
    const { lane: l, caller } = lane(scratch());
    await l.place({ userObjectId: TARGET, text: "first" });
    await expect(
      l.place({ userObjectId: TARGET, text: "second" }),
    ).rejects.toThrow(/already calling/);
    expect(caller.placed).toHaveLength(1);
  });

  it("parks no fallback for a call with no real conversation", async () => {
    // A one-to-one call has no meeting chat, and posting to what the field
    // carries instead would fail or reach the wrong place.
    const dir = scratch();
    const { lane: l } = lane(dir);
    await l.place({ userObjectId: TARGET, text: "hello", threadId: " " });
    expect(new PendingMessages(dir).waiting()[0]!.threadId).toBe("");
  });

  it.each([
    ["19:meeting@thread.v2", "call-1", true],
    ["call-1", "call-1", false],
    ["", "call-1", false],
    ["8:orgid:something", "call-1", false],
  ])("knows which threads are postable (%s)", (thread, call, postable) => {
    expect(callThreadIsPostable(thread as string, call as string)).toBe(
      postable,
    );
  });
});

describe("answering", () => {
  it("says the parked line when they answer", async () => {
    const dir = scratch();
    const { lane: l } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "the report is ready",
    });

    const said: string[] = [];
    const s = session(placed.callId, "outbound", false);
    const leg = l.attach(
      s,
      async (m: PendingMessage) => void said.push(m.text),
    );
    expect(leg).toBeDefined();
    expect(said).toEqual([]); // still ringing

    s.recordingActive = true;
    await leg!.onContext();
    expect(said).toEqual(["the report is ready"]);
    expect(new PendingMessages(dir).waiting()).toEqual([]);
  });

  it("says it only once", async () => {
    const { lane: l } = lane(scratch());
    const placed = await l.place({ userObjectId: TARGET, text: "hello" });
    const said: string[] = [];
    const leg = l.attach(
      session(placed.callId, "outbound", true),
      async (m) => void said.push(m.text),
    )!;
    await leg.onContext();
    await leg.onContext();
    await leg.answered();
    expect(said).toEqual(["hello"]);
  });

  it("attaches nothing to an inbound call", async () => {
    // So a plugin can call attach unconditionally from onStart.
    const { lane: l } = lane(scratch());
    expect(
      l.attach(session("call-1", "inbound"), async () => {
        throw new Error("nothing to say on an inbound call");
      }),
    ).toBeUndefined();
  });

  it("gives the message back when nobody answers", async () => {
    // Nobody heard it, so it still has to reach them somehow.
    const dir = scratch();
    const { lane: l } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    const leg = l.attach(session(placed.callId), async () => {
      throw new Error("they never answered");
    })!;
    await leg.aclose("caller-hung-up");
    expect(new PendingMessages(dir).waiting().map((m) => m.callId)).toEqual([
      placed.callId,
    ]);
  });

  it("hides a reserved message from the sweep", async () => {
    // The sweep must not tell somebody it could not reach them while their
    // phone is still ringing.
    const dir = scratch();
    const { lane: l, chat } = lane(dir, { answerTimeoutMs: 0 });
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    l.attach(session(placed.callId), async () => undefined);
    expect(await l.sweep()).toBe(0);
    expect(chat.sent).toEqual([]);
  });

  it("does not lose the message when speaking fails", async () => {
    const dir = scratch();
    const { lane: l } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    const leg = l.attach(session(placed.callId, "outbound", true), async () => {
      throw new Error("the provider went away");
    })!;
    await leg.onContext();
    await leg.aclose("handler-failure");
    expect(new PendingMessages(dir).waiting().map((m) => m.callId)).toEqual([
      placed.callId,
    ]);
  });
});

describe("not answering", () => {
  it("puts the answer in chat", async () => {
    const { lane: l, chat } = lane(scratch());
    const placed = await l.place({
      userObjectId: TARGET,
      text: "the report is ready",
      threadId: "19:c@thread.v2",
    });
    expect(await l.onOutcome(placed.callId, "no-answer")).toBe(true);
    expect(chat.sent).toHaveLength(1);
    expect(chat.sent[0]!.text).toContain("couldn't reach you");
    expect(chat.sent[0]!.text).toContain("the report is ready");
  });

  it.each([
    ["declined", "declined my call"],
    ["busy", "line was busy"],
    ["failed", "could not be completed"],
  ])("says what happened for %s", async (outcome, wording) => {
    const { lane: l, chat } = lane(scratch());
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    await l.onOutcome(placed.callId, outcome);
    expect(chat.sent[0]!.text).toContain(wording);
  });

  it("does nothing on an answered outcome", async () => {
    const dir = scratch();
    const { lane: l, chat } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    expect(await l.onOutcome(placed.callId, "answered")).toBe(true);
    expect(chat.sent).toEqual([]);
    expect(new PendingMessages(dir).waiting()).toHaveLength(1);
  });

  it("ignores an outcome this SDK does not know", async () => {
    // Treating an unknown word as a failure would post "I could not reach you"
    // to somebody who answered.
    const dir = scratch();
    const { lane: l, chat } = lane(dir);
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    expect(await l.onOutcome(placed.callId, "transferred")).toBe(true);
    expect(chat.sent).toEqual([]);
    expect(new PendingMessages(dir).waiting()).toHaveLength(1);
  });

  it("tells them once when the timer and the outcome both fire", async () => {
    const { lane: l, chat } = lane(scratch(), { answerTimeoutMs: 0 });
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    await l.sweep();
    await l.onOutcome(placed.callId, "no-answer");
    expect(chat.sent.map((s) => s.idempotencyKey)).toEqual([
      `standin-noanswer-${placed.callId}`,
    ]);
  });

  it("cancels the ringing leg on the timer path only", async () => {
    const { lane: l, caller } = lane(scratch(), { answerTimeoutMs: 0 });
    const first = await l.place({
      userObjectId: TARGET,
      text: "a",
      threadId: "19:c@thread.v2",
    });
    await l.sweep();
    expect(caller.cancelled).toEqual([first.callId]);

    caller.cancelled.length = 0;
    const second = await l.place({
      userObjectId: TARGET,
      text: "b",
      threadId: "19:c@thread.v2",
    });
    await l.onOutcome(second.callId, "no-answer");
    // The call already ended on its own; cancelling it would be noise.
    expect(caller.cancelled).toEqual([]);
  });

  it("retires a call with nowhere to post, loudly", async () => {
    const dir = scratch();
    const { lane: l, chat } = lane(dir, { answerTimeoutMs: 0 });
    await l.place({ userObjectId: TARGET, text: "hello" });
    expect(await l.sweep()).toBe(0);
    expect(chat.sent).toEqual([]);
    expect(new PendingMessages(dir).waiting()).toEqual([]);
  });

  it("tries a failed delivery again", async () => {
    const dir = scratch();
    const { lane: l, chat } = lane(dir, { answerTimeoutMs: 0 });
    chat.ok = false;
    const placed = await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    await l.sweep();
    const held = new PendingMessages(dir).waiting();
    expect(held.map((m) => m.callId)).toEqual([placed.callId]);
    expect(held[0]!.attempts).toBe(1);
    expect(held[0]!.createdMs).toBeGreaterThan(0);
  });

  it("stops a delivery that keeps failing", async () => {
    const dir = scratch();
    const { lane: l, chat } = lane(dir, { answerTimeoutMs: 0 });
    chat.ok = false;
    await l.place({
      userObjectId: TARGET,
      text: "hello",
      threadId: "19:c@thread.v2",
    });
    for (let i = 0; i < 8; i += 1) await l.sweep();
    expect(new PendingMessages(dir).waiting()).toEqual([]);
  });
});

describe("ringing them back", () => {
  const chatMessage = {
    tenantId: "tenant-1",
    conversationId: "19:c@thread.v2",
    senderAadId: TARGET,
    senderName: "Dana",
  };

  it("takes who to ring from the message, not the model", () => {
    // An agent that can be told who to ring can be told to ring anybody.
    const { lane: l } = lane(scratch());
    l.rememberChatSender(chatMessage);
    const target = l.chatCallbackTarget("19:c@thread.v2");
    expect(typeof target).not.toBe("string");
    expect((target as { userObjectId: string }).userObjectId).toBe(TARGET);
  });

  it("returns a sentence for an unknown conversation", () => {
    // The caller is a tool result a model reads out loud.
    const { lane: l } = lane(scratch());
    expect(typeof l.chatCallbackTarget("19:nobody")).toBe("string");
  });

  it("does not record a message with no sender", () => {
    const { lane: l } = lane(scratch());
    l.rememberChatSender({ ...chatMessage, senderAadId: undefined });
    expect(typeof l.chatCallbackTarget("19:c@thread.v2")).toBe("string");
  });

  it("takes no target on either tool", () => {
    // There is no target parameter, and there never will be.
    for (const spec of [CHAT_CALLBACK_TOOL, CALL_BACK_TOOL]) {
      expect(Object.keys(spec.parameters ?? {})).toEqual(["message"]);
      expect(spec.required).toEqual(["message"]);
    }
  });
});

describe("the policy and the store", () => {
  it("matches the allowlist case-insensitively", () => {
    // A case mismatch would read as "not allowed" with nothing to say why.
    const policy = new OutboundPolicy({ allowed: ["AAD-Person"] });
    expect(() => policy.check("aad-person")).not.toThrow();
    expect(() => policy.check("AAD-PERSON")).not.toThrow();
  });

  it("treats an empty allowlist as outbound off", () => {
    expect(() => new OutboundPolicy({ allowed: [] }).check(TARGET)).toThrow(
      /outbound calling is off/,
    );
  });

  it("gives back a reservation from a dead worker", () => {
    // Without this a process that dies mid-ring leaves the message reserved for
    // ever, and the person promised an answer never gets one.
    const dir = scratch();
    const store = new PendingMessages(dir);
    store.park({ callId: "c1", text: "hello", threadId: "19:t" });
    store.reserve("c1");
    const old = Date.now() / 1000 - 3600;
    utimesSync(join(dir, "c1.answering"), old, old);

    expect(store.recoverReservations(600_000)).toBe(1);
    expect(store.waiting().map((m) => m.callId)).toEqual(["c1"]);
  });

  it("leaves a fresh reservation alone", () => {
    const store = new PendingMessages(scratch());
    store.park({ callId: "c1", text: "hello" });
    store.reserve("c1");
    expect(store.recoverReservations(600_000)).toBe(0);
  });
});
