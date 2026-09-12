// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Proving an install works, without a bill, a tunnel or a Microsoft tenant.
 *
 * The question every plugin has to answer before anybody trusts it with a real
 * call is "does this actually work here?", and until now the only way to find
 * out was to place a real call: a provider bill, a public tunnel, a Teams
 * tenant and somebody's afternoon.
 *
 * So this rings the worker itself. It binds an ephemeral listener on loopback,
 * connects a real client that speaks the real call wire, streams a few frames
 * of silence, and reports what came back. Everything it exercises is the thing
 * that breaks in the field: the secret, the bind, the handshake, the session,
 * and whether audio makes the round trip.
 *
 * The client lives next to the wire it speaks, deliberately. A copy of it kept
 * in a plugin drifts, and the copy that used to exist kept passing against a
 * path that no longer existed, which is exactly the failure a smoke check is
 * for.
 *
 * **Nothing that delivers is started.** No pending-message sweep, no no-answer
 * reaper, no outbound caller, no chat lane. A verification command that quietly
 * resumed durable jobs would place real calls and post real messages as a side
 * effect of somebody typing `smoke`, which is the single worst thing this could
 * possibly do.
 *
 * Identical in shape to the Python SDK's `standin.smoke`.
 */

import { randomBytes } from "node:crypto";

import WebSocket from "ws";

import { CallServer } from "./callServer.js";
import type { HandlerFactory } from "./handler.js";
import {
  SIGNATURE_HEADER,
  TIMESTAMP_HEADER,
  nowMs,
  signHandshake,
} from "./hmac.js";
import { logger } from "./log.js";

/** PCM16 mono at 16 kHz for 20 ms. The cadence a real call arrives at. */
export const FRAME_MS = 20;
export const FRAME_BYTES = ((16_000 * FRAME_MS) / 1000) * 2;

/**
 * How long the whole run may take before it is a recorded failure rather than a
 * hang. A listener that accepts and then stops talking would otherwise hang CI,
 * and hang a status tool call for ever.
 */
export const RUN_TIMEOUT_MS = 15_000;
export const CONNECT_TIMEOUT_MS = 5_000;

/** One thing that either works here or does not. */
export interface SmokeCheck {
  readonly name: string;
  readonly ok: boolean;
  readonly detail?: string;
  /**
   * What stops working when this fails. A report that names the missing surface
   * without naming the consequence sends the operator to the source.
   */
  readonly cost?: string;
  /**
   * Whether `ok` depends on it. Counted by default, so a check a plugin adds
   * counts unless it passes `required: false`.
   */
  readonly required?: boolean;
}

/** What the run found. */
export interface SmokeResult {
  readonly checks: SmokeCheck[];
  readonly echoFrames: number;
  readonly error?: string;
  /**
   * True only when every mandatory check passed.
   *
   * Never true when nothing echoed: the point of the run is that audio made the
   * round trip, and a run that proves nothing must not read as a pass.
   */
  readonly ok: boolean;
}

/**
 * A client that speaks the call wire, for one call that nobody is on.
 *
 * Used by {@link runSmoke}. Exported because a plugin with its own listener may
 * want to point one at it.
 */
export class SyntheticCall {
  echoFrames = 0;
  readonly #url: string;
  readonly #secret: string;
  readonly #callId: string;
  readonly #frames: number;
  readonly #connectTimeoutMs: number;

  constructor(
    url: string,
    secret: string,
    callId: string,
    frames = 10,
    connectTimeoutMs = CONNECT_TIMEOUT_MS,
  ) {
    this.#url = url;
    this.#secret = secret;
    this.#callId = callId;
    this.#frames = Math.max(1, frames);
    this.#connectTimeoutMs = connectTimeoutMs;
  }

  /** Connect, greet, stream, hang up. Rejects on anything that fails. */
  async run(): Promise<void> {
    // Signed freshly for this connect. The listener enforces single use inside
    // the freshness window, so a retry that reused these headers would loop on
    // 401 and read as a wrong secret.
    const stamp = nowMs();
    const ws = new WebSocket(this.#url, {
      headers: {
        [TIMESTAMP_HEADER]: String(stamp),
        [SIGNATURE_HEADER]: signHandshake(this.#secret, stamp, this.#callId),
      },
      handshakeTimeout: this.#connectTimeoutMs,
    });
    ws.on("message", (data) => {
      try {
        if (JSON.parse(String(data)).type === "audio.frame")
          this.echoFrames += 1;
      } catch {
        // Not ours to interpret. A frame this cannot read is not an echo.
      }
    });

    try {
      await new Promise<void>((resolve, reject) => {
        ws.once("open", resolve);
        ws.once("error", reject);
        ws.once("close", (code: number) =>
          reject(new Error(`the socket closed with ${code}`)),
        );
      });
      ws.removeAllListeners("close");
      ws.removeAllListeners("error");

      let closed = "";
      ws.on("close", (code: number) => {
        closed = `the socket closed with ${code}`;
      });
      ws.on("error", (err: Error) => {
        closed = String(err);
      });

      ws.send(
        JSON.stringify({
          type: "session.start",
          // Identical to the id in the path. The listener refuses a start that
          // disagrees with the authenticated path, and the refusal would read
          // as a wire fault.
          callId: this.#callId,
          threadId: "",
          direction: "inbound",
          caller: { displayName: "StandIn smoke check" },
        }),
      );
      // Handlers commonly gate output on the call being recorded, so without
      // this a recording-gated handler stays silent and the run reports a false
      // negative.
      ws.send(JSON.stringify({ type: "recording.status", status: "active" }));

      const silence = Buffer.alloc(FRAME_BYTES).toString("base64");
      for (let seq = 0; seq < this.#frames; seq += 1) {
        if (closed !== "") throw new Error(closed);
        ws.send(
          JSON.stringify({
            type: "audio.frame",
            seq,
            timestampMs: seq * FRAME_MS,
            payloadBase64: silence,
          }),
        );
        await new Promise((resolve) => setTimeout(resolve, FRAME_MS));
      }
      ws.send(JSON.stringify({ type: "session.end", reason: "smoke-done" }));
      // One cadence beat for whatever the handler said last to arrive.
      await new Promise((resolve) => setTimeout(resolve, FRAME_MS * 2));
    } finally {
      // On every path. An error path that skipped this leaks a socket per run
      // inside a long-lived host that exposes the check.
      try {
        ws.close();
      } catch {
        // Never connected, or already gone.
      }
    }
  }
}

/** Extra checks a plugin runs after the call. */
export type ExtraChecks = () => Promise<SmokeCheck[]>;

/**
 * Ring this worker's own handler and report what worked.
 *
 * `extra` is a plugin's own checks, run after the call. Each one counts
 * towards `ok` unless it says `required: false`.
 */
export async function runSmoke(
  handlerFactory: HandlerFactory,
  frames = 10,
  extra?: ExtraChecks,
  timeoutMs = RUN_TIMEOUT_MS,
): Promise<SmokeResult> {
  const checks: SmokeCheck[] = [];
  let error = "";
  let echoFrames = 0;

  // Its own credential, never the operator's. A fixed string would be a
  // predictable one on a live listener, and borrowing the operator's makes the
  // run pass or fail for reasons that have nothing to do with the wire. It also
  // makes the check runnable before the secret is configured, which is exactly
  // when people run it.
  const secret = randomBytes(16).toString("hex");
  checks.push({
    name: "secret",
    ok: true,
    detail: "generated for this run",
    cost: "nothing could authenticate",
  });

  const server = new CallServer({
    handlerFactory,
    secret,
    host: "127.0.0.1",
    port: 0,
    staleCallReaperMs: 0,
  });
  try {
    await server.start();
  } catch (err) {
    return finish(checks, 0, String(err), [
      {
        name: "listener",
        ok: false,
        detail: String(err),
        cost: "no call could ever reach this worker",
      },
    ]);
  }
  checks.push({
    name: "listener",
    ok: true,
    detail: `127.0.0.1:${server.port}${server.wsPath}`,
    cost: "no call could ever reach this worker",
  });

  const callId = `smoke-${randomBytes(4).toString("hex")}`;
  const url = `ws://127.0.0.1:${server.port}${server.wsPath}/${callId}`;
  const call = new SyntheticCall(url, secret, callId, frames);
  const started = Date.now();
  try {
    await withTimeout(call.run(), timeoutMs);
    checks.push({
      name: "call",
      ok: true,
      detail: `${frames} frames in ${Date.now() - started} ms`,
      cost: "calls do not connect",
    });
  } catch (err) {
    const timedOut = err === TIMED_OUT;
    error = timedOut
      ? `the call did not finish within ${timeoutMs} ms`
      : String(err);
    checks.push({
      name: "call",
      ok: false,
      detail: error,
      // A hang and a refusal are different things to go and look at, so they
      // are told apart here rather than both reading as "it did not work".
      cost: timedOut
        ? "a real call would hang the same way"
        : "a real call would fail the same way",
    });
  } finally {
    try {
      await server.aclose();
    } catch {
      // Already down. Nothing to report about a teardown nobody waited on.
    }
  }

  echoFrames = call.echoFrames;
  checks.push({
    name: "audio",
    ok: echoFrames > 0,
    detail: `${echoFrames} frames came back`,
    cost: "the caller would hear nothing",
  });

  if (extra !== undefined) {
    try {
      checks.push(...(await extra()));
    } catch (err) {
      logger.debug(`standin: a plugin smoke check failed: ${String(err)}`);
      // Advisory: the plugin's own checks failing to RUN says nothing about
      // whether a call works, which is what this command answers.
      checks.push({
        name: "plugin checks",
        ok: false,
        detail: String(err),
        cost: "this plugin's own checks did not run",
        required: false,
      });
    }
  }
  return finish(checks, echoFrames, error);
}

/** The result as something to print. */
export function report(result: SmokeResult): string {
  const lines = [`standin: ${result.ok ? "ok" : "NOT ok"}`];
  for (const check of result.checks) {
    const mark = check.ok ? "ok  " : "FAIL";
    lines.push(
      `  ${mark} ${check.name}${check.detail ? ` (${check.detail})` : ""}`,
    );
    if (!check.ok && check.cost) lines.push(`       without it: ${check.cost}`);
  }
  if (result.error) lines.push(`  error: ${result.error}`);
  return lines.join("\n");
}

function finish(
  checks: SmokeCheck[],
  echoFrames: number,
  error: string,
  extra: SmokeCheck[] = [],
): SmokeResult {
  const all = [...checks, ...extra];
  return {
    checks: all,
    echoFrames,
    error: error || undefined,
    ok: all.every((check) => check.required === false || check.ok),
  };
}

const TIMED_OUT = Symbol("standin.smokeTimeout");

async function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      work,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(TIMED_OUT), ms);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}
