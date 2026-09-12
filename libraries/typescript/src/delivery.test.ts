// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Choosing between speaking into a live call and ringing somebody back.
 *
 * The twin is `libraries/python/tests/test_delivery.py`.
 */

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  LiveCalls,
  TENANT_ENV,
  VoiceDelivery,
  type LiveSpeaker,
} from "./delivery.js";
import {
  OutboundPolicy,
  PendingMessages,
  type OutboundCaller,
  type PlacedCall,
} from "./outbound.js";

const dirs: string[] = [];

afterEach(() => {
  while (dirs.length > 0) rmSync(dirs.pop()!, { recursive: true, force: true });
});

function store(): PendingMessages {
  const dir = mkdtempSync(join(tmpdir(), "standin-delivery-"));
  dirs.push(dir);
  return new PendingMessages(dir);
}

class FakeSpeaker implements LiveSpeaker {
  said: string[] = [];
  constructor(readonly fail?: Error) {}
  async say(text: string): Promise<void> {
    if (this.fail) throw this.fail;
    this.said.push(text);
  }
}

class FakeCaller {
  placed: Array<[string, string]> = [];
  fail: Error | undefined;
  constructor(readonly callId = "placed-1") {}
  async placeCall(opts: {
    userObjectId: string;
    tenantId: string;
  }): Promise<PlacedCall> {
    if (this.fail) throw this.fail;
    this.placed.push([opts.userObjectId, opts.tenantId]);
    return { callId: this.callId, scenarioId: "s" } as PlacedCall;
  }
}

function delivery(
  live: LiveCalls,
  overrides: {
    /** null means no caller is configured at all. */
    caller?: FakeCaller | null;
    policy?: OutboundPolicy;
    pending?: PendingMessages;
    tenantId?: string;
  } = {},
): VoiceDelivery {
  return new VoiceDelivery(live, {
    caller: (overrides.caller === null
      ? undefined
      : (overrides.caller ?? new FakeCaller())) as unknown as
      OutboundCaller | undefined,
    policy: overrides.policy ?? new OutboundPolicy({ allowed: ["dana"] }),
    pending: overrides.pending ?? store(),
    tenantId: "tenantId" in overrides ? overrides.tenantId : "tenant-1",
  });
}

describe("reaching somebody by voice", () => {
  it("speaks into a live call rather than ringing it again", async () => {
    // Ringing somebody who is mid sentence with you is the rudest possible way
    // to tell them something.
    const live = new LiveCalls();
    const speaker = new FakeSpeaker();
    live.register(speaker, "call-1", "19:thread");
    const caller = new FakeCaller();

    const result = await delivery(live, { caller }).deliver(
      "Your build finished.",
      "dana",
      "19:thread",
    );
    expect(result).toEqual({ ok: true, mode: "live-call" });
    expect(speaker.said).toEqual(["Your build finished."]);
    expect(caller.placed).toEqual([]);
  });

  it("finds a call filed only by its own id", async () => {
    // A delivery addressed by conversation would otherwise ring a second call
    // to somebody already on the line.
    const live = new LiveCalls();
    live.register(new FakeSpeaker(), "dana");
    expect((await delivery(live).deliver("hello", "dana")).mode).toBe(
      "live-call",
    );
  });

  it("keeps the second call's registration on one thread", async () => {
    // Call B for the same thread can start before call A's teardown runs. A
    // blind delete would wipe B and every later delivery would ring afresh.
    const live = new LiveCalls();
    const first = new FakeSpeaker();
    const second = new FakeSpeaker();
    live.register(first, "call-a", "19:thread");
    live.register(second, "call-b", "19:thread");
    live.unregister(first, "call-a", "19:thread");

    expect(live.find("19:thread")).toBe(second);
    await delivery(live).deliver("hello", "", "19:thread");
    expect(second.said).toEqual(["hello"]);
  });

  it("rings back when the live call is wedged rather than swallowing the line", async () => {
    const live = new LiveCalls();
    live.register(new FakeSpeaker(new Error("socket is wedged")), "dana");
    const caller = new FakeCaller();

    const result = await delivery(live, { caller }).deliver(
      "Your build finished.",
      "dana",
    );
    // A half-spoken line can be repeated by the call-back. A swallowed one
    // cannot be recovered by anything.
    expect(result.ok).toBe(true);
    expect(result.mode).toBe("call-back");
    expect(caller.placed).toEqual([["dana", "tenant-1"]]);
  });

  it("never rings anybody when there is nothing to say", async () => {
    const caller = new FakeCaller();
    const result = await delivery(new LiveCalls(), { caller }).deliver(
      "   ",
      "dana",
    );
    expect(result).toEqual({ ok: false, error: "there was nothing to say" });
    expect(caller.placed).toEqual([]);
  });

  it("returns a sentence rather than throwing on every refusal", async () => {
    // The result is read by a host that marks the platform failed on an
    // exception, or by a model that says it out loud.
    const off = await delivery(new LiveCalls(), {
      policy: new OutboundPolicy({ allowed: [] }),
    }).deliver("hello", "dana");
    expect(off.ok).toBe(false);
    expect(off.error).toContain("outbound calling is off");

    const none = await delivery(new LiveCalls(), { caller: null }).deliver(
      "hello",
      "dana",
    );
    expect(none.error).toContain("no outbound caller");

    const noTenant = await delivery(new LiveCalls(), { tenantId: "" }).deliver(
      "hello",
      "dana",
    );
    expect(noTenant.error).toContain("no tenant is configured");
  });

  it("counts calls that rang, not attempts", async () => {
    // A broken worker would otherwise burn the hour on calls that never rang
    // anybody, and the next real delivery is refused because of it.
    const caller = new FakeCaller();
    caller.fail = new Error("the worker is down");
    const sender = delivery(new LiveCalls(), {
      caller,
      policy: new OutboundPolicy({ allowed: ["dana"], maxPerHour: 1 }),
    });

    expect((await sender.deliver("hello", "dana")).ok).toBe(false);
    caller.fail = undefined;
    expect((await sender.deliver("hello", "dana")).ok).toBe(true);
    expect((await sender.deliver("hello", "dana")).error).toContain(
      "already placed",
    );
  });

  it("parks the line before the delivery returns", async () => {
    // The answering leg is a different call and can be answered before a later
    // park would have run, and then the person picks up to silence.
    const pending = store();
    const result = await delivery(new LiveCalls(), { pending }).deliver(
      "Your build finished.",
      "dana",
      "19:thread",
    );

    const parked = pending.pop(result.callId!);
    expect(parked?.text).toBe("Your build finished.");
    // Carried so an unanswered call can post the answer to the chat it came
    // from instead of losing it.
    expect(parked?.threadId).toBe("19:thread");
    expect(parked?.tenantId).toBe("tenant-1");
    expect(parked?.target).toBe("dana");
  });

  it("takes the tenant from the operator, never from the message", async () => {
    // A model steered by whoever is talking must not choose which organisation
    // gets dialled.
    const caller = new FakeCaller();
    process.env[TENANT_ENV] = "from-the-operator";
    try {
      const sender = new VoiceDelivery(new LiveCalls(), {
        caller: caller as unknown as OutboundCaller,
        policy: new OutboundPolicy({ allowed: ["dana"] }),
        pending: store(),
      });
      expect((await sender.deliver("hello", "dana")).ok).toBe(true);
      expect(caller.placed).toEqual([["dana", "from-the-operator"]]);
    } finally {
      delete process.env[TENANT_ENV];
    }
  });

  it("admits a park that failed rather than reporting a delivery", async () => {
    // Parking writes to disk. A full or read-only one must not throw out of a
    // method whose whole contract is that it does not, and must not be reported
    // as a delivery either: the call rang, and nobody will hear the line.
    const pending = store();
    pending.park = () => {
      throw new Error("read-only file system");
    };
    const result = await delivery(new LiveCalls(), { pending }).deliver(
      "Your build finished.",
      "dana",
    );
    expect(result).toEqual({
      ok: false,
      mode: "call-back",
      callId: "placed-1",
      error: "the call was placed, but what to say could not be saved",
    });
  });

  it("does not report a placement with no call id as delivered", async () => {
    const result = await delivery(new LiveCalls(), {
      caller: new FakeCaller(""),
    }).deliver("hello", "dana");
    expect(result).toEqual({ ok: false, error: "the call was not placed" });
  });
});
