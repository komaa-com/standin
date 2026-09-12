// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT
// GENERATED from protocol/schema.yaml; do not hand-edit.
// Schema SHA-256: 9157d78287022cc97566640007ea61c5336cb11c731ba5fcb15b0833b8048d5b
// Regenerate with: python3 protocol/generate.py

import { clean, normalizePongTimestamp } from "./protocolRuntime.js";
export { contextSentences, decodePcm, parseMessage } from "./protocolRuntime.js";

export const SAMPLE_RATE_HZ = 16_000;
export const NUM_CHANNELS = 1;

export const TYPE_SESSION_START = "session.start";
export const TYPE_SESSION_END = "session.end";
export const TYPE_RECORDING_STATUS = "recording.status";
export const TYPE_AUDIO_FRAME = "audio.frame";
export const TYPE_VIDEO_FRAME = "video.frame";
export const TYPE_PARTICIPANTS = "participants";
export const TYPE_DTMF = "dtmf";
export const TYPE_PING = "ping";
export const TYPE_ASSISTANT_SAY = "assistant.say";
export const TYPE_ASSISTANT_CANCEL = "assistant.cancel";
export const TYPE_EXPRESSION = "expression";
export const TYPE_SPEECH_MARKS = "speech.marks";
export const TYPE_DISPLAY_IMAGE = "display.image";
export const TYPE_DISPLAY_FRAME = "display.frame";
export const TYPE_PONG = "pong";

/** Caller identity; blank or absent values normalize to undefined. */
export interface Caller {
  readonly aadId?: string;
  readonly displayName?: string;
  readonly tenantId?: string;
}

/** Call context with the SDK's compatible defaults. */
export interface SessionStart {
  readonly callId: string;
  readonly threadId: string;
  readonly caller: Caller;
  readonly direction: "inbound" | "outbound";
  readonly recordingStatus?: string;
  readonly tenantId?: string;
}

/** Read call context; only callId lacks a safe default. */
export function parseSessionStart(msg: Record<string, unknown>): SessionStart {
  const callId = clean(msg.callId);
  if (!callId) throw new Error("session.start is missing callId");
  const rawCaller = msg.caller;
  const callerObj: Record<string, unknown> =
    typeof rawCaller === "object" && rawCaller !== null && !Array.isArray(rawCaller)
      ? (rawCaller as Record<string, unknown>) : {};
  const direction = clean(msg.direction) ?? "inbound";
  return {
    callId: callId,
    threadId: clean(msg.threadId) ?? "",
    caller: {
      aadId: clean(callerObj.aadId),
      displayName: clean(callerObj.displayName),
      tenantId: clean(callerObj.tenantId),
    },
    direction: direction === "inbound" || direction === "outbound" ? direction : "inbound",
    recordingStatus: clean(msg.recordingStatus),
    tenantId: clean(msg.tenantId),
  };
}

/** Build an outbound `audio.frame` JSON frame. */
export function audioFrame(seq: number, timestampMs: number, pcm: Buffer): string {
  return JSON.stringify({
    type: TYPE_AUDIO_FRAME,
    seq: seq,
    timestampMs: timestampMs,
    payloadBase64: pcm.toString("base64"),
  });
}

/** Build an outbound `pong` JSON frame. */
export function pong(ts: unknown): string {
  return JSON.stringify({
    type: TYPE_PONG,
    ts: normalizePongTimestamp(ts),
  });
}

/** Build an outbound `assistant.cancel` JSON frame. */
export function assistantCancel(turnId: number): string {
  return JSON.stringify({
    type: TYPE_ASSISTANT_CANCEL,
    turnId: turnId,
  });
}

/** Build an outbound `session.end` JSON frame. */
export function sessionEnd(reason: string): string {
  return JSON.stringify({
    type: TYPE_SESSION_END,
    reason: reason,
  });
}
