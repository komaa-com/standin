// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The TypeScript half of the shared conformance suite.
 *
 * Reads `protocol/conformance.json` - the same file the Python SDK's
 * `test_conformance.py` reads - and asserts the same expectations. That is what
 * makes "the two SDKs are at parity" a property CI proves rather than a claim a
 * README makes.
 *
 * When you find a parity bug, add the case to the vectors first: one language
 * will fail, and that tells you which one to fix.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { FrameAligner, pcm16Rms, resamplePcm16 } from "./audio.js";
import { expression, speechMarks } from "./avatar.js";
import { buildReply, isPersonal, parseInbound } from "./chat.js";
import { signHandshake, verifyHandshake } from "./hmac.js";
import {
  SAMPLE_RATE_HZ,
  assistantCancel,
  audioFrame,
  contextSentences,
  decodePcm,
  parseMessage,
  parseSessionStart,
  pong,
  sessionEnd,
} from "./protocol.js";
import {
  MAX_IMAGE_BYTES,
  displayFrame,
  displayImage,
  parseVideoFrame,
} from "./vision.js";

const here = dirname(fileURLToPath(import.meta.url));
const vectorsPath = join(
  here,
  "..",
  "..",
  "..",
  "protocol",
  "conformance.json",
);
const V = JSON.parse(readFileSync(vectorsPath, "utf8")) as Record<string, any>;

describe("hmac", () => {
  it.each(V.hmac.sign)("signs: $name", (c: any) => {
    expect(signHandshake(c.secret, c.timestampMs, c.id)).toBe(c.expected);
  });

  it.each(V.hmac.verifyRejects)("rejects: $name", (c: any) => {
    // currentMs pinned to the vector's timestamp so freshness never decides
    // these cases - each one is about the input being malformed, not stale.
    const parsed = Number(c.timestamp);
    const current =
      Number.isFinite(parsed) && c.timestamp ? parsed : 1735689600000;
    expect(
      verifyHandshake(
        c.secret,
        c.timestamp ?? undefined,
        c.id,
        c.signature ?? undefined,
        current,
      ),
    ).toBe(false);
  });

  it("round-trips", () => {
    const c = V.hmac.sign[0];
    const sig = signHandshake(c.secret, c.timestampMs, c.id);
    expect(
      verifyHandshake(
        c.secret,
        c.timestampMs,
        c.id,
        sig,
        Number(c.timestampMs),
      ),
    ).toBe(true);
  });
});

describe("sessionStart", () => {
  it.each(V.sessionStart.accepts)("parses: $name", (c: any) => {
    const got = parseSessionStart(c.input);
    const want = c.expected;
    expect(got.callId).toBe(want.callId);
    expect(got.threadId).toBe(want.threadId);
    expect(got.direction).toBe(want.direction);
    expect(got.recordingStatus ?? null).toBe(want.recordingStatus);
    expect(got.tenantId ?? null).toBe(want.tenantId);
    expect(got.caller.aadId ?? null).toBe(want.caller.aadId);
    expect(got.caller.displayName ?? null).toBe(want.caller.displayName);
    expect(got.caller.tenantId ?? null).toBe(want.caller.tenantId);
  });

  it.each(V.sessionStart.rejects)("rejects: $name", (c: any) => {
    expect(() => parseSessionStart(c.input)).toThrow();
  });
});

describe("parseMessage", () => {
  it.each(V.parseMessage.dropped)("drops: $name", (c: any) => {
    expect(parseMessage(c.input)).toBeUndefined();
  });

  it.each(V.parseMessage.accepted)("accepts: $name", (c: any) => {
    expect(parseMessage(c.input)?.type).toBe(c.expectedType);
  });

  it.each(V.parseMessage.bytesDropped)("drops encoding: $name", (c: any) => {
    expect(parseMessage(Buffer.from(c.inputBase64, "base64"))).toBeUndefined();
  });
});

describe("decodePcm", () => {
  it.each(V.decodePcm.accepts)("accepts: $name", (c: any) => {
    expect(decodePcm(c.payloadBase64).length).toBe(c.expectedBytes);
  });

  it.each(V.decodePcm.rejects)("rejects: $name", (c: any) => {
    expect(() => decodePcm(c.payloadBase64)).toThrow();
  });
});

describe("audioTimeline", () => {
  it("is integer division at every step", () => {
    expect(V.audioTimeline.sampleRateHz).toBe(SAMPLE_RATE_HZ);
    let seq = 0;
    let sentMs = 0;
    for (const step of V.audioTimeline.steps) {
      const pcm = Buffer.alloc(step.pcmBytes);
      seq += 1;
      const frame = JSON.parse(audioFrame(seq, sentMs, pcm));
      expect(frame.seq, JSON.stringify(step)).toBe(step.expectedSeq);
      expect(frame.timestampMs, JSON.stringify(step)).toBe(
        step.expectedTimestampMs,
      );
      sentMs += Math.floor(
        (Math.floor(pcm.length / 2) * 1000) / SAMPLE_RATE_HZ,
      );
    }
  });
});

describe("contextSentences", () => {
  it.each(V.contextSentences.participants)("participants($count)", (c: any) => {
    expect(contextSentences.participants(c.count)).toBe(c.expected);
  });

  it.each(V.contextSentences.dtmf)("dtmf($digit)", (c: any) => {
    expect(contextSentences.dtmf(c.digit)).toBe(c.expected);
  });

  it.each(V.contextSentences.recording)("recording($status)", (c: any) => {
    expect(contextSentences.recording(c.status)).toBe(c.expected);
  });
});

describe("outboundFrames", () => {
  it.each(V.outboundFrames.pong)("pong", (c: any) => {
    expect(JSON.parse(pong(c.input))).toEqual(c.expected);
  });

  it.each([Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    "normalizes nonfinite pong timestamp %s",
    (timestamp) => {
      expect(JSON.parse(pong(timestamp))).toEqual({ type: "pong", ts: 0 });
    },
  );

  it.each(V.outboundFrames.sessionEnd)("sessionEnd", (c: any) => {
    expect(JSON.parse(sessionEnd(c.input))).toEqual(c.expected);
  });

  it.each(V.outboundFrames.assistantCancel)("assistantCancel", (c: any) => {
    expect(JSON.parse(assistantCancel(c.input))).toEqual(c.expected);
  });
});

describe("audio", () => {
  it.each(V.audio.resample)("resample: $name", (c: any) => {
    const got = resamplePcm16(
      Buffer.from(c.inputBase64, "base64"),
      c.srcHz,
      c.dstHz,
    );
    expect(got.length).toBe(c.expectedBytes);
    expect(got.toString("base64")).toBe(c.expectedBase64);
  });

  it.each(V.audio.frameAligner)("frameAligner: $name", (c: any) => {
    const aligner = new FrameAligner(c.frameBytes);
    for (const push of c.pushes) {
      const frames = aligner.push(Buffer.alloc(push.inputBytes));
      expect(frames.length, JSON.stringify(push)).toBe(push.framesOut);
      for (const f of frames) expect(f.length).toBe(c.frameBytes);
      expect(aligner.pending, JSON.stringify(push)).toBe(push.pendingAfter);
    }
    const tail = aligner.flush();
    if (c.flushBytes === null) {
      expect(tail).toBeUndefined();
    } else {
      expect(tail?.length).toBe(c.flushBytes);
    }
    expect(aligner.flush()).toBeUndefined();
  });
});

describe("chat", () => {
  it.each(V.chat.parseInbound.accepts)("parses: $name", (c: any) => {
    const m = parseInbound(c.body);
    const w = c.expected;
    expect(m.tenantId).toBe(w.tenantId);
    expect(m.conversationId).toBe(w.conversationId);
    expect(m.activityId).toBe(w.activityId);
    expect(m.scope).toBe(w.scope);
    expect(m.text).toBe(w.text);
    expect(m.senderName ?? null).toBe(w.senderName);
    expect(m.senderAadId ?? null).toBe(w.senderAadId);
    expect(m.senderIsGuest).toBe(w.senderIsGuest);
    expect(m.senderIsLinkedOwner).toBe(w.senderIsLinkedOwner);
    expect(m.attachments).toEqual(w.attachments);
    expect(m.mentions).toEqual(w.mentions);
    expect(m.locale ?? null).toBe(w.locale);
    expect(m.cardAction ?? null).toEqual(w.cardAction);
    expect(isPersonal(m)).toBe(w.isPersonal);
  });

  it.each(V.chat.parseInbound.rejects)("rejects: $name", (c: any) => {
    expect(() => parseInbound(c.body)).toThrow();
  });

  it.each(V.chat.buildReply)("buildReply: $name", (c: any) => {
    // tenantId and conversationId echo the inbound EXACTLY: that check is the
    // cross-tenant leak guard the whole relay rests on, so a divergence between
    // the two SDKs here is a security bug.
    const inbound = parseInbound(V.chat.parseInbound.accepts[0].body);
    expect(buildReply(inbound, "the answer", c.kind)).toEqual(c.expected);
  });
});

describe("vision", () => {
  it.each(V.video.parseAccepts)("parses: $name", (c: any) => {
    const frame = parseVideoFrame(c.input);
    expect(frame).toBeDefined();
    expect(frame!.source).toBe(c.expected.source);
    expect(frame!.ts).toBe(c.expected.ts);
    expect(frame!.width).toBe(c.expected.width);
    expect(frame!.height).toBe(c.expected.height);
    expect(frame!.mime).toBe(c.expected.mime);
    expect(frame!.dataBase64).toBe(c.expected.dataBase64);
    expect(frame!.participantId ?? null).toBe(c.expected.participantId);
    expect(frame!.participantName ?? null).toBe(c.expected.participantName);
    // The two forms a provider asks for: raw bytes to upload, a data URL to
    // paste into a vision request.
    expect(frame!.data).toEqual(Buffer.from(c.expected.dataBase64, "base64"));
    expect(frame!.dataUrl).toBe(
      `data:${c.expected.mime};base64,${c.expected.dataBase64}`,
    );
  });

  // Dropped, never thrown: one malformed image must not end a live call.
  it.each(V.video.parseRejects)("drops: $name", (c: any) => {
    expect(parseVideoFrame(c.input)).toBeUndefined();
  });

  it.each(V.video.displayImage)("displayImage: $name", (c: any) => {
    const built = displayImage(c.dataBase64, {
      mime: c.mime,
      durationMs: c.durationMs,
      mode: c.mode,
      caption: c.caption,
    });
    expect(JSON.parse(built)).toEqual(c.expected);
  });

  it.each(V.video.displayFrame)("displayFrame: $name", (c: any) => {
    const built = displayFrame(c.seq, c.ts, c.dataBase64, {
      mime: c.mime,
      width: c.width,
      height: c.height,
    });
    expect(JSON.parse(built)).toEqual(c.expected);
  });

  it("takes a Buffer and base64 as two spellings of one image", () => {
    const raw = Buffer.from([0xff, 0xd8, 0xff, 0xe0]);
    expect(displayImage(raw)).toBe(displayImage(raw.toString("base64")));
  });

  // Refuse here, where the error names the problem, rather than letting the
  // service close the socket in the middle of a call.
  it.each([
    { name: "empty image", image: Buffer.alloc(0), mime: "image/jpeg" },
    {
      name: "unsupported mime",
      image: Buffer.from([0xff, 0xd8]),
      mime: "image/gif",
    },
    { name: "not base64", image: "not base64!", mime: "image/jpeg" },
    {
      name: "oversized",
      image: Buffer.alloc(MAX_IMAGE_BYTES + 1),
      mime: "image/jpeg",
    },
  ])("refuses what the service would reject: $name", (c: any) => {
    expect(() => displayImage(c.image, { mime: c.mime })).toThrow();
  });
});

describe("avatar", () => {
  it.each(V.avatar.expression)("expression: $name", (c: any) => {
    expect(JSON.parse(expression(c.input))).toEqual(c.expected);
  });

  it.each(V.avatar.expressionRejects)(
    "expression needs an emotion: $name",
    (c: any) => {
      expect(() => expression(c.input)).toThrow();
    },
  );

  // One bad mark would desynchronise the mouth for the rest of the utterance,
  // so the builder drops it rather than sending it.
  it.each(V.avatar.speechMarks)("speechMarks: $name", (c: any) => {
    expect(JSON.parse(speechMarks(c.input))).toEqual(c.expected);
  });
});

describe("loudness", () => {
  // Both SDKs must read the same loudness off the same frame, or a barge-in
  // threshold tuned in one language misfires in the other.
  it.each(V.audio.pcm16Rms.cases)("pcm16Rms: $name", (c: any) => {
    expect(pcm16Rms(Buffer.from(c.pcmBase64, "base64"))).toBeCloseTo(
      c.expected,
      9,
    );
  });
});
