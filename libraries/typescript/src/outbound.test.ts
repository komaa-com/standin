// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Outbound calling: the signed request, the durable store, and the guards.
 *
 * The client is driven against a real local HTTP server that verifies the
 * signature the way StandIn does, so these assert the bytes on the wire rather
 * than that the code runs. The Python twin is `tests/test_outbound.py` and
 * asserts the same behaviours.
 */

import { createServer, type Server } from "node:http";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { CallServer } from "./callServer.js";
import {
  SIGNATURE_V2_HEADER,
  TIMESTAMP_HEADER,
  nowMs,
  signRequest,
} from "./hmac.js";
import {
  OutboundCaller,
  OutboundError,
  OutboundPolicy,
  PendingMessages,
  type PendingMessage,
} from "./outbound.js";

const SECRET = "outbound-secret-never-a-real-one";

interface Seen {
  method: string;
  path: string;
  body: string;
  signed: boolean;
  v1: string;
}

const servers: Server[] = [];
const callServers: CallServer[] = [];

afterEach(async () => {
  for (const s of servers.splice(0)) await new Promise((r) => s.close(r));
  for (const s of callServers.splice(0)) await s.aclose();
});

/** A server that verifies v2 the way the service does, and records what arrived. */
async function fakeStandIn(
  opts: { status?: number; payload?: unknown } = {},
): Promise<{ url: string; seen: Seen[] }> {
  const seen: Seen[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks);
      const path = (req.url ?? "").split("?")[0] ?? "";
      const timestamp = String(req.headers[TIMESTAMP_HEADER] ?? "");
      const signature = String(req.headers[SIGNATURE_V2_HEADER] ?? "");
      const expected = signRequest(
        SECRET,
        timestamp,
        req.method ?? "GET",
        path,
        raw,
      );
      seen.push({
        method: req.method ?? "",
        path,
        body: raw.toString("utf8"),
        signed: signature === expected,
        v1: String(req.headers["x-standin-signature"] ?? ""),
      });
      if (signature !== expected) {
        res.writeHead(401).end("bad signature");
        return;
      }
      const status = opts.status ?? 200;
      if (status >= 400) {
        res.writeHead(status).end("nope");
        return;
      }
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify(
          opts.payload ?? { callId: "call-out-1", scenarioId: "scenario-9" },
        ),
      );
    });
  });
  servers.push(server);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return { url: `http://127.0.0.1:${port}`, seen };
}

function store(): PendingMessages {
  return new PendingMessages(mkdtempSync(join(tmpdir(), "standin-outbound-")));
}

describe("the outbound wire", () => {
  it("signs the exact request", async () => {
    // v2 binds method, path and a hash of the body, which is what puts the
    // tenant under the signature.
    const { url, seen } = await fakeStandIn();
    const placed = await new OutboundCaller({
      secret: SECRET,
      workerUrl: url,
    }).placeCall({
      userObjectId: "aad-1",
      tenantId: "tenant-A",
    });
    expect(placed).toEqual({ callId: "call-out-1", scenarioId: "scenario-9" });
    expect(seen[0]).toMatchObject({
      method: "POST",
      path: "/api/calls",
      signed: true,
    });
    expect(JSON.parse(seen[0]!.body)).toEqual({
      userObjectId: "aad-1",
      tenantId: "tenant-A",
    });
  });

  it("sends no v1 signature", async () => {
    // Sending v1 alongside v2 would let a downgrade pick the weaker one, which
    // leaves the tenant, and so the organisation being rung, unsigned.
    const { url, seen } = await fakeStandIn();
    await new OutboundCaller({ secret: SECRET, workerUrl: url }).placeCall({
      userObjectId: "aad-1",
      tenantId: "tenant-A",
    });
    expect(seen[0]!.v1).toBe("");
  });

  it("signs the call id into the cancel path", async () => {
    const { url, seen } = await fakeStandIn();
    const ok = await new OutboundCaller({
      secret: SECRET,
      workerUrl: url,
    }).cancelCall("call-out-1");
    expect(ok).toBe(true);
    expect(seen[0]).toMatchObject({
      method: "DELETE",
      path: "/api/calls/call-out-1",
      signed: true,
    });
  });

  it("says what to check when the signature is rejected", async () => {
    // A wrong path reads exactly like a wrong secret, so the message names both.
    const { url } = await fakeStandIn();
    const caller = new OutboundCaller({
      secret: "the-wrong-secret",
      workerUrl: url,
    });
    await expect(
      caller.placeCall({ userObjectId: "aad-1", tenantId: "tenant-A" }),
    ).rejects.toThrow(/rejected the signature/);
  });

  it("reports an error status rather than swallowing it", async () => {
    const { url } = await fakeStandIn({ status: 503 });
    await expect(
      new OutboundCaller({ secret: SECRET, workerUrl: url }).placeCall({
        userObjectId: "aad-1",
        tenantId: "tenant-A",
      }),
    ).rejects.toThrow(/503/);
  });

  it("treats a response with no call id as an error", async () => {
    const { url } = await fakeStandIn({ payload: { scenarioId: "s" } });
    await expect(
      new OutboundCaller({ secret: SECRET, workerUrl: url }).placeCall({
        userObjectId: "aad-1",
        tenantId: "tenant-A",
      }),
    ).rejects.toThrow(/no callId/);
  });

  it("never throws on cancel", async () => {
    // It runs on the no-answer path, where an exception turns a tidy-up into a
    // failure and the caller has already stopped waiting.
    const { url } = await fakeStandIn({ status: 500 });
    const caller = new OutboundCaller({ secret: SECRET, workerUrl: url });
    expect(await caller.cancelCall("call-out-1")).toBe(false);
    expect(await caller.cancelCall("")).toBe(false);
  });

  it.each(["ftp://host/x", "http://user:pw@host", "not a url"])(
    "refuses a bad worker url at construction: %s",
    (url) => {
      expect(
        () => new OutboundCaller({ secret: SECRET, workerUrl: url }),
      ).toThrow(OutboundError);
    },
  );

  it("refuses a missing secret at construction", () => {
    const saved = process.env.STANDIN_SECRET;
    delete process.env.STANDIN_SECRET;
    try {
      expect(
        () => new OutboundCaller({ workerUrl: "http://127.0.0.1:9440" }),
      ).toThrow(/STANDIN_SECRET/);
    } finally {
      if (saved !== undefined) process.env.STANDIN_SECRET = saved;
    }
  });
});

describe("the pending store", () => {
  it("survives a new process", () => {
    // The answering leg is a different call and may be a different process. In
    // memory, a restart between the two loses it and the callee hears silence.
    const dir = mkdtempSync(join(tmpdir(), "standin-outbound-"));
    new PendingMessages(dir).park({
      callId: "c1",
      text: "Your build finished.",
    });
    expect(new PendingMessages(dir).pop("c1")?.text).toBe(
      "Your build finished.",
    );
  });

  it("pops a message only once", () => {
    // Two workers answering the same leg is normal. Only one may speak.
    const s = store();
    s.park({ callId: "c1", text: "once" });
    expect(s.pop("c1")).toBeDefined();
    expect(s.pop("c1")).toBeUndefined();
  });

  it("is not an error to pop an unknown call", () => {
    expect(store().pop("never-parked")).toBeUndefined();
  });

  it("remembers the thread it came from", () => {
    // Without it, an unanswered call has nowhere to put the answer.
    const s = store();
    s.park({
      callId: "c1",
      text: "hi",
      threadId: "19:meeting@thread.v2",
      requestedBy: "aad-9",
    });
    const got = s.pop("c1");
    expect(got?.threadId).toBe("19:meeting@thread.v2");
    expect(got?.requestedBy).toBe("aad-9");
  });

  it("keeps a call id from escaping the store directory", () => {
    const dir = mkdtempSync(join(tmpdir(), "standin-outbound-"));
    new PendingMessages(dir).park({ callId: "../../etc/passwd", text: "nope" });
    expect(readdirSync(dir).filter((f) => f.endsWith(".json")).length).toBe(1);
  });

  it("claims stale messages for the no-answer path", () => {
    const s = store();
    s.park({ callId: "old", text: "nobody answered", createdMs: 1 });
    s.park({ callId: "new", text: "still ringing" });

    expect(s.claimStale(1_000).map((m: PendingMessage) => m.callId)).toEqual([
      "old",
    ]);
    // Claimed exactly once, so a second sweep cannot post it twice.
    expect(s.claimStale(1_000)).toEqual([]);
    expect(s.pop("new")).toBeDefined();
  });

  it("judges an orphaned claim by the claim, not the record", () => {
    // A record is claimed BECAUSE it is already old. Judging a half-finished
    // claim by the record's age would let a second sweep take a message the
    // first is still delivering, and post it twice.
    const s = store();
    s.park({ callId: "old", text: "in flight", createdMs: 1 });
    s.claimStale(1_000);
    expect(s.recoverOrphans(60_000)).toEqual([]);
  });
});

describe("the outbound policy", () => {
  it("is off until somebody is allowed", () => {
    // Inbound, the caller chose to dial. Outbound, a model was talked into it.
    expect(() => new OutboundPolicy().check("aad-1")).toThrow(
      /outbound calling is off/,
    );
  });

  it("does not let an inbound allow-all allow an outbound target", () => {
    const policy = new OutboundPolicy({ allowed: ["aad-1"] });
    policy.check("aad-1");
    expect(() => policy.check("aad-2")).toThrow(
      /not on this agent's outbound allowlist/,
    );
  });

  it("counts placed calls against the hourly cap", () => {
    const policy = new OutboundPolicy({ allowed: ["aad-1"], maxPerHour: 2 });
    for (let i = 0; i < 2; i += 1) {
      policy.check("aad-1");
      policy.record();
    }
    expect(() => policy.check("aad-1")).toThrow(/already placed 2 calls/);
  });

  it("treats a zero cap as no cap", () => {
    const policy = new OutboundPolicy({ allowed: ["aad-1"], maxPerHour: 0 });
    for (let i = 0; i < 50; i += 1) {
      policy.check("aad-1");
      policy.record();
    }
  });
});

describe("the outcome route", () => {
  async function listen(
    onCallOutcome?: (callId: string, outcome: string) => void,
  ): Promise<CallServer> {
    const server = new CallServer({
      handlerFactory: () => ({}),
      secret: SECRET,
      host: "127.0.0.1",
      port: 0,
      ...(onCallOutcome ? { onCallOutcome } : {}),
    });
    await server.start();
    callServers.push(server);
    return server;
  }

  async function post(
    server: CallServer,
    callId: string,
    body: string,
    opts: { secret?: string; timestamp?: number } = {},
  ): Promise<number> {
    const path = `${server.wsPath}/outcome/${callId}`;
    const timestamp = String(opts.timestamp ?? nowMs());
    const response = await fetch(`http://127.0.0.1:${server.port}${path}`, {
      method: "POST",
      headers: {
        [TIMESTAMP_HEADER]: timestamp,
        [SIGNATURE_V2_HEADER]: signRequest(
          opts.secret ?? SECRET,
          timestamp,
          "POST",
          path,
          body,
        ),
      },
      body,
    });
    return response.status;
  }

  it("delivers a signed outcome to the plugin", async () => {
    // The only signal that nobody answered. Without it an unanswered call waits
    // out the ring timeout before anything can be said about it.
    const seen: Array<[string, string]> = [];
    const server = await listen((callId, outcome) =>
      seen.push([callId, outcome]),
    );
    expect(await post(server, "call-out-1", '{"outcome":"no-answer"}')).toBe(
      204,
    );
    expect(seen).toEqual([["call-out-1", "no-answer"]]);
  });

  it("refuses an unsigned outcome", async () => {
    const seen: Array<[string, string]> = [];
    const server = await listen((callId, outcome) =>
      seen.push([callId, outcome]),
    );
    expect(await post(server, "c", "{}", { secret: "the-wrong-secret" })).toBe(
      401,
    );
    expect(seen).toEqual([]);
  });

  it("refuses a stale outcome", async () => {
    // The signature is only worth anything inside the replay window.
    const seen: Array<[string, string]> = [];
    const server = await listen((callId, outcome) =>
      seen.push([callId, outcome]),
    );
    expect(
      await post(server, "c", "{}", { timestamp: nowMs() - 10_000_000 }),
    ).toBe(401);
    expect(seen).toEqual([]);
  });

  it("refuses an oversized outcome", async () => {
    // The body must be read before a signature over its hash can be checked, so
    // the cap is what bounds an unauthenticated peer.
    const seen: Array<[string, string]> = [];
    const server = await listen((callId, outcome) =>
      seen.push([callId, outcome]),
    );
    expect(await post(server, "c", "x".repeat(9 * 1024))).toBe(413);
    expect(seen).toEqual([]);
  });

  it("opens no route unless a plugin asks for one", async () => {
    // A worker that never places a call opens no extra surface.
    const server = await listen();
    const response = await fetch(
      `http://127.0.0.1:${server.port}${server.wsPath}/outcome/c`,
      {
        method: "POST",
        body: "{}",
      },
    );
    expect(response.status).toBe(404);
  });

  it("acknowledges even when the plugin throws", async () => {
    // Otherwise StandIn retries a report the worker will never accept.
    const server = await listen(() => {
      throw new Error("the plugin is broken");
    });
    expect(await post(server, "c", '{"outcome":"no-answer"}')).toBe(204);
  });
});
