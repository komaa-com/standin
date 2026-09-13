// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The parts of this plugin that are pure, and the seam adapter's refusals.
 *
 * These need no OpenClaw gateway, no provider credentials and no Microsoft
 * tenant, which is the point: a fork can run them.
 */

import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { Transcript, contextSentences } from "../../index.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeInboundRejection, isInboundCallAllowed } from "./allowlist.js";
import { resolvePluginConfig, sessionKey } from "./config.js";
import {
  drainMeetingRecap,
  enqueueMeetingRecap,
  runMeetingRecap,
  transcriptOnly,
} from "./recap.js";
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

describe("session scope", () => {
  it("keys a session by call, thread, or AAD, and never by an empty guest key", () => {
    const start = {
      callId: "call-1",
      threadId: "19:meeting@thread.v2",
      caller: { aadId: "aad-1" },
    };
    expect(sessionKey("per-call", start)).toBe("teams:call-1");
    expect(sessionKey("per-thread", start)).toBe("teams:19:meeting@thread.v2");
    expect(sessionKey("per-aad", start)).toBe("teams:aad-1");
    expect(
      sessionKey("per-aad", { callId: "call-1", caller: { aadId: "" } }),
    ).toBe("teams:call-1");
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

  it("defaults sessionScope to per-call, and a typo does not widen it", () => {
    expect(resolvePluginConfig({}).voice.sessionScope).toBe("per-call");
    expect(resolvePluginConfig({ sessionScope: "per-aad" }).voice.sessionScope).toBe(
      "per-aad",
    );
    expect(resolvePluginConfig({ sessionScope: "everyone" }).voice.sessionScope).toBe(
      "per-call",
    );
  });

  it("keeps meetingRecap off unless it is exactly true", () => {
    expect(resolvePluginConfig({}).voice.meetingRecap).toBe(false);
    expect(resolvePluginConfig({ meetingRecap: true }).voice.meetingRecap).toBe(
      true,
    );
  });
});

describe("meeting recap", () => {
  let dir = "";
  const previous = process.env.STANDIN_RECAP_DIR;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "standin-recap-"));
    process.env.STANDIN_RECAP_DIR = dir;
  });

  afterEach(() => {
    if (previous === undefined) delete process.env.STANDIN_RECAP_DIR;
    else process.env.STANDIN_RECAP_DIR = previous;
    rmSync(dir, { recursive: true, force: true });
  });

  function spoolFiles(): string[] {
    return readdirSync(dir).filter(
      (name) => name.endsWith(".json") || name.endsWith(".claimed"),
    );
  }

  it("does nothing when the operator left it off", async () => {
    const summarise = vi.fn(async () => "minutes");
    await runMeetingRecap({
      enabled: false,
      session: fakeSession().session as never,
      transcript: { empty: false } as never,
      summarise,
    });
    expect(summarise).not.toHaveBeenCalled();
  });

  it("strips the prompt wrapper when a host explicitly wants the raw transcript", () => {
    expect(transcriptOnly("intro\n\nTranscript:\nAlice: hi\n")).toBe("Alice: hi");
  });

  /** A meeting call with two people on it and a thread to post into. */
  function meetingSession() {
    return {
      callId: "call-m",
      participantCount: 2,
      start: {
        callId: "call-m",
        threadId: "19:meeting_abc@thread.v2",
        tenantId: "tenant-1",
        caller: { aadId: "caller-1" },
        direction: "inbound" as const,
      },
      sendAudio: async () => {},
      cancelPlayback: async () => {},
      end: async () => {},
    } as never;
  }

  function spokenTranscript(): Transcript {
    const transcript = new Transcript();
    transcript.add("Dana", "we agreed to ship on Friday");
    transcript.add("Assistant", "noted, Friday it is", "assistant");
    return transcript;
  }

  it("posts the minutes into the meeting thread when recap is on", async () => {
    const send = vi.fn(async () => true);
    const summarise = vi.fn(async () => "- Ship on Friday");
    await runMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise,
      chat: { send },
    });
    expect(summarise).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledTimes(1);
    const posted = send.mock.calls[0]![0] as {
      tenantId: string;
      conversationId: string;
      text: string;
    };
    expect(posted.conversationId).toBe("19:meeting_abc@thread.v2");
    expect(posted.tenantId).toBe("tenant-1");
    expect(posted.text).toContain("Ship on Friday");
  });

  it("posts nothing when the summariser fails, never the raw transcript", async () => {
    const send = vi.fn(async () => true);
    await runMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise: async () => {
        throw new Error("agent down");
      },
      chat: { send },
    });
    expect(send).not.toHaveBeenCalled();

    // An empty answer is the same outcome: nothing goes out.
    await runMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise: async () => "",
      chat: { send },
    });
    expect(send).not.toHaveBeenCalled();
  });

  it("spends no consult when there is no chat lane to post through", async () => {
    const summarise = vi.fn(async () => "minutes");
    await runMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise,
    });
    expect(summarise).not.toHaveBeenCalled();
  });

  it("writes the spool before the consult starts", async () => {
    let release!: (value: string) => void;
    const hang = new Promise<string>((resolve) => {
      release = resolve;
    });
    const send = vi.fn(async () => true);
    const work = enqueueMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise: () => hang,
      chat: { send },
    });
    expect(spoolFiles().length).toBeGreaterThan(0);
    expect(send).not.toHaveBeenCalled();
    release("- Ship on Friday");
    await work;
    expect(send).toHaveBeenCalledTimes(1);
    expect(spoolFiles()).toEqual([]);
  });

  it("posts from the spool after a restart", async () => {
    await enqueueMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise: async () => "- Ship on Friday",
    });
    expect(spoolFiles().length).toBeGreaterThan(0);

    const send = vi.fn(async () => true);
    const posted = await drainMeetingRecap({
      chat: { send },
      summarise: async () => "- Ship on Friday",
    });
    expect(posted).toBe(1);
    expect(send).toHaveBeenCalledTimes(1);
    const postedBody = send.mock.calls[0]![0] as { text: string };
    expect(postedBody.text).toContain("Ship on Friday");
    expect(spoolFiles()).toEqual([]);
  });

  it("keeps the spool when the summariser fails", async () => {
    const send = vi.fn(async () => true);
    await enqueueMeetingRecap({
      enabled: true,
      session: meetingSession(),
      transcript: spokenTranscript(),
      summarise: async () => {
        throw new Error("agent down");
      },
      chat: { send },
    });
    expect(send).not.toHaveBeenCalled();
    expect(spoolFiles().length).toBeGreaterThan(0);
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
