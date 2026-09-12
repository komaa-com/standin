// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * StandIn signatures for WebSocket handshakes, chat POSTs and control requests.
 *
 * WebSocket handshakes use `HMAC-SHA256(secret, "{timestampMs}.{id}")`,
 * lowercase hex, carried in `X-StandIn-Timestamp` / `X-StandIn-Signature`.
 *
 *     inbound   StandIn dials the call listener; `id` is the callId in the URL
 *               path, and `CallServer` VERIFIES inside a replay window.
 *     outbound  the worker dials the chat channel; `id` is the channel name,
 *               and `ChatChannel` SIGNS.
 *
 * Chat POSTs sign the exact body bytes with a five-minute replay window.
 * Control requests use v2, binding the method, path and hash of the whole body.
 *
 * Byte-for-byte identical to the Python SDK's `standin/sdk/_hmac.py`; the shared
 * conformance vectors assert that a signature produced by either verifies in the
 * other.
 */

import { createHash, createHmac, timingSafeEqual } from "node:crypto";

export const TIMESTAMP_HEADER = "x-standin-timestamp";
export const SIGNATURE_HEADER = "x-standin-signature";
export const SIGNATURE_V2_HEADER = "x-standin-signature-v2";

/** Handshakes are dialed and answered immediately; anything older is a replay. */
export const REPLAY_WINDOW_MS = 60_000;

/** Chat POSTs allow delayed relay retries; WebSocket upgrades still use 60 s. */
export const CHAT_REPLAY_WINDOW_MS = 300_000;

export function nowMs(): number {
  return Date.now();
}

/** Signature for a WebSocket upgrade. */
export function signHandshake(
  secret: string,
  timestampMs: number | string,
  handshakeId: string,
): string {
  return createHmac("sha256", secret)
    .update(`${timestampMs}.${handshakeId}`, "utf8")
    .digest("hex");
}

/** Constant-time check of an inbound upgrade. Empty inputs fail CLOSED. */
export function verifyHandshake(
  secret: string,
  timestamp: string | undefined,
  handshakeId: string,
  signature: string | undefined,
  currentMs?: number,
): boolean {
  if (!secret || !timestamp || !signature) return false;

  // Restrict the timestamp to ASCII decimal, matching the Python SDK.
  // Number() also accepts empty strings, hexadecimal and exponent notation.
  if (!/^-?\d+$/.test(timestamp.trim())) return false;
  const ts = Number(timestamp.trim());
  if (!Number.isFinite(ts)) return false;

  const now = currentMs ?? nowMs();
  if (Math.abs(now - ts) > REPLAY_WINDOW_MS) return false;

  const expected = signHandshake(secret, timestamp, handshakeId);
  const given = signature.trim().toLowerCase();

  // timingSafeEqual THROWS on a length mismatch, and this is reachable by anyone
  // who can open a socket. Unguarded, a short signature header turns an
  // unauthenticated upgrade into a 500 instead of a 401 - a crash path, and an
  // oracle that tells "malformed" apart from "wrong".
  const a = Buffer.from(expected, "utf8");
  const b = Buffer.from(given, "utf8");
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

/** Sign a chat POST's exact transmitted bytes as `{timestampMs}.{rawBody}`.
 * Strings are UTF-8 encoded. Serialize once and send those same bytes.
 */
export function signBody(
  secret: string,
  timestampMs: number | string,
  rawBody: string | Uint8Array,
): string {
  return createHmac("sha256", secret)
    .update(`${timestampMs}.`, "utf8")
    .update(rawBody)
    .digest("hex");
}

/** Verify a chat POST, allowing 300 s of clock skew by default.
 * Missing or malformed headers fail closed. The WebSocket chat channel still
 * signs the channel name and uses the separate 60 s window.
 */
export function verifyBody(
  secret: string,
  timestamp: string | undefined,
  rawBody: string | Uint8Array,
  signature: string | undefined,
  currentMs?: number,
  windowMs = CHAT_REPLAY_WINDOW_MS,
): boolean {
  if (!secret || !timestamp || !signature) return false;
  if (!/^-?\d+$/.test(timestamp.trim())) return false;
  const ts = Number(timestamp.trim());
  if (!Number.isFinite(ts)) return false;
  if (Math.abs((currentMs ?? nowMs()) - ts) > windowMs) return false;

  const expected = Buffer.from(signBody(secret, timestamp, rawBody), "utf8");
  const given = Buffer.from(signature.trim().toLowerCase(), "utf8");
  if (expected.length !== given.length) return false;
  return timingSafeEqual(expected, given);
}

/** Return `METHOD\npath\nsha256_hex(body)` for a v2 control request.
 * The path is the HTTP request path, without the origin or query string,
 * matching the worker verifier. The body covers every field, including tenantId.
 */
export function canonicalRequest(
  method: string,
  path: string,
  rawBody: string | Uint8Array,
): string {
  const bodyHash = createHash("sha256").update(rawBody).digest("hex");
  return `${method.toUpperCase()}\n${path}\n${bodyHash}`;
}

/** Sign an outbound control request for X-StandIn-Signature-V2.
 * Pass the exact bytes you will transmit. v1 handshakes remain supported for
 * WebSockets; control requests should send this v2 header.
 */
export function signRequest(
  secret: string,
  timestampMs: number | string,
  method: string,
  path: string,
  rawBody: string | Uint8Array,
): string {
  return signHandshake(
    secret,
    timestampMs,
    canonicalRequest(method, path, rawBody),
  );
}
