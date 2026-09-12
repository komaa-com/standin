// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** Runtime helpers shared by the generated StandIn call protocol bindings. */

const BASE64 =
  /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

/** Read blank strings and non-string identity fields as absent. */
export function clean(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  return value.trim() || undefined;
}

/** Drop malformed JSON while preserving unknown message types for the receive loop. */
export function parseMessage(
  raw: string | Buffer,
): Record<string, unknown> | undefined {
  let obj: unknown;
  try {
    const text =
      typeof raw === "string"
        ? raw
        : new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
            raw,
          );
    obj = JSON.parse(text);
  } catch {
    return undefined;
  }
  if (typeof obj !== "object" || obj === null || Array.isArray(obj))
    return undefined;
  const rec = obj as Record<string, unknown>;
  if (typeof rec.type !== "string") return undefined;
  return rec;
}

/** Decode canonical standard base64 containing complete, nonempty PCM16 samples. */
export function decodePcm(payloadBase64: unknown): Buffer {
  if (typeof payloadBase64 !== "string" || payloadBase64 === "") {
    throw new Error("audio.frame carries no payloadBase64");
  }
  if (!BASE64.test(payloadBase64)) {
    throw new Error("audio.frame payloadBase64 is not valid base64");
  }
  const pcm = Buffer.from(payloadBase64, "base64");
  // Buffer's decoder tolerates invalid padding and nonzero padding bits.
  // Require the same canonical representation as the Python runtime.
  if (encodePcm(pcm) !== payloadBase64) {
    throw new Error("audio.frame payloadBase64 is not valid base64");
  }
  if (pcm.length < 2 || pcm.length % 2 !== 0) {
    throw new Error(`malformed PCM16 payload (${pcm.length} bytes)`);
  }
  return pcm;
}

/** Encode raw PCM bytes using canonical standard base64. */
export function encodePcm(pcm: Buffer): string {
  return pcm.toString("base64");
}

/** Serialize an outbound message as compact JSON. */
export function encode(message: Record<string, unknown>): string {
  return JSON.stringify(message);
}

/** Echo safe integer timestamps, using zero for malformed or imprecise values. */
export function normalizePongTimestamp(value: unknown): number {
  return typeof value === "number" && Number.isSafeInteger(value) ? value : 0;
}

/** The exact context sentences presented to agents by both SDKs. */
export const contextSentences = {
  participants(count: number): string {
    return count <= 1
      ? "This is a 1:1 call with a single human caller."
      : `There are ${count} human participants on this call. ` +
          "Stay quiet unless directly addressed.";
  },
  dtmf(digit: string): string {
    return `The caller pressed the "${digit}" key on their keypad.`;
  },
  recording(status: string): string {
    return status === "active"
      ? "The Microsoft Teams call recording is now ACTIVE."
      : "The Microsoft Teams call recording is not active.";
  },
} as const;
