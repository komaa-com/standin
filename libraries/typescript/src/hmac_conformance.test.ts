// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** Shared authentication vectors, including the worker's independent v2 pin. */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// Import the public surface so these tests also guard package exports.
import {
  CHAT_REPLAY_WINDOW_MS,
  REPLAY_WINDOW_MS,
  SIGNATURE_HEADER,
  SIGNATURE_V2_HEADER,
  TIMESTAMP_HEADER,
  canonicalRequest,
  signBody,
  signHandshake,
  signRequest,
  verifyBody,
  verifyHandshake,
} from "./index.js";

const here = dirname(fileURLToPath(import.meta.url));
const V = JSON.parse(
  readFileSync(join(here, "../../../protocol/conformance.json"), "utf8"),
);

function body(c: any): string | Uint8Array {
  return "body" in c
    ? c.body
    : new Uint8Array(Buffer.from(c.bodyBase64, "base64"));
}

describe("chat POST authentication conformance", () => {
  it.each(V.hmacBody.sign)(
    "matches the shared body signature: $name",
    (c: any) => {
      const raw = body(c);
      expect(signBody(c.secret, c.timestampMs, raw)).toBe(c.expected);
      expect(
        verifyBody(
          c.secret,
          c.timestampMs,
          raw,
          c.expected,
          Number(c.timestampMs),
        ),
      ).toBe(true);
      if (typeof raw === "string") {
        expect(
          signBody(c.secret, Number(c.timestampMs), Buffer.from(raw, "utf8")),
        ).toBe(c.expected);
      }
    },
  );

  it.each(V.hmacBody.clockSkew)(
    "keeps POST and WebSocket windows distinct at $skewMs ms",
    (c: any) => {
      const base = V.hmacBody.sign[0];
      const now = Number(base.timestampMs) + c.skewMs;
      expect(
        verifyBody(
          base.secret,
          base.timestampMs,
          base.body,
          base.expected,
          now,
        ),
      ).toBe(c.expected);
      const sig = signHandshake(base.secret, base.timestampMs, "chat");
      expect(
        verifyHandshake(base.secret, base.timestampMs, "chat", sig, now),
      ).toBe(Math.abs(c.skewMs) <= 60_000);
    },
  );

  it.each(V.hmacBody.malformedTimestamps)(
    "rejects signed malformed timestamp %j on both lanes",
    (timestamp: string) => {
      expect(
        verifyBody(
          "secret",
          timestamp,
          "hello",
          signBody("secret", timestamp, "hello"),
          1_700_000_000_000,
        ),
      ).toBe(false);
      expect(
        verifyHandshake(
          "secret",
          timestamp,
          "chat",
          signHandshake("secret", timestamp, "chat"),
          1_700_000_000_000,
        ),
      ).toBe(false);
    },
  );

  it.each([
    undefined,
    "",
    "ab",
    "0".repeat(64),
    "ü".repeat(32),
    "🙂".repeat(16),
  ])("rejects malformed signature %j", (signature) => {
    const c = V.hmacBody.sign[0];
    expect(
      verifyBody(
        c.secret,
        c.timestampMs,
        c.body,
        signature,
        Number(c.timestampMs),
      ),
    ).toBe(false);
  });

  it("rejects missing credentials, body tampering and a changed timestamp", () => {
    const c = V.hmacBody.sign[0];
    const now = Number(c.timestampMs);
    for (const secret of ["", "wrong-secret"]) {
      expect(verifyBody(secret, c.timestampMs, c.body, c.expected, now)).toBe(
        false,
      );
    }
    expect(verifyBody(c.secret, undefined, c.body, c.expected, now)).toBe(
      false,
    );
    expect(
      verifyBody(c.secret, c.timestampMs, `${c.body} `, c.expected, now),
    ).toBe(false);
    expect(verifyBody(c.secret, String(now + 1), c.body, c.expected, now)).toBe(
      false,
    );
    expect(verifyBody(c.secret, c.timestampMs, "chat", c.expected, now)).toBe(
      false,
    );
  });

  it("normalizes signatures and supports an explicit POST replay window", () => {
    const c = V.hmacBody.sign[0];
    const now = Number(c.timestampMs);
    expect(
      verifyBody(
        c.secret,
        c.timestampMs,
        c.body,
        ` ${c.expected.toUpperCase()}\n`,
        now,
      ),
    ).toBe(true);
    expect(
      verifyBody(
        c.secret,
        c.timestampMs,
        c.body,
        c.expected,
        now + 60_000,
        60_000,
      ),
    ).toBe(true);
    expect(
      verifyBody(
        c.secret,
        c.timestampMs,
        c.body,
        c.expected,
        now + 60_001,
        60_000,
      ),
    ).toBe(false);
  });
});

describe("v2 control request conformance", () => {
  it.each(V.hmacV2.sign)(
    "matches the shared canonical request and signature: $name",
    (c: any) => {
      const raw = body(c);
      expect(canonicalRequest(c.method, c.path, raw)).toBe(c.canonical);
      expect(signRequest(c.secret, c.timestampMs, c.method, c.path, raw)).toBe(
        c.expected,
      );
      if (typeof raw === "string") {
        expect(
          signRequest(
            c.secret,
            Number(c.timestampMs),
            c.method,
            c.path,
            Buffer.from(raw, "utf8"),
          ),
        ).toBe(c.expected);
      }
    },
  );

  it.each(V.hmacV2.tamperedRequests)("rejects $name", (c: any) => {
    const original = V.hmacV2.sign[0];
    expect(
      signRequest(
        original.secret,
        original.timestampMs,
        c.method,
        c.path,
        c.body,
      ),
    ).not.toBe(original.expected);
  });

  it("binds the timestamp and cannot be a v1 user signature", () => {
    const c = V.hmacV2.sign[0];
    expect(
      signRequest(
        c.secret,
        Number(c.timestampMs) + 1,
        c.method,
        c.path,
        c.body,
      ),
    ).not.toBe(c.expected);
    expect(signHandshake(c.secret, c.timestampMs, "u1")).not.toBe(c.expected);
    expect(signRequest(c.secret, c.timestampMs, "post", c.path, c.body)).toBe(
      c.expected,
    );
  });

  it("exports the wire header names and separate replay windows", () => {
    expect(TIMESTAMP_HEADER.toLowerCase()).toBe("x-standin-timestamp");
    expect(SIGNATURE_HEADER.toLowerCase()).toBe("x-standin-signature");
    expect(SIGNATURE_V2_HEADER.toLowerCase()).toBe("x-standin-signature-v2");
    expect(REPLAY_WINDOW_MS).toBe(60_000);
    expect(CHAT_REPLAY_WINDOW_MS).toBe(300_000);
  });
});
