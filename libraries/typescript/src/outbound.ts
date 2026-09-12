// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Calling somebody, instead of waiting for them to call you.
 *
 * Every other lane in this SDK starts with a caller dialling your agent. This
 * one runs the other way: your agent asks StandIn to ring a Microsoft Teams
 * user, and speaks when they answer.
 *
 * That inversion is what makes it worth its own module, because the leg that
 * answers is **a different call**. You ask for the call in one place, and
 * minutes later StandIn dials your worker with `direction: "outbound"` and a
 * fresh `callId`. The thing you wanted said has to survive the gap, so
 * {@link PendingMessages} parks it on disk and the handler pops it when the leg
 * arrives. Park it in memory and a restart between the two loses it silently,
 * with the caller's phone still ringing.
 *
 * **Read the policy before you skip it.** Inbound, the caller chose to dial you.
 * Outbound, a model decided to ring somebody, and that model is steered by
 * whoever is talking to it. An agent with an outbound tool and no allowlist is
 * an agent that can be talked into cold-calling your directory, which is why the
 * allowlist here is separate from and stricter than any inbound one.
 *
 * Identical in shape to the Python SDK's `standin.outbound`.
 */

import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import type { ToolSpec } from "./callTools.js";
import { StandInError } from "./errors.js";
import {
  SIGNATURE_V2_HEADER,
  TIMESTAMP_HEADER,
  nowMs,
  signRequest,
} from "./hmac.js";
import { logger } from "./log.js";

/**
 * The control route StandIn exposes for placing a call. v2 signs the path, so
 * this string is part of the signature: a route that is merely plausible
 * produces a valid-looking request that is refused.
 */
const PLACE_PATH = "/api/calls";

/** Where the worker listens for control requests, when nothing says otherwise. */
const DEFAULT_WORKER_URL = "http://127.0.0.1:9440";

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * Placing or cancelling an outbound call failed.
 *
 * Carries the reason in its message, because the thing that usually wants it is
 * a tool result being read back to whoever asked for the call.
 */
export class OutboundError extends StandInError {
  constructor(message: string) {
    super(message);
    this.name = "OutboundError";
  }
}

/** StandIn accepted the request and is ringing the callee. */
export interface PlacedCall {
  /** The id the answering leg will arrive with. Park your message against it. */
  readonly callId: string;
  /** StandIn's own correlation id, when it sends one. */
  readonly scenarioId: string;
}

/**
 * Where durable outbound state lives.
 *
 * `STANDIN_STATE_DIR` when set, otherwise `~/.standin/state`. Deliberately NOT a
 * temp directory: a temp directory passes every test and loses every parked
 * message on the next reboot, which is invisible until a caller answers a call
 * that then says nothing.
 */
export function stateDir(): string {
  const configured = (process.env.STANDIN_STATE_DIR ?? "").trim();
  const path = configured || join(homedir(), ".standin", "state");
  mkdirSync(path, { recursive: true, mode: 0o700 });
  return path;
}

/** Options for {@link OutboundCaller}. */
export interface OutboundCallerOptions {
  secret?: string;
  workerUrl?: string;
  timeoutMs?: number;
}

/**
 * Asks StandIn to ring a Microsoft Teams user.
 *
 * One instance per worker is enough; it holds no per-call state.
 *
 * Signed with v2 only, and that is deliberate. v2 binds the method, the path and
 * a hash of the body, which is what puts `tenantId` under the signature. v1
 * signs a single value, so a v1-signed request leaves the organisation being
 * rung unsigned, and sending both would let a downgrade pick the weaker one.
 */
export class OutboundCaller {
  readonly #secret: string;
  readonly #workerUrl: string;
  readonly #timeoutMs: number;

  constructor(options: OutboundCallerOptions = {}) {
    this.#secret = options.secret ?? process.env.STANDIN_SECRET ?? "";
    if (!this.#secret) {
      throw new OutboundError(
        "STANDIN_SECRET is required to place an outbound call",
      );
    }
    const raw =
      options.workerUrl || process.env.STANDIN_WORKER_URL || DEFAULT_WORKER_URL;
    this.#workerUrl = checkWorkerUrl(raw);
    this.#timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  }

  /** The control endpoint this caller talks to. */
  get workerUrl(): string {
    return this.#workerUrl;
  }

  /**
   * Ring a Microsoft Teams user. Returns the id the answering leg carries.
   *
   * Throws {@link OutboundError} for anything that is not an accepted request,
   * with the reason in the message, so a tool can read it back to whoever asked
   * for the call.
   */
  async placeCall(opts: {
    userObjectId: string;
    tenantId: string;
  }): Promise<PlacedCall> {
    const target = opts.userObjectId.trim();
    if (!target)
      throw new OutboundError(
        "an outbound call needs the person's directory id",
      );
    const body = JSON.stringify({
      userObjectId: target,
      tenantId: opts.tenantId.trim(),
    });
    const payload = await this.#send("POST", PLACE_PATH, body);
    const callId = payload.callId ?? payload.call_id;
    if (typeof callId !== "string" || !callId) {
      throw new OutboundError(
        "StandIn accepted the call but returned no callId",
      );
    }
    const scenario = payload.scenarioId ?? payload.scenario_id ?? "";
    return { callId, scenarioId: String(scenario) };
  }

  /**
   * Stop a call that is still ringing. Never throws.
   *
   * Best-effort on purpose: this runs on the no-answer path, where the caller
   * has already stopped waiting and an exception would only turn a tidy-up into
   * a failure. A call that has already gone counts as cancelled.
   */
  async cancelCall(callId: string): Promise<boolean> {
    if (!callId) return false;
    try {
      await this.#send("DELETE", `${PLACE_PATH}/${callId}`, "");
      return true;
    } catch (err) {
      logger.info(
        `standin: could not cancel outbound call ${callId}: ${String(err)}`,
      );
      return false;
    }
  }

  async #send(
    method: string,
    path: string,
    body: string,
  ): Promise<Record<string, unknown>> {
    const timestamp = String(nowMs());
    const headers: Record<string, string> = {
      [TIMESTAMP_HEADER]: timestamp,
      [SIGNATURE_V2_HEADER]: signRequest(
        this.#secret,
        timestamp,
        method,
        path,
        body,
      ),
      "content-type": "application/json",
    };
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      const response = await fetch(`${this.#workerUrl}${path}`, {
        method,
        headers,
        body: body || undefined,
        signal: controller.signal,
      });
      const text = await response.text();
      if (response.status === 401) {
        // The one failure worth naming precisely: v2 signs the path, so a wrong
        // path reads exactly like a wrong secret and has cost people hours.
        throw new OutboundError(
          `StandIn rejected the signature on ${method} ${path}. ` +
            "Check STANDIN_SECRET, and that the clock is not skewed.",
        );
      }
      if (!response.ok) {
        throw new OutboundError(
          `${method} ${path} returned HTTP ${response.status}: ${text.slice(0, 200)}`,
        );
      }
      if (!text.trim()) return {};
      try {
        const parsed: unknown = JSON.parse(text);
        return typeof parsed === "object" && parsed !== null
          ? (parsed as Record<string, unknown>)
          : {};
      } catch {
        return {};
      }
    } catch (err) {
      if (err instanceof OutboundError) throw err;
      if (err instanceof Error && err.name === "AbortError") {
        throw new OutboundError(
          `${method} ${path} timed out after ${this.#timeoutMs}ms`,
        );
      }
      throw new OutboundError(
        `could not reach the StandIn worker at ${this.#workerUrl}: ${String(err)}`,
      );
    } finally {
      clearTimeout(deadline);
    }
  }
}

function checkWorkerUrl(raw: string): string {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new OutboundError(`STANDIN_WORKER_URL is not a valid URL: ${raw}`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new OutboundError(
      `STANDIN_WORKER_URL must be http or https, got ${raw}`,
    );
  }
  if (!url.hostname)
    throw new OutboundError(`STANDIN_WORKER_URL has no host: ${raw}`);
  if (url.username || url.password) {
    throw new OutboundError("STANDIN_WORKER_URL must not carry credentials");
  }
  return raw.replace(/\/+$/, "");
}

// ------------------------------------------------------------- what to say

/** What to say when this call is answered, and where it came from. */
export interface PendingMessage {
  readonly callId: string;
  /** The line to speak on answer. */
  readonly text: string;
  /**
   * The Microsoft Teams conversation the request came from, so an unanswered
   * call can put the answer there instead of losing it.
   */
  readonly threadId?: string;
  /** Directory id of whoever asked for the call. */
  readonly requestedBy?: string;
  readonly createdMs?: number;
  readonly metadata?: Record<string, unknown>;
  /** Which tenant to post the fallback into. Never taken from a model. */
  readonly tenantId?: string;
  /**
   * Directory id of whoever was rung, so a second call to the same person can
   * be refused while the first is still ringing.
   */
  readonly target?: string;
  /** Delivery attempts so far, so a chat that keeps failing stops. */
  readonly attempts?: number;
}

/**
 * What the agent wanted said, parked until the call is answered.
 *
 * On disk, because the answering leg is a different call and may be a different
 * process. A restart between asking for a call and it being answered is
 * ordinary, and an in-memory store loses the message silently: the callee picks
 * up and hears nothing.
 *
 * Popping is atomic. Two workers racing the same answered call is a normal
 * consequence of running more than one, and only one of them may speak.
 */
export class PendingMessages {
  readonly #dir: string;

  constructor(directory?: string) {
    this.#dir = directory ?? join(stateDir(), "outbound");
    mkdirSync(this.#dir, { recursive: true, mode: 0o700 });
  }

  #path(callId: string): string {
    return join(this.#dir, `${safeName(callId)}.json`);
  }

  /** Remember what to say on this call. Overwrites an earlier one. */
  park(message: PendingMessage): void {
    const record = { ...message, createdMs: message.createdMs || nowMs() };
    const target = this.#path(message.callId);
    // Written beside and renamed, so a reader never sees half a record.
    const temp = `${target}.${randomUUID().replace(/-/g, "")}.tmp`;
    mkdirSync(dirname(target), { recursive: true, mode: 0o700 });
    writeFileSync(temp, JSON.stringify(record), { mode: 0o600 });
    renameSync(temp, target);
  }

  /**
   * Take the message for this call, once.
   *
   * The rename is the lock: exactly one caller can rename a given file, so two
   * workers answering the same leg cannot both speak.
   */
  pop(callId: string): PendingMessage | undefined {
    const target = this.#path(callId);
    const claimed = `${target}.${randomUUID().replace(/-/g, "")}.claimed`;
    try {
      renameSync(target, claimed);
    } catch {
      return undefined;
    }
    try {
      return JSON.parse(readFileSync(claimed, "utf8")) as PendingMessage;
    } catch {
      return undefined;
    } finally {
      try {
        unlinkSync(claimed);
      } catch {
        // already gone
      }
    }
  }

  /**
   * Take every message nobody answered in time.
   *
   * Used by the no-answer sweep: a parked message older than the ringing window
   * means the callee never picked up, and what the agent wanted said should go
   * to the chat it came from rather than evaporate.
   */
  /**
   * Every parked record, read without claiming any of them.
   *
   * For asking "am I already calling this person?" before ringing them again. A
   * reserved record is deliberately absent: that call is already connected, so
   * it is not one somebody is still waiting on.
   */
  waiting(): PendingMessage[] {
    const out: PendingMessage[] = [];
    for (const name of readdirSync(this.#dir).sort()) {
      if (!name.endsWith(".json")) continue;
      try {
        out.push(
          JSON.parse(
            readFileSync(join(this.#dir, name), "utf8"),
          ) as PendingMessage,
        );
      } catch {
        // Unreadable is not this method's problem.
      }
    }
    return out;
  }

  /**
   * Take this record for a leg that is ringing, without deleting it.
   *
   * A rename, exactly like {@link pop}, so only one worker can hold it. The
   * difference is what happens next: a reserved record can be given BACK. A leg
   * that never gets answered has to leave the message where the sweep will find
   * it, or the answer is lost because nobody picked up.
   *
   * While reserved the record is invisible to {@link claimStale}, which reads
   * `.json`: the sweep must not post "I could not reach you" to a call that is
   * still ringing.
   */
  reserve(callId: string): PendingMessage | undefined {
    const target = this.#path(callId);
    const held = target.replace(/\.json$/, ".answering");
    try {
      renameSync(target, held);
    } catch {
      return undefined;
    }
    try {
      return JSON.parse(readFileSync(held, "utf8")) as PendingMessage;
    } catch {
      rmSync(held, { force: true });
      return undefined;
    }
  }

  /** It was said. Retire the record. */
  commit(callId: string): void {
    rmSync(this.#path(callId).replace(/\.json$/, ".answering"), {
      force: true,
    });
  }

  /** It was not said. Put it back for the sweep to deliver to chat. */
  release(callId: string): void {
    const held = this.#path(callId).replace(/\.json$/, ".answering");
    try {
      if (existsSync(held)) renameSync(held, this.#path(callId));
    } catch {
      // The recovery below is the backstop.
    }
  }

  /**
   * Give back reservations whose worker died holding them.
   *
   * Judged by the reservation's own age. Without this a process that dies
   * mid-ring leaves the message reserved for ever, and the person who was
   * promised an answer never gets one.
   */
  recoverReservations(olderThanMs: number): number {
    const cutoff = Date.now() - olderThanMs;
    let recovered = 0;
    for (const name of readdirSync(this.#dir)) {
      if (!name.endsWith(".answering")) continue;
      const path = join(this.#dir, name);
      try {
        if (statSync(path).mtimeMs > cutoff) continue;
        renameSync(path, path.replace(/\.answering$/, ".json"));
        recovered += 1;
      } catch {
        // Gone, or taken by somebody else.
      }
    }
    return recovered;
  }

  claimStale(olderThanMs: number): PendingMessage[] {
    const cutoff = nowMs() - olderThanMs;
    const taken: PendingMessage[] = [];
    for (const name of readdirSync(this.#dir).sort()) {
      if (!name.endsWith(".json")) continue;
      let record: PendingMessage;
      try {
        record = JSON.parse(
          readFileSync(join(this.#dir, name), "utf8"),
        ) as PendingMessage;
      } catch {
        continue;
      }
      if ((record.createdMs ?? 0) > cutoff) continue;
      const popped = this.pop(record.callId || name.replace(/\.json$/, ""));
      if (popped) taken.push(popped);
    }
    return taken;
  }

  /**
   * Take back messages a crashed sweep claimed and never delivered.
   *
   * Judged by how long ago the CLAIM was made, which is why claiming writes a
   * fresh file rather than renaming in place. Give this a longer window than
   * {@link claimStale}, so an in-flight delivery is never taken from under a
   * worker that is still working on it.
   */
  recoverOrphans(olderThanMs: number): PendingMessage[] {
    const cutoff = Date.now() - olderThanMs;
    const taken: PendingMessage[] = [];
    for (const name of readdirSync(this.#dir).sort()) {
      if (!name.endsWith(".claimed")) continue;
      const path = join(this.#dir, name);
      try {
        if (statSync(path).mtimeMs > cutoff) continue;
        taken.push(JSON.parse(readFileSync(path, "utf8")) as PendingMessage);
        unlinkSync(path);
      } catch {
        continue;
      }
    }
    return taken;
  }
}

/** A filename that cannot escape the directory it belongs in. */
function safeName(callId: string): string {
  const cleaned = [...callId]
    .map((c) => (/[A-Za-z0-9\-_.]/.test(c) ? c : "-"))
    .join("")
    .slice(0, 120);
  return cleaned || "unnamed";
}

// ----------------------------------------------------------------- the policy

/** Options for {@link OutboundPolicy}. */
export interface OutboundPolicyOptions {
  /** Directory ids the agent may ring. Empty means outbound is off. */
  allowed?: Iterable<string>;
  /**
   * Calls placed in any rolling hour, across all targets. Zero means no cap,
   * which is a deliberate choice rather than a default.
   */
  maxPerHour?: number;
}

/**
 * Who this agent may ring, and how often.
 *
 * Separate from any inbound allowlist, and stricter, because the two answer
 * different questions. Inbound asks "may this person talk to the agent?" and the
 * person chose to dial. Outbound asks "may the agent ring this person?" and the
 * agent was talked into it by whoever is on the call.
 *
 * So allowing every inbound caller allows no outbound target.
 */
export class OutboundPolicy {
  readonly allowed: ReadonlySet<string>;
  readonly maxPerHour: number;
  #placed: number[] = [];

  constructor(options: OutboundPolicyOptions = {}) {
    this.allowed = new Set(options.allowed ?? []);
    this.maxPerHour = Math.max(0, options.maxPerHour ?? 6);
  }

  /**
   * Read `STANDIN_OUTBOUND_ALLOW` and `STANDIN_OUTBOUND_MAX_PER_HOUR`.
   *
   * Unset means outbound calling is off, which is the right default for a
   * capability that can ring a stranger.
   */
  static fromEnv(): OutboundPolicy {
    const allowed = (process.env.STANDIN_OUTBOUND_ALLOW ?? "")
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
    const raw = Number.parseInt(
      process.env.STANDIN_OUTBOUND_MAX_PER_HOUR ?? "6",
      10,
    );
    return new OutboundPolicy({
      allowed,
      maxPerHour: Number.isFinite(raw) ? raw : 6,
    });
  }

  /** Throws {@link OutboundError} unless this call may be placed now. */
  check(userObjectId: string): void {
    // Folded on both sides below. A directory id is not case-sensitive, and a
    // case mismatch would read as "not allowed" with nothing to say why.
    const target = userObjectId.trim().toLowerCase();
    const allowed = new Set(
      [...this.allowed].map((entry) => entry.toLowerCase()),
    );
    if (!target)
      throw new OutboundError(
        "an outbound call needs the person's directory id",
      );
    if (this.allowed.size === 0) {
      throw new OutboundError(
        "outbound calling is off: set STANDIN_OUTBOUND_ALLOW to the directory ids " +
          "this agent may ring",
      );
    }
    if (!allowed.has(target)) {
      throw new OutboundError(
        "that person is not on this agent's outbound allowlist",
      );
    }
    if (this.maxPerHour) {
      const cutoff = Date.now() - 3_600_000;
      this.#placed = this.#placed.filter((t) => t > cutoff);
      if (this.#placed.length >= this.maxPerHour) {
        throw new OutboundError(
          `this agent has already placed ${this.maxPerHour} calls in the last hour`,
        );
      }
    }
  }

  /** Count a placed call against the hourly cap. */
  record(): void {
    this.#placed.push(Date.now());
  }
}

// ------------------------------------------------------------------ the lane

/** How long a call may ring before nobody is going to answer it. */
export const DEFAULT_ANSWER_TIMEOUT_MS = 120_000;

/** How often the sweep looks for calls nobody answered. */
export const DEFAULT_SWEEP_INTERVAL_MS = 30_000;

/** After this, an undelivered answer is too old to be worth sending. */
export const DEFAULT_PENDING_TTL_MS = 3_600_000;

/** A reservation older than this belonged to a worker that died holding it. */
export const RESERVATION_STALE_MS = 600_000;

/**
 * The answering leg can attach before the message has been parked, so attach
 * waits a little rather than deciding there is nothing to say.
 */
export const PARK_GRACE_MS = 5_000;
export const PARK_POLL_MS = 250;

/** The same race on the outcome path. */
export const OUTCOME_GRACE_MS = 5_000;

/** How long after somebody's chat message the agent may ring them back. */
export const CHAT_CALLBACK_WINDOW_MS = 600_000;

/** What a model may park. It is read out loud on answer. */
export const MAX_PENDING_TEXT_CHARS = 4000;

/** How many times a failing chat delivery is retried before it is dropped. */
export const MAX_DELIVERY_ATTEMPTS = 5;

/** Outcomes that mean nobody took the call. */
export const UNANSWERED_OUTCOMES: ReadonlySet<string> = new Set([
  "no-answer",
  "declined",
  "busy",
  "failed",
]);

/**
 * What to say in chat for each of them. Written for the person who missed the
 * call, not for an operator reading a log.
 */
export const OUTCOME_WORDING: Record<string, string> = {
  "no-answer": "I tried to call you but couldn't reach you.",
  declined: "You declined my call, no problem.",
  busy: "I tried to call you but the line was busy.",
  failed: "I tried to call you but the call could not be completed.",
};

/** Marks the fallback so it reads as a missed call rather than a stray message. */
export const NO_ANSWER_PREFIX = "\u{1F4DE} ";

/**
 * Whether a live call has a chat its answer could go to instead.
 *
 * A one-to-one call has no meeting conversation, and the field then carries
 * something that is not one. Posting to it would either fail or reach the wrong
 * place, so a call without a real thread is parked with no fallback.
 */
export function callThreadIsPostable(
  threadId: string,
  callId: string,
): boolean {
  const thread = (threadId ?? "").trim();
  return thread !== "" && thread.startsWith("19:") && thread !== callId;
}

/** Who to ring, resolved from a chat message rather than from a model. */
export interface ChatCallbackTarget {
  readonly userObjectId: string;
  readonly tenantId: string;
  readonly conversationId: string;
  readonly displayName: string;
}

/** Whatever can post into a conversation. `ChatChannel` satisfies it. */
export interface ChatSender {
  send(options: {
    tenantId: string;
    conversationId: string;
    text: string;
    idempotencyKey?: string;
  }): Promise<boolean>;
}

/**
 * Say the parked line. The plugin owns the wording, because only it knows
 * whether its provider takes an instruction or a literal line of speech.
 */
export type Speak = (message: PendingMessage) => Promise<void>;

/** What a session has to offer for the lane to attach to it. */
export interface OutboundSession {
  readonly callId: string;
  readonly start: { readonly direction: string };
  readonly recordingActive: boolean;
  end(reason: string): Promise<void>;
}

export const CHAT_CALLBACK_TOOL: ToolSpec = {
  name: "call_me_with_the_answer",
  description:
    "Ring the person you are talking to and tell them the answer out loud, instead of " +
    "replying here. Use it when they ask you to call them, or when the answer is easier " +
    "said than written.",
  parameters: {
    message: { type: "string", description: "What to say when they answer." },
  },
  required: ["message"],
};

export const CALL_BACK_TOOL: ToolSpec = {
  name: "call_me_back",
  description:
    "Ring this caller again later and say something. Use it when the work will not finish " +
    "while they are on the line and they asked to be called rather than messaged.",
  parameters: {
    message: { type: "string", description: "What to say when they answer." },
  },
  required: ["message"],
};

interface ChatSenderRecord {
  userObjectId: string;
  tenantId: string;
  displayName: string;
  atMs: number;
}

/**
 * One answering leg, holding the message until somebody actually answers.
 *
 * Built by {@link OutboundLane.attach} from `onStart`. The plugin forwards two
 * things and the leg does the rest.
 */
export class OutboundLeg {
  readonly #lane: OutboundLane;
  readonly #session: OutboundSession;
  readonly #speak: Speak;
  readonly #answerTimeoutMs: number;
  #message: PendingMessage | undefined;
  #spoken = false;
  #closed = false;
  #watchdog: NodeJS.Timeout | undefined;

  constructor(
    lane: OutboundLane,
    session: OutboundSession,
    message: PendingMessage | undefined,
    speak: Speak,
    answerTimeoutMs: number,
  ) {
    this.#lane = lane;
    this.#session = session;
    this.#message = message;
    this.#speak = speak;
    this.#answerTimeoutMs = answerTimeoutMs;
  }

  /** What is waiting to be said, if anything. */
  get message(): PendingMessage | undefined {
    return this.#message;
  }

  /** @internal Used by the lane when a late-arriving record is found. */
  setMessage(message: PendingMessage): void {
    this.#message = message;
  }

  /** Start watching. Called by the lane once the record is settled. */
  arm(): void {
    if (this.#message === undefined || this.#closed) return;
    this.#watchdog = setTimeout(
      () => void this.#giveUp(),
      this.#answerTimeoutMs,
    );
    this.#watchdog.unref?.();
    // Answered before we even looked: a fast pickup beats the attach.
    if (this.#session.recordingActive) void this.#deliver();
  }

  /**
   * Forward every `onContext`. Recording going active is the answer.
   *
   * There is no "they picked up" message on the wire. Recording turning on is
   * what happens when a Microsoft Teams call is actually connected, so that
   * transition is the signal, and the plugin already receives it.
   */
  async onContext(): Promise<void> {
    if (this.#session.recordingActive) await this.#deliver();
  }

  /** Say it now. For a plugin with a better signal than the recording. */
  async answered(): Promise<void> {
    await this.#deliver();
  }

  /** The leg is over. Anything unsaid goes back for the sweep. */
  async aclose(reason = "call-ended"): Promise<void> {
    if (this.#closed) return;
    this.#closed = true;
    clearTimeout(this.#watchdog);
    if (this.#message !== undefined && !this.#spoken) {
      // Released, not dropped: nobody heard it, so it still has to reach them
      // somehow.
      this.#lane.pending.release(this.#message.callId);
      logger.info(
        `standin: outbound call ${safeName(this.#message.callId)} ended unanswered ` +
          `(${reason}); the answer goes to chat`,
      );
    }
  }

  async #deliver(): Promise<void> {
    if (this.#spoken || this.#closed || this.#message === undefined) return;
    this.#spoken = true;
    clearTimeout(this.#watchdog);
    try {
      await this.#speak(this.#message);
    } catch (err) {
      // Saying it failed, so it was not said. Put it back rather than
      // pretending the person was told.
      this.#spoken = false;
      logger.error(
        `standin: speaking the parked message failed: ${String(err)}`,
      );
      return;
    }
    this.#lane.pending.commit(this.#message.callId);
    this.#lane.finalized.add(this.#message.callId);
  }

  /**
   * End a leg that rings for ever.
   *
   * The idle watchdog cannot do this: a ringing leg carries no caller audio by
   * definition, so to that watchdog every outbound call looks dead.
   */
  async #giveUp(): Promise<void> {
    if (this.#spoken || this.#closed) return;
    logger.info(
      `standin: nobody answered outbound call ${safeName(this.#session.callId)} ` +
        `within ${this.#answerTimeoutMs}ms`,
    );
    await this.#session.end("outbound-no-answer");
  }
}

/** Options for {@link OutboundLane}. */
export interface OutboundLaneOptions {
  caller?: OutboundCaller;
  policy?: OutboundPolicy;
  pending?: PendingMessages;
  chat?: ChatSender;
  tenantId?: string;
  answerTimeoutMs?: number;
  sweepIntervalMs?: number;
  ttlMs?: number;
  maxInFlightPerTarget?: number;
}

/**
 * Placing a call, saying the thing, and what to do when nobody answers.
 *
 * The three are one capability, and splitting them is how the answer gets lost.
 * A call is placed because somebody is owed something; if they do not pick up,
 * they are still owed it.
 *
 * Everything durable is on disk, so a restart between the ring and the answer
 * loses nothing.
 */
export class OutboundLane {
  readonly #caller: OutboundCaller | undefined;
  readonly #policy: OutboundPolicy;
  readonly pending: PendingMessages;
  readonly #chat: ChatSender | undefined;
  readonly #tenantId: string;
  readonly #answerTimeoutMs: number;
  readonly #sweepIntervalMs: number;
  readonly #ttlMs: number;
  readonly #maxInFlight: number;
  readonly #senders = new Map<string, ChatSenderRecord>();
  readonly finalized = new Set<string>();
  #sweeper: NodeJS.Timeout | undefined;

  constructor(options: OutboundLaneOptions = {}) {
    this.#caller = options.caller;
    this.#policy = options.policy ?? OutboundPolicy.fromEnv();
    this.pending = options.pending ?? new PendingMessages();
    this.#chat = options.chat;
    this.#tenantId = options.tenantId ?? "";
    this.#answerTimeoutMs =
      options.answerTimeoutMs ?? DEFAULT_ANSWER_TIMEOUT_MS;
    this.#sweepIntervalMs =
      options.sweepIntervalMs ?? DEFAULT_SWEEP_INTERVAL_MS;
    this.#ttlMs = options.ttlMs ?? DEFAULT_PENDING_TTL_MS;
    this.#maxInFlight = Math.max(1, options.maxInFlightPerTarget ?? 1);
  }

  /**
   * Ring somebody and park what to say. Throws {@link OutboundError}.
   *
   * Everything that can be refused is refused BEFORE the call is placed, so a
   * refusal never leaves somebody's phone ringing for a message that was never
   * going to be sent.
   */
  async place(options: {
    userObjectId: string;
    text: string;
    tenantId?: string;
    threadId?: string;
    requestedBy?: string;
    metadata?: Record<string, unknown>;
  }): Promise<PlacedCall> {
    const line = (options.text ?? "").trim();
    if (line === "")
      throw new OutboundError("there was nothing to say, so I did not call");
    if (line.length > MAX_PENDING_TEXT_CHARS) {
      throw new OutboundError(
        `that message is too long to deliver by phone (${line.length} characters)`,
      );
    }
    this.#policy.check(options.userObjectId);

    const target = options.userObjectId.trim().toLowerCase();
    const inFlight = this.pending
      .waiting()
      .filter((m) => m.target === target).length;
    if (inFlight >= this.#maxInFlight) {
      throw new OutboundError(
        "I am already calling that person about something else",
      );
    }
    if (this.#caller === undefined) {
      throw new OutboundError("this worker is not set up to place calls");
    }

    const tenantId = (options.tenantId ?? this.#tenantId).trim();
    const placed = await this.#caller.placeCall({
      userObjectId: options.userObjectId.trim(),
      tenantId,
    });
    this.#policy.record();

    // Only a real conversation. A call that has none is parked with no fallback
    // rather than one that would fail or reach the wrong place.
    const threadId = callThreadIsPostable(options.threadId ?? "", placed.callId)
      ? (options.threadId ?? "")
      : "";
    this.pending.park({
      callId: placed.callId,
      text: line,
      threadId,
      requestedBy: options.requestedBy ?? "",
      createdMs: nowMs(),
      metadata: options.metadata ?? {},
      tenantId,
      target,
    });
    // One audit line, and never the text: it is somebody's message.
    logger.info(
      `standin: placed an outbound call to ${safeName(target)} (call ${safeName(placed.callId)}, ` +
        `chat fallback ${threadId ? "yes" : "no"}, asked by ${safeName(options.requestedBy || "unknown")})`,
    );
    return placed;
  }

  /**
   * Bind an answering leg to whatever was parked for it.
   *
   * Returns undefined on an inbound call, so a plugin can call it
   * unconditionally from `onStart`.
   */
  attach(session: OutboundSession, speak: Speak): OutboundLeg | undefined {
    if (session.start.direction !== "outbound") return undefined;
    const held = this.pending.reserve(session.callId);
    const leg = new OutboundLeg(
      this,
      session,
      held,
      speak,
      this.#answerTimeoutMs,
    );
    if (held !== undefined) {
      leg.arm();
    } else {
      // The leg can be answered before place() has finished parking, so waiting
      // a moment beats deciding there is nothing to say. Not awaited: onStart
      // must not block the frame loop.
      void this.#attachLater(session, leg);
    }
    return leg;
  }

  async #attachLater(
    session: OutboundSession,
    leg: OutboundLeg,
  ): Promise<void> {
    const deadline = Date.now() + PARK_GRACE_MS;
    while (Date.now() < deadline) {
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, PARK_POLL_MS);
        timer.unref?.();
      });
      const held = this.pending.reserve(session.callId);
      if (held !== undefined) {
        leg.setMessage(held);
        leg.arm();
        return;
      }
    }
    // Already delivered or already given up on. Not a fresh call.
    if (this.finalized.has(session.callId))
      await session.end("outbound-expired");
  }

  /**
   * What StandIn reports when an outbound call ended without an answer.
   *
   * Pass it to `new CallServer({ onCallOutcome })`. An outcome this does not
   * recognise is logged and ignored: an unknown word is not a failure, and
   * treating it as one would post "I could not reach you" to somebody who
   * answered.
   */
  async onOutcome(callId: string, outcome: string): Promise<boolean> {
    const state = (outcome ?? "").trim().toLowerCase();
    if (state === "answered") return true;
    if (!UNANSWERED_OUTCOMES.has(state)) {
      logger.info(
        `standin: ignoring an outbound outcome this SDK does not know: ${state}`,
      );
      return true;
    }
    if (this.finalized.has(callId)) {
      // The sweep already told them. Waiting out the grace for a record that is
      // gone delays nothing and helps nobody.
      return true;
    }

    const deadline = Date.now() + OUTCOME_GRACE_MS;
    for (;;) {
      const held = this.pending.pop(callId);
      if (held !== undefined) return this.#deliverToChat(held, state);
      if (Date.now() >= deadline) return false;
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, PARK_POLL_MS);
        timer.unref?.();
      });
    }
  }

  /** Deliver what nobody answered. Returns how many went out. */
  async sweep(): Promise<number> {
    this.pending.recoverReservations(RESERVATION_STALE_MS);
    let delivered = 0;
    for (const held of this.pending.claimStale(this.#answerTimeoutMs)) {
      if (await this.#deliverToChat(held, "no-answer")) delivered += 1;
      if (this.#caller !== undefined && held.callId) {
        // Fire and forget. A ring nobody will answer should stop, but a failure
        // to stop it must not lose the message.
        try {
          await this.#caller.cancelCall(held.callId);
        } catch {
          // Already gone, or unreachable. Either way the message is delivered.
        }
      }
    }
    return delivered;
  }

  async #deliverToChat(
    held: PendingMessage,
    outcome: string,
  ): Promise<boolean> {
    this.finalized.add(held.callId);
    if (!held.threadId || this.#chat === undefined) {
      logger.warn(
        `standin: outbound call ${safeName(held.callId)} went unanswered and there is ` +
          "no chat to tell them",
      );
      return false;
    }
    const body = `${NO_ANSWER_PREFIX}${OUTCOME_WORDING[outcome]} Here's what I had: ${held.text}`;
    let sent = false;
    try {
      sent = await this.#chat.send({
        tenantId: held.tenantId || this.#tenantId,
        conversationId: held.threadId,
        text: body,
        // The timer and the outcome can both fire for one call. The same key
        // means the person is told once.
        idempotencyKey: `standin-noanswer-${held.callId}`,
      });
    } catch (err) {
      logger.warn(
        `standin: posting an unanswered call's message failed: ${String(err)}`,
      );
    }
    if (sent) return true;
    this.#requeue(held);
    return false;
  }

  /** Put a failed delivery back, or give up loudly. */
  #requeue(held: PendingMessage): void {
    const attempts = (held.attempts ?? 0) + 1;
    const ageMs = held.createdMs ? nowMs() - held.createdMs : 0;
    if (attempts >= MAX_DELIVERY_ATTEMPTS || ageMs > this.#ttlMs) {
      logger.error(
        `standin: giving up on delivering outbound call ${safeName(held.callId)} ` +
          `after ${attempts} attempts`,
      );
      return;
    }
    this.finalized.delete(held.callId);
    this.pending.park({ ...held, attempts });
  }

  /**
   * Record who last wrote in this conversation, from the message itself.
   *
   * The ONLY place a callback target comes from. Never from message text, and
   * never from a tool parameter: an agent that can be told who to ring can be
   * told to ring anybody.
   */
  rememberChatSender(message: {
    conversationId: string;
    tenantId: string;
    senderAadId?: string;
    senderName?: string;
  }): void {
    if (!message.senderAadId) return;
    this.#senders.set(message.conversationId, {
      userObjectId: message.senderAadId,
      tenantId: message.tenantId,
      displayName: message.senderName ?? "",
      atMs: nowMs(),
    });
  }

  /**
   * Who to ring for this conversation, or a sentence saying why not.
   *
   * A sentence rather than an exception: the caller is a tool result that a
   * model reads out loud.
   */
  chatCallbackTarget(conversationId: string): ChatCallbackTarget | string {
    const record = this.#senders.get(conversationId);
    if (record === undefined)
      return "I do not know who to call for this conversation.";
    if (nowMs() - record.atMs > CHAT_CALLBACK_WINDOW_MS) {
      return "That was a while ago. Ask me again and I can call you.";
    }
    return {
      userObjectId: record.userObjectId,
      tenantId: record.tenantId,
      conversationId,
      displayName: record.displayName,
    };
  }

  /** Begin sweeping. Idempotent. */
  start(): void {
    if (this.#sweeper !== undefined) return;
    this.#sweeper = setInterval(() => {
      void this.sweep().catch((err: unknown) => {
        logger.error(`standin: the outbound sweep failed: ${String(err)}`);
      });
    }, this.#sweepIntervalMs);
    this.#sweeper.unref?.();
  }

  /** Stop sweeping. Anything parked stays parked. */
  async aclose(): Promise<void> {
    clearInterval(this.#sweeper);
    this.#sweeper = undefined;
  }
}
