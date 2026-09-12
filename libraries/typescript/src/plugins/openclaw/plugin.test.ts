// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The parts of this plugin that are pure, and the seam adapter's refusals.
 *
 * These need no OpenClaw gateway, no provider credentials and no Microsoft
 * tenant, which is the point: a fork can run them.
 */

import { contextSentences } from "../../index.js";
import { describe, expect, it, vi } from "vitest";

import { describeInboundRejection, isInboundCallAllowed } from "./allowlist.js";
import { resolvePluginConfig } from "./config.js";
// The echo guard and the verbal-interrupt check are the SDK's, not this
// plugin's, so the test reaches for the one definition like every other caller.
import { pcm16Rms, shouldSuppressEcho } from "../../echoGuard.js";
import { isVerbalInterrupt } from "../../gate.js";

/** A buffer of constant-amplitude PCM16, for a predictable RMS. */
function tone(amplitude: number, samples = 320): Buffer {
  const buf = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i++) buf.writeInt16LE(amplitude, i * 2);
  return buf;
}

describe("allowlist", () => {
  it("refuses when the policy is unset, so a config typo cannot open the line", () => {
    expect(isInboundCallAllowed(undefined, ["abc"], "abc")).toBe(false);
  });

  it("matches an AAD object id case-insensitively", () => {
    expect(isInboundCallAllowed("allowlist", ["AB-12"], "ab-12")).toBe(true);
  });

  it("matches a phone number on digits only", () => {
    expect(
      isInboundCallAllowed("allowlist", ["+971 4 555 0000"], "97145550000"),
    ).toBe(true);
  });

  it("open accepts an anonymous caller", () => {
    expect(isInboundCallAllowed("open", undefined, "")).toBe(true);
  });

  it("names the fix in the rejection line", () => {
    expect(describeInboundRejection("allowlist", "abc")).toContain("allowFrom");
  });
});

describe("echo guard", () => {
  const future = Date.now() + 5_000;

  it("drops quiet caller audio while our own audio is still playing", () => {
    expect(shouldSuppressEcho(tone(200), future)).toBe(true);
  });

  it("lets a loud interruption through as a barge-in", () => {
    expect(shouldSuppressEcho(tone(20_000), future)).toBe(false);
  });

  it("suppresses everything before the caller's first turn, however loud", () => {
    expect(
      shouldSuppressEcho(tone(20_000), future, { allowBargeIn: false }),
    ).toBe(true);
  });

  it("does nothing once playout has finished", () => {
    expect(shouldSuppressEcho(tone(200), Date.now() - 5_000)).toBe(false);
  });

  it("can be turned off outright", () => {
    expect(
      shouldSuppressEcho(tone(200), future, {
        suppressInputDuringPlayback: false,
      }),
    ).toBe(false);
  });

  it("reports zero RMS for an empty buffer rather than NaN", () => {
    expect(pcm16Rms(Buffer.alloc(0))).toBe(0);
  });
});

describe("verbal interrupt", () => {
  it("cuts on a bare stop", () => {
    expect(isVerbalInterrupt("stop")).toBe(true);
  });

  it("cuts through surrounding filler", () => {
    expect(isVerbalInterrupt("ok, stop please")).toBe(true);
  });

  it("cuts in Arabic", () => {
    expect(isVerbalInterrupt("توقف")).toBe(true);
  });

  it("does not cut on a sentence that merely contains the word", () => {
    expect(isVerbalInterrupt("stop by the store on your way home")).toBe(false);
  });

  it("treats the wake phrase alone as an address, not an interrupt", () => {
    expect(isVerbalInterrupt("hey assistant", ["hey assistant"])).toBe(false);
  });
});

describe("config", () => {
  it("fails the secret CLOSED when an unresolved reference arrives as an object", () => {
    // String({}) would be "[object Object]": non-empty, guessable, and accepted.
    expect(
      resolvePluginConfig({ secret: { $env: "MISSING" } }).media.secret,
    ).toBe("");
  });

  it("leaves bindAddress undefined so the SDK default stands", () => {
    expect(resolvePluginConfig({}).media.bindAddress).toBeUndefined();
  });

  it("defaults the listener to the StandIn layout", () => {
    const media = resolvePluginConfig({}).media;
    expect([media.port, media.path]).toEqual([9442, "/msteams/calling"]);
  });

  it("keeps requireRecordingStatus off unless it is exactly true", () => {
    expect(
      resolvePluginConfig({ requireRecordingStatus: "yes" }).voice
        .requireRecordingStatus,
    ).toBe(false);
  });
});

// The handler builds a realtime call through this module, so stub the module
// rather than the host: these tests are about refusal and ordering, not audio.
const fakeCall = {
  connect: vi.fn(async () => {}),
  pushAudio: vi.fn(),
  pushContext: vi.fn(),
  setRecordingActive: vi.fn(),
  interrupt: vi.fn(),
  say: vi.fn(),
  close: vi.fn(),
};
vi.mock("./realtime.js", () => ({ createRealtimeCall: () => fakeCall }));

const { TeamsCallHandler } = await import("./handler.js");

/** Just enough CallSession to drive onStart. Records what it was ended with. */
function fakeSession(aadId = "caller-1") {
  const ended: string[] = [];
  return {
    ended,
    session: {
      callId: "call-1",
      start: {
        callId: "call-1",
        threadId: "",
        caller: { aadId },
        direction: "inbound" as const,
      },
      sendAudio: async () => {},
      cancelPlayback: async () => {},
      end: async (reason: string) => {
        ended.push(reason);
      },
    },
  };
}

const openConfig = resolvePluginConfig({ secret: "s", inboundPolicy: "open" });
const realtime = { provider: {}, providerConfig: {} } as never;
const registry = { acquire: () => true, bind: () => {}, release: () => {} };

describe("TeamsCallHandler refusals", () => {
  it("refuses when no realtime provider resolved", async () => {
    const { session, ended } = fakeSession();
    const handler = new TeamsCallHandler({ config: openConfig, registry });
    await handler.onStart(session as never);
    expect(ended).toEqual(["realtime-unavailable"]);
  });

  it("refuses a caller the inbound policy does not allow", async () => {
    const { session, ended } = fakeSession();
    const config = resolvePluginConfig({
      secret: "s",
      inboundPolicy: "allowlist",
      allowFrom: [],
    });
    const handler = new TeamsCallHandler({ config, realtime, registry });
    await handler.onStart(session as never);
    expect(ended).toEqual(["not-allowed"]);
  });

  it("refuses when the operator's concurrency cap is reached", async () => {
    const { session, ended } = fakeSession();
    const handler = new TeamsCallHandler({
      config: openConfig,
      realtime,
      registry: { ...registry, acquire: () => false },
    });
    await handler.onStart(session as never);
    expect(ended).toEqual(["busy"]);
  });

  it("checks the policy before taking a slot, so a refused caller cannot fill the worker", async () => {
    const acquire = vi.fn(() => true);
    const config = resolvePluginConfig({
      secret: "s",
      inboundPolicy: "allowlist",
      allowFrom: [],
    });
    const handler = new TeamsCallHandler({
      config,
      realtime,
      registry: { ...registry, acquire },
    });
    await handler.onStart(fakeSession().session as never);
    expect(acquire).not.toHaveBeenCalled();
  });

  it("refuses and frees the slot when the model never comes up", async () => {
    const release = vi.fn();
    const { session, ended } = fakeSession();
    fakeCall.connect.mockRejectedValueOnce(
      new Error("no credentials") as never,
    );
    const handler = new TeamsCallHandler({
      config: openConfig,
      realtime,
      registry: { ...registry, release },
    });
    await handler.onStart(session as never);
    expect(ended).toEqual(["realtime-unavailable"]);
    await handler.aclose("realtime-unavailable");
    expect(release).toHaveBeenCalledWith("call-1");
  });
});

describe("TeamsCallHandler call surface", () => {
  async function live() {
    const handler = new TeamsCallHandler({
      config: openConfig,
      realtime,
      registry,
    });
    await handler.onStart(fakeSession().session as never);
    return handler;
  }

  it("maps recording context onto the media gate instead of the model", async () => {
    const handler = await live();
    fakeCall.pushContext.mockClear();
    fakeCall.setRecordingActive.mockClear();
    await handler.onContext(contextSentences.recording("active"));
    expect(fakeCall.setRecordingActive).toHaveBeenCalledWith(true);
    expect(fakeCall.pushContext).not.toHaveBeenCalled();
  });

  it("forwards every other context sentence to the model", async () => {
    const handler = await live();
    fakeCall.pushContext.mockClear();
    await handler.onContext(contextSentences.dtmf("5"));
    expect(fakeCall.pushContext).toHaveBeenCalledWith(
      contextSentences.dtmf("5"),
    );
  });

  it("interrupts BEFORE it speaks the goodbye", async () => {
    const handler = await live();
    const order: string[] = [];
    fakeCall.interrupt.mockImplementation(() => order.push("interrupt"));
    fakeCall.say.mockImplementation(() => order.push("say"));
    await handler.onGoodbye("Thanks, goodbye.");
    expect(order).toEqual(["interrupt", "say"]);
  });
});
