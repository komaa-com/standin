// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The call listener every StandIn plugin shares.
 *
 * StandIn dials `wss://<your-host>/msteams/calling/{callId}` once
 * per call. This server answers that dial, authenticates it, speaks the call
 * wire protocol, and hands each call to a {@link CallHandler} supplied by a
 * plugin. Everything here is the same whichever agent framework is on the other
 * side, which is exactly why it lives in the SDK and not in the plugins:
 *
 * - the HMAC handshake, its freshness window and its single-use replay guard
 * - capacity, draining, and the one-live-session-per-callId rule
 * - the frame loop
 * - outbound sequence numbers and the audio timeline
 * - the pre-start watchdog and the caller-audio idle watchdog
 * - idempotent teardown that always frees the slot
 *
 * The Python SDK's `standin/sdk/call_server.py` is the same server, method for
 * method. Behavioural parity is asserted by the shared conformance vectors.
 */

import {
  createServer,
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from "node:http";
import type { Duplex } from "node:stream";

import { WebSocket, WebSocketServer } from "ws";

import {
  expression as buildExpression,
  speechMarks as buildSpeechMarks,
  type Emotion,
  type SpeechMark,
} from "./avatar.js";
import { StandInError } from "./errors.js";
import type { CallHandler, CallSession, HandlerFactory } from "./handler.js";
import { logger } from "./log.js";
import {
  REPLAY_WINDOW_MS,
  SIGNATURE_HEADER,
  SIGNATURE_V2_HEADER,
  TIMESTAMP_HEADER,
  nowMs,
  signRequest,
  verifyHandshake,
} from "./hmac.js";
import {
  SAMPLE_RATE_HZ,
  type SessionStart,
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
  displayFrame as buildDisplayFrame,
  displayImage as buildDisplayImage,
  parseVideoFrame,
  type DisplayImageOptions,
  type VideoFrame,
  type VideoSource,
} from "./vision.js";

/** How often the single-use handshake cache is swept for expired entries. */
const PRUNE_INTERVAL_MS = 1_000;

/**
 * Teardown must never be held hostage by a peer that stopped reading. Until the
 * close settles the callId is still occupied - so every retry for that call
 * 409s, and at maxConnections the whole listener stops accepting. The advisory
 * `session.end` is written before we wait, so a peer that is still listening has
 * what it needs either way.
 */
const CLOSE_TIMEOUT_MS = 2_000;

/** 2 MB bounds a single inbound message, matching the Python SDK. */
const MAX_PAYLOAD_BYTES = 2 * 1024 * 1024;

/**
 * An outcome report is a handful of JSON fields. The cap matters because the
 * body must be read before the signature over its hash can be checked, so this
 * is the bound on what an unauthenticated peer can make the worker read.
 */
const MAX_OUTCOME_BYTES = 8 * 1024;

/** Constant-time compare of two lowercase hex digests of the same length. */
function timingSafeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1)
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Log-safe rendering of an attacker-influenceable id: control characters (CR/LF
 * forge log lines) replaced, length bounded.
 */
function safe(value: string): string {
  // eslint-disable-next-line no-control-regex
  return value.replace(/[^\x20-\x7e]/g, "?").slice(0, 80);
}

/**
 * Call one optional handler method. A {@link CallHandler} is an interface with
 * every member optional, so a plugin implements only what it cares about and a
 * missing method is a no-op. Sync implementations are accepted too.
 */
async function dispatch<K extends keyof CallHandler>(
  handler: CallHandler,
  method: K,
  ...args: Parameters<NonNullable<CallHandler[K]>>
): Promise<void> {
  const fn = handler[method] as ((...a: unknown[]) => unknown) | undefined;
  if (typeof fn !== "function") return;
  await fn.apply(handler, args as unknown[]);
}

/**
 * Outbound bytes past which agent audio is dropped rather than queued.
 *
 * A slow or wedged peer turns "send everything" into an unbounded queue, and
 * that queue is what stalls the provider receive loop feeding it. Shedding keeps
 * the loop moving: the caller hears a gap rather than the call wedging.
 *
 * Audio only. Control frames are what END a call, and a call that cannot be
 * ended is the failure this exists to prevent.
 */
export const MAX_AUDIO_BUFFER_BYTES = 1024 * 1024;

/**
 * How long a call may go without an agent ever answering it.
 *
 * The one gap none of the other watchdogs cover. `preStartTimeoutMs` watches for
 * session.start, which arrived. `onStartTimeoutMs` bounds onStart, which
 * succeeded. `audioIdleTimeoutMs` is satisfied because the CALLER is still
 * talking. So StandIn is on the call, the caller hears nothing, and nothing ends
 * it.
 */
export const STALE_CALL_REAPER_MS = 120_000;

/**
 * How often to look. Coarse on purpose: this is a grace period, not something
 * anybody measures to the second.
 */
export const REAPER_CHECK_INTERVAL_MS = 15_000;
export const REAPER_MIN_INTERVAL_MS = 50;

/** Milliseconds from a clock that cannot step backwards. */
function monotonicMs(): number {
  return Number(process.hrtime.bigint() / 1_000_000n);
}

/**
 * Whether this call has run past its grace with nothing answering it.
 *
 * Pure, with the clock passed in, so the boundary is testable without waiting.
 * Strictly greater, so a tick landing exactly on the grace does not reap a call
 * one instant early.
 */
export function isUnanswered(
  call: { startedAtMs: number; answeredAtMs?: number },
  staleMs: number,
  now: number,
): boolean {
  return (
    call.answeredAtMs === undefined &&
    staleMs > 0 &&
    now - call.startedAtMs > staleMs
  );
}

/** One live call: the StandIn socket on one side, a plugin's handler on the other. */
class Call implements CallSession {
  #server: CallServer;
  #callId: string;
  #ws: WebSocket;
  #handler: CallHandler | undefined;
  #start: SessionStart | undefined;
  #seq = 0;
  #sentMs = 0;
  // The tile stream has its own sequence: it is a separate stream from the
  // audio, and a receiver drops out-of-order frames per stream.
  #tileSeq = 0;
  #lastAudio: number | undefined;
  // One recording flag, kept current here so no plugin has to re-derive it
  // from the context sentence it happened to see.
  #recording = false;
  // Whether a recording.status frame has actually said so. session.start OMITS
  // the field when the state was unknown at answer time, and an omitted field is
  // not "not recording": a recording.status that lands first would otherwise be
  // overwritten by the absent snapshot, and every recording-gated capability
  // would stay shut for the rest of the call with nothing said.
  #recordingReported = false;
  // The active speaker, when StandIn sends unmixed audio. Undefined on the
  // mixed path, which is most calls.
  #speaker: string | undefined;
  #participants = 0;
  #audioDropped = 0;
  #lastDropLog = 0;
  // Monotonic, not wall clock: a clock step forward larger than the grace
  // period would otherwise reap every live unanswered call at once.
  readonly startedAtMs = monotonicMs();
  answeredAtMs: number | undefined;
  // Latest frame per source, and only the latest: frames arrive sparsely and a
  // held history would be an unbounded buffer of the caller's screen. Never
  // written to disk.
  #frames = new Map<VideoSource, VideoFrame>();
  #closed = false;
  /** Set only AFTER onStart resolves; the pre-start watchdog keys on this. */
  #started = false;
  #inStart = false;
  #queue: Promise<void> = Promise.resolve();
  #closingReason = "call-ended";
  #closePromise: Promise<void> | undefined;
  #idleTimer: NodeJS.Timeout | undefined;
  #durationTimer: NodeJS.Timeout | undefined;
  #preStartTimer: NodeJS.Timeout | undefined;

  constructor(server: CallServer, callId: string, ws: WebSocket) {
    this.#server = server;
    this.#callId = callId;
    this.#ws = ws;
  }

  // ---- the CallSession surface handed to the plugin ----

  get callId(): string {
    return this.#callId;
  }

  get start(): SessionStart {
    if (this.#start === undefined)
      throw new StandInError("the call has not started yet");
    return this.#start;
  }

  /**
   * Send agent audio to the caller. The server owns `seq` and the timeline, so a
   * handler that swaps or re-publishes its audio source cannot make
   * `timestampMs` jump backwards while `seq` keeps climbing.
   */
  /** Whether this call is being recorded, right now. */
  get recordingActive(): boolean {
    return this.#recording;
  }

  /** Whether anything has actually taken this call yet. */
  get answered(): boolean {
    return this.answeredAtMs !== undefined;
  }

  /**
   * Say that an agent has taken the call. Stamped once, never re-stamped.
   *
   * A plugin that joins a room calls this when the agent's own audio track
   * appears, not when a participant connects: monitors, recorders and avatar
   * workers all connect, and none of them is an agent answering.
   *
   * A plugin that never calls it is still covered, because sending audio counts.
   * The explicit call exists for an agent that joins and listens before it says
   * anything.
   */
  markAnswered(): void {
    this.answeredAtMs ??= monotonicMs();
  }

  /** Who is speaking, when StandIn sends unmixed audio. */
  get speaker(): string | undefined {
    return this.#speaker;
  }

  /** How many people are on the call. Zero until StandIn says. */
  get participantCount(): number {
    return this.#participants;
  }

  /** Outbound bytes the socket has not flushed yet, straight off the socket. */
  get bufferedBytes(): number {
    return this.#ws.bufferedAmount;
  }

  /** The outbound audio timeline this call stamps its audio with. */
  get mediaTimeMs(): number {
    return this.#sentMs;
  }

  async sendAudio(pcm: Buffer): Promise<void> {
    // Gated on the SOCKET, not on #closed: aclose sets #closed BEFORE teardown
    // dispatches the handler's aclose, so gating on the flag would silently
    // break the guarantee that a handler can still speak on the way out.
    if (this.#ws.readyState !== WebSocket.OPEN || pcm.length === 0) return;
    // Sending audio IS answering, so every plugin is covered by the reaper
    // without doing anything.
    this.markAnswered();
    const timestampMs = this.#sentMs;
    // The timeline advances whether or not this frame goes out. It is the
    // CALLER's clock: a dropped frame is a gap in what they hear, not a rewind,
    // and stalling the clock would make every later frame claim a time that has
    // already passed.
    this.#seq += 1;
    // Integer division at every step, matching Python's
    // `(len(pcm) // 2) * 1000 // SAMPLE_RATE_HZ` exactly. A float here would
    // drift the two SDKs' timelines apart over a long call.
    this.#sentMs += Math.floor(
      (Math.floor(pcm.length / 2) * 1000) / SAMPLE_RATE_HZ,
    );
    if (this.#overAudioBudget()) return;
    this.#send(audioFrame(this.#seq, timestampMs, pcm));
  }

  /** Whether the socket is too far behind to take another audio frame. */
  #overAudioBudget(): boolean {
    if (this.bufferedBytes <= MAX_AUDIO_BUFFER_BYTES) return false;
    this.#audioDropped += 1;
    const now = Date.now();
    if (now - this.#lastDropLog >= 5_000) {
      logger.warn(
        `standin: call ${safe(this.#callId)} is shedding agent audio to keep the loop ` +
          `moving (${this.#audioDropped} frames so far); the peer is not reading`,
      );
      this.#lastDropLog = now;
    }
    return true;
  }

  /**
   * Drop whatever agent audio StandIn still has buffered.
   *
   * The only lever that un-sends audio already handed to the service: it
   * flushes the platform player, so the caller stops hearing the turn they just
   * interrupted. Without it a barge-in stops the MODEL but the bot keeps talking
   * for the length of the buffered PCM.
   *
   * Call it the moment your provider reports the caller started speaking, before
   * you cancel the response upstream.
   */
  async cancelPlayback(): Promise<void> {
    this.#send(assistantCancel(this.#seq));
  }

  /**
   * Ask for the call to end.
   *
   * Awaiting teardown from INSIDE onStart would deadlock: teardown waits for
   * onStart to return before dispatching the handler's aclose, and onStart
   * would be waiting for teardown. Refusing a call is a normal thing to do from
   * onStart - an allowlist rejection, a busy runtime, a provider that will not
   * connect - so it is made safe here: ask for the close, return immediately,
   * and let it run once onStart unwinds.
   */
  latestVideoFrame(source?: VideoSource): VideoFrame | undefined {
    if (source !== undefined) return this.#frames.get(source);
    return this.#frames.get("screenshare") ?? this.#frames.get("camera");
  }

  /**
   * Draw an image on the bot's video tile.
   *
   * Gated on the socket for the same reason sendAudio is: a handler is allowed
   * to show something on its way out.
   */
  /**
   * Send one frame of continuous avatar video.
   *
   * Gated on the socket, like sendAudio: a handler may still be showing
   * something on its way out.
   */
  async sendTileFrame(
    jpeg: Buffer,
    width?: number,
    height?: number,
  ): Promise<void> {
    if (this.#ws.readyState !== this.#ws.OPEN || jpeg.length === 0) return;
    this.#tileSeq += 1;
    this.#send(
      buildDisplayFrame(this.#tileSeq, this.#sentMs, jpeg, {
        mime: "image/jpeg",
        width,
        height,
      }),
    );
  }

  async displayImage(
    image: Buffer | string,
    options: DisplayImageOptions = {},
  ): Promise<void> {
    if (this.#ws.readyState !== this.#ws.OPEN) return;
    this.#send(buildDisplayImage(image, options));
  }

  /** Hint the avatar's emotion. Video only, and never fatal. */
  async express(emotion: Emotion): Promise<void> {
    if (this.#ws.readyState !== this.#ws.OPEN) return;
    this.#send(buildExpression(emotion));
  }

  /** Send one utterance's viseme timeline for avatar lip-sync. */
  async sendSpeechMarks(marks: Iterable<SpeechMark>): Promise<void> {
    if (this.#ws.readyState !== this.#ws.OPEN) return;
    this.#send(buildSpeechMarks(marks));
  }

  async end(reason: string): Promise<void> {
    if (this.#inStart) {
      this.#closingReason =
        this.#closePromise === undefined ? reason : this.#closingReason;
      if (this.#closePromise === undefined) {
        this.#closed = true;
        this.#closePromise = this.#teardown();
      }
      return;
    }
    await this.aclose(reason);
  }

  // ---- lifecycle ----

  /** Wire the socket up. Resolves when the call has fully ended. */
  run(): Promise<void> {
    this.#preStartTimer = setTimeout(() => {
      if (!this.#started && !this.#closed) {
        logger.warn(
          `standin: call ${safe(this.#callId)} never sent session.start`,
        );
        void this.aclose("pre-start-timeout");
      }
    }, this.#server.preStartTimeoutMs);
    // Never keep the process alive for a watchdog.
    this.#preStartTimer.unref?.();

    // SERIALIZED, matching Python's `async for msg in self._ws`. Unawaited,
    // any audio.frame packed into the same TCP read as session.start is
    // dispatched while #onSessionStart is still awaiting onStart - so the
    // handler sees audio before it is built, which handler.ts documents as
    // impossible. Python honours it by awaiting serially; without this queue the
    // two SDKs disagree on the one ordering guarantee the seam makes.
    this.#ws.on("message", (data: Buffer, isBinary: boolean) => {
      if (isBinary) return;
      this.#queue = this.#queue
        .then(() => this.#onFrame(data))
        .catch(() => undefined);
    });
    this.#ws.on("error", (err) => {
      logger.error(
        `standin: call ${safe(this.#callId)} socket error: ${String(err)}`,
      );
      void this.aclose("transport-failure");
    });
    this.#ws.on("close", () => {
      void this.aclose();
    });

    return new Promise<void>((resolve) => {
      this.#onEnded = resolve;
    });
  }

  #onEnded: (() => void) | undefined;

  async #onFrame(data: Buffer): Promise<void> {
    const frame = parseMessage(data);
    if (frame === undefined) return;
    const kind = frame.type as string;

    try {
      if (kind === "session.start") {
        if (this.#start !== undefined) return; // a second start is a sender bug
        await this.#onSessionStart(parseSessionStart(frame));
      } else if (kind === "audio.frame") {
        this.#lastAudio = Date.now();
        await this.#onCallerAudio(frame);
      } else if (kind === "video.frame") {
        await this.#onVideoFrame(frame);
      } else if (kind === "ping") {
        this.#send(pong(frame.ts));
      } else if (kind === "participants") {
        // The same sentences the Python SDK publishes, so agents written against
        // either read identical context.
        if (typeof frame.count === "number") {
          this.#participants = Math.max(0, Math.trunc(frame.count));
          await this.#onContext(contextSentences.participants(frame.count));
        }
      } else if (kind === "dtmf") {
        if (typeof frame.digit === "string" && frame.digit !== "") {
          await this.#onContext(contextSentences.dtmf(frame.digit));
        }
      } else if (kind === "recording.status") {
        if (typeof frame.status === "string") {
          this.#recording = frame.status === "active";
          this.#recordingReported = true;
          await this.#onContext(contextSentences.recording(frame.status));
        }
      } else if (kind === "assistant.say") {
        const text = frame.text;
        if (typeof text === "string" && text.trim() !== "") {
          // Flush the worker's queued agent playback FIRST: without the cancel,
          // the goodbye publishes behind seconds of already-buffered audio and
          // the call is torn down before it plays.
          this.#send(assistantCancel(this.#seq));
          await this.#guard("onGoodbye", text);
        }
      } else if (kind === "session.end") {
        const reason =
          typeof frame.reason === "string" && frame.reason
            ? frame.reason
            : "call-ended";
        await this.aclose(reason);
      }
      // Anything else (the avatar surface included) is ignored by contract, so an
      // older plugin and a newer StandIn interoperate.
    } catch (err) {
      logger.error(
        `standin: call ${safe(this.#callId)} failed: ${String(err)}`,
      );
      await this.aclose("transport-failure");
    }
  }

  async #onSessionStart(start: SessionStart): Promise<void> {
    if (start.callId !== this.#callId) {
      // The URL path is what the HMAC signed. A body that disagrees is either a
      // bug or an attempt to ride one call's signature into another's session.
      throw new StandInError(
        `session.start callId ${JSON.stringify(start.callId)} does not match the authenticated path`,
      );
    }
    clearTimeout(this.#preStartTimer);
    this.#start = start;
    // Only when nothing has reported the real state yet. recording.status can
    // land before session.start, and the snapshot is omitted when the state was
    // unknown at answer time, so seeding unconditionally turns a live ACTIVE
    // into false for the whole call.
    if (!this.#recordingReported)
      this.#recording = start.recordingStatus === "active";
    this.#handler = this.#server.buildHandler();
    this.#lastAudio = Date.now();

    // Armed BEFORE onStart, not after: onStart does real network work, and while
    // it is awaited the frame loop is queued behind it, so session.end is never
    // read. Armed after, a hung onStart has no watchdog at all and the callId
    // 409s forever - one leaked slot per inbound call, up to maxConnections.
    this.#armIdleWatchdog();
    this.#armDurationCeiling();

    this.#inStart = true;
    try {
      await this.#guardStart();
    } finally {
      this.#inStart = false;
    }
    this.#started = true;

    logger.info(
      `standin: call ${safe(this.#callId)} started (${start.direction}, caller ` +
        `${safe(start.caller.displayName ?? "unknown")})`,
    );
  }

  /** onStart with its own timeout and its own close reason. */
  async #guardStart(): Promise<void> {
    const handler = this.#handler;
    if (handler === undefined) return;
    const timeout = this.#server.onStartTimeoutMs;
    try {
      if (timeout > 0) {
        let timer: NodeJS.Timeout | undefined;
        const expiry = new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error("on-start-timeout")),
            timeout,
          );
          timer.unref?.();
        });
        try {
          await Promise.race([dispatch(handler, "onStart", this), expiry]);
        } finally {
          clearTimeout(timer);
        }
      } else {
        await dispatch(handler, "onStart", this);
      }
    } catch (err) {
      const timedOut = String(err).includes("on-start-timeout");
      logger.error(
        `standin: handler.onStart ${timedOut ? "timed out" : "failed"} on call ` +
          `${safe(this.#callId)}: ${String(err)}`,
      );
      // A third-party outage inside a plugin must not be reported to StandIn as
      // StandIn's own socket failing - that sends both sides debugging the wrong
      // system.
      await this.aclose(
        timedOut ? "handler-start-timeout" : "handler-start-failure",
      );
    }
  }

  async #onCallerAudio(frame: Record<string, unknown>): Promise<void> {
    if (this.#handler === undefined || this.#closed) return;
    let pcm: Buffer;
    try {
      pcm = decodePcm(frame.payloadBase64);
    } catch (err) {
      logger.warn(`standin: dropping caller frame: ${String(err)}`);
      return;
    }
    await this.#noteSpeaker(frame.speakerName);
    await this.#guard("onCallerAudio", pcm);
  }

  /**
   * Remember who is talking, and say so once when it changes.
   *
   * Absent on the mixed path, which is most calls, so this is additive: a
   * handler that never looks at it behaves exactly as before. The callback
   * fires on CHANGE only. It rides every audio frame, and a model told forty
   * times a second who is speaking would hear nothing else.
   */
  async #noteSpeaker(name: unknown): Promise<void> {
    if (typeof name !== "string") return;
    const speaker = name.trim();
    if (speaker === "" || speaker === this.#speaker) return;
    this.#speaker = speaker;
    // #guard already no-ops on a handler that does not implement it.
    await this.#guard("onSpeakerChange", speaker);
  }

  /**
   * Store the latest frame per source, then offer it to the handler.
   *
   * Stored even when the handler implements no callback, because
   * latestVideoFrame is the way most plugins use this lane: the model asks
   * to look long after the frame arrived.
   */
  async #onVideoFrame(frame: Record<string, unknown>): Promise<void> {
    if (this.#handler === undefined || this.#closed) return;
    const parsed = parseVideoFrame(frame);
    // Sparse and best-effort by contract. One unusable frame is not worth a log
    // line per frame, let alone ending the call.
    if (parsed === undefined) return;
    this.#frames.set(parsed.source, parsed);
    await this.#guard("onVideoFrame", parsed);
  }

  async #onContext(text: string): Promise<void> {
    // Context can arrive before session.start on a fast dial. Dropping it is
    // correct: there is no handler to receive it, and the server does not queue
    // on a plugin's behalf.
    if (this.#handler === undefined) return;
    await this.#guard("onContext", text);
  }

  /**
   * Run one handler callback. A plugin throwing must end its own call, not the
   * worker, and not the frame loop mid-utterance.
   */
  async #guard<K extends keyof CallHandler>(
    method: K,
    ...args: Parameters<NonNullable<CallHandler[K]>>
  ): Promise<void> {
    const handler = this.#handler;
    if (handler === undefined) return;
    try {
      await dispatch(handler, method, ...args);
    } catch (err) {
      logger.error(
        `standin: handler.${String(method)} failed on call ${safe(this.#callId)}: ${String(err)}`,
      );
      await this.aclose("handler-failure");
    }
  }

  /**
   * End the call when the caller's audio stops arriving.
   *
   * A live Microsoft Teams call delivers PCM continuously - silence is still frames - so
   * audio going quiet for this long means the call is gone on the far side and
   * nobody told us. That happens: the peer keeps the socket open (and even keeps
   * pinging) while its own teardown is wedged, and without this backstop the
   * handler's session burns until someone notices.
   */
  /**
   * End a call that has run past its ceiling, still going.
   *
   * The idle watchdog ends a call that went QUIET. This one ends a call that
   * has not: a caller who will not hang up, a model looping at itself, an
   * automated system that dialled and never stopped talking. Each of those bills
   * a provider by the minute for as long as the socket lives, and none of them
   * trips a silence check.
   *
   * The goodbye goes through the handler's `onGoodbye`, the same callback
   * StandIn's own closing line uses, so no plugin needs new code for this.
   * Playback is flushed FIRST, or the line queues behind however many seconds of
   * agent audio the service still holds and the call ends before anyone hears it.
   */
  #armDurationCeiling(): void {
    const limit = this.#server.maxCallMs;
    if (limit <= 0) return;
    this.#durationTimer = setTimeout(
      () => void this.#endOnCeiling(limit),
      limit,
    );
    this.#durationTimer.unref?.();
  }

  async #endOnCeiling(limit: number): Promise<void> {
    if (this.#closed || this.#ws.readyState !== WebSocket.OPEN) return;
    logger.info(
      `standin: call ${safe(this.#callId)} reached its ${limit}ms limit; saying goodbye`,
    );
    try {
      await this.cancelPlayback();
    } catch {
      // A dying socket is teardown's business, not the ceiling's.
    }
    const text = this.#server.goodbyeText.trim();
    if (text !== "") {
      await this.#guard("onGoodbye", text);
      // Bounded whatever the handler does with it. A plugin that hangs in
      // onGoodbye must not turn a time-limited call into an endless one, which
      // is the exact failure this exists to prevent.
      await new Promise((resolve) => {
        const timer = setTimeout(
          resolve,
          Math.max(0, this.#server.goodbyeGraceMs),
        );
        timer.unref?.();
      });
    }
    await this.aclose("call-duration-limit");
  }

  #armIdleWatchdog(): void {
    const idle = this.#server.audioIdleTimeoutMs;
    if (idle <= 0) return;
    const tick = Math.min(idle / 4, 10_000);
    this.#idleTimer = setInterval(() => {
      if (this.#closed) return;
      const last = this.#lastAudio;
      if (last !== undefined && Date.now() - last > idle) {
        logger.warn(
          `standin: call ${safe(this.#callId)} got no caller audio for ${idle}ms; ending it`,
        );
        void this.aclose("caller-idle-timeout");
      }
    }, tick);
    this.#idleTimer.unref?.();
  }

  /**
   * Idempotent teardown, and the FIRST reason wins - a cascade of close causes
   * must not overwrite the one that actually ended the call. Every caller awaits
   * the same promise.
   */
  async aclose(reason?: string): Promise<void> {
    if (this.#closePromise === undefined) {
      this.#closingReason = reason ?? this.#closingReason;
      this.#closed = true;
      this.#closePromise = this.#teardown();
    }
    await this.#closePromise;
  }

  async #teardown(): Promise<void> {
    try {
      clearTimeout(this.#preStartTimer);
      clearInterval(this.#idleTimer);
      clearTimeout(this.#durationTimer);

      // A handler may refuse the call from inside onStart by awaiting
      // session.end(...). Dispatching aclose while onStart is still on the stack
      // tears down half-built state, and then onStart RESUMES and finishes
      // building a provider session nothing will ever close - one leaked socket
      // per refusal. Wait for it, bounded.
      for (let i = 0; this.#inStart && i < 200; i++) {
        await new Promise((r) => setTimeout(r, 10));
      }

      // The plugin releases its side BEFORE the socket closes, so a handler that
      // wants to say something on the way out still can.
      const handler = this.#handler;
      this.#handler = undefined;
      if (handler !== undefined) {
        try {
          await dispatch(handler, "aclose", this.#closingReason);
        } catch (err) {
          logger.error(`standin: handler.aclose failed: ${String(err)}`);
        }
      }

      if (this.#ws.readyState === WebSocket.OPEN) {
        this.#send(sessionEnd(this.#closingReason));
        await this.#closeSocket();
      }
    } finally {
      // Unconditional: whatever failed above, the slot is released and the
      // callId becomes usable again.
      this.#server.release(this.#callId);
      logger.info(
        `standin: call ${safe(this.#callId)} ended (${this.#closingReason})`,
      );
      this.#onEnded?.();
    }
  }

  /** Bounded: see CLOSE_TIMEOUT_MS. Releasing the slot matters more than a clean
   * close handshake with an absent peer. */
  #closeSocket(): Promise<void> {
    return new Promise<void>((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(() => {
        this.#ws.terminate();
        resolve();
      }, CLOSE_TIMEOUT_MS);
      timer.unref?.();
      this.#ws.once("close", done);
      try {
        this.#ws.close();
      } catch {
        done();
      }
    });
  }

  #send(text: string): void {
    if (this.#ws.readyState !== WebSocket.OPEN) return;
    try {
      this.#ws.send(text);
    } catch {
      // A send failing on a dying socket is not an error worth surfacing; the
      // close handler is already on its way.
    }
  }
}

/** Options for {@link CallServer}. */
export interface CallServerOptions {
  /** Builds one {@link CallHandler} per call. This is the plugin. */
  handlerFactory: HandlerFactory;

  /**
   * Called with (callId, outcome) when StandIn reports how an outbound call
   * ended without anyone answering.
   *
   * Set it and the route exists; leave it and a POST there is a 404, so a
   * worker that never places a call opens no extra surface.
   */
  onCallOutcome?: (callId: string, outcome: string) => void | Promise<void>;
  /**
   * The connection secret from the StandIn portal. Must byte-match, or the
   * handshake is rejected with 401. Defaults to `STANDIN_SECRET`.
   */
  secret?: string;
  /** Bind address. Defaults to `STANDIN_HOST` then `0.0.0.0`. */
  host?: string;
  /** Port. Defaults to `STANDIN_PORT` then 9442. */
  port?: number;
  /** Path StandIn dials. Defaults to `STANDIN_WS_PATH` then `/msteams/calling`. */
  wsPath?: string;
  /** Concurrent live calls, checked before any crypto runs. */
  maxConnections?: number;
  /** Ms a socket may stay silent after authenticating before it is dropped. */
  preStartTimeoutMs?: number;
  /** Ms without caller audio before a live call is declared dead (0 disables). */
  audioIdleTimeoutMs?: number;
  /**
   * Ms a handler's onStart may take before the call is given up (0 disables).
   * It does real network work - joining a room, opening a provider socket - and
   * the frame loop is queued behind it, so an unbounded one holds a slot for the
   * life of the worker.
   */
  onStartTimeoutMs?: number;
  /**
   * A hard ceiling on ONE call, from `session.start` (0 disables).
   *
   * Different from `audioIdleTimeoutMs`, which ends a call that went quiet: this
   * one ends a call that is still going. A caller who will not hang up, a model
   * looping at itself, an automated system that dialled and never stopped
   * talking, all bill a provider by the minute for as long as the socket lives.
   */
  maxCallMs?: number;
  /**
   * What to say before hanging up on the limit. Delivered through the handler's
   * `onGoodbye`, the same callback StandIn's own closing line uses, so a plugin
   * needs no new code to honour it.
   */
  goodbyeText?: string;
  /** Ms to let that line finish before the call ends. */
  goodbyeGraceMs?: number;
  /**
   * Ms a call may run with nothing having answered it (0 disables).
   *
   * The gap none of the other watchdogs cover: an agent dispatch that never
   * lands leaves StandIn on the call, the caller hearing nothing, and no timer
   * that fires.
   */
  staleCallReaperMs?: number;
}

/**
 * Answers the socket StandIn dials, and hands each call to a plugin.
 *
 * Defaults match the StandIn plugin layout: port 9442, path `/msteams/calling`.
 * `0.0.0.0` because the worker usually runs in a container behind an ingress;
 * bind `127.0.0.1` when only a local tunnel should reach the listener (the
 * upgrade is HMAC-authenticated either way).
 */
export class CallServer {
  readonly #handlerFactory: HandlerFactory;
  readonly #onCallOutcome:
    ((callId: string, outcome: string) => void | Promise<void>) | undefined;
  readonly #secret: string;
  readonly #host: string;
  #port: number;
  readonly #wsPath: string;
  readonly #maxConnections: number;
  readonly preStartTimeoutMs: number;
  readonly audioIdleTimeoutMs: number;
  readonly onStartTimeoutMs: number;
  readonly maxCallMs: number;
  readonly goodbyeText: string;
  readonly goodbyeGraceMs: number;
  readonly staleCallReaperMs: number;
  #reaper: NodeJS.Timeout | undefined;

  #calls = new Map<string, Call>();
  /**
   * fingerprint -> signing timestamp (ms). Pruned by AGE, never wholesale:
   * clearing the map would reopen the replay window for every handshake still
   * inside it.
   */
  #usedSignatures = new Map<string, number>();
  #lastPrune = nowMs();
  #http: Server | undefined;
  #wss: WebSocketServer | undefined;

  /** Stop accepting new calls; live ones continue. */
  draining = false;

  constructor(options: CallServerOptions) {
    if (typeof options.handlerFactory !== "function") {
      throw new StandInError(
        "handlerFactory must be a function that builds one handler per call",
      );
    }
    this.#handlerFactory = options.handlerFactory;
    this.#onCallOutcome = options.onCallOutcome;

    this.#secret = options.secret ?? process.env.STANDIN_SECRET ?? "";
    if (!this.#secret) {
      throw new StandInError(
        "a StandIn connection secret is required: pass secret or set STANDIN_SECRET",
      );
    }

    this.#host = options.host ?? process.env.STANDIN_HOST ?? "0.0.0.0";
    this.#port = options.port ?? Number(process.env.STANDIN_PORT ?? 9442);
    const path =
      options.wsPath ?? process.env.STANDIN_WS_PATH ?? "/msteams/calling";
    this.#wsPath = "/" + path.trim().replace(/^\/+|\/+$/g, "");
    if (this.#wsPath === "/") {
      throw new StandInError(
        "wsPath must be a real path such as /msteams/calling",
      );
    }

    this.#maxConnections = options.maxConnections ?? 64;
    this.preStartTimeoutMs = options.preStartTimeoutMs ?? 10_000;
    this.audioIdleTimeoutMs = options.audioIdleTimeoutMs ?? 45_000;
    this.onStartTimeoutMs = options.onStartTimeoutMs ?? 15_000;
    this.maxCallMs = options.maxCallMs ?? 0;
    this.goodbyeText =
      options.goodbyeText ??
      "We are out of time on this call, so I have to stop here. Goodbye.";
    this.goodbyeGraceMs = options.goodbyeGraceMs ?? 6_000;
    this.staleCallReaperMs = options.staleCallReaperMs ?? STALE_CALL_REAPER_MS;
  }

  get wsPath(): string {
    return this.#wsPath;
  }

  /** The interface the listener is bound to, or will be. */
  get host(): string {
    return this.#host;
  }

  get port(): number {
    return this.#port;
  }

  get activeCalls(): number {
    return this.#calls.size;
  }

  /**
   * Whether the listener is actually bound.
   *
   * A host that calls connect twice today binds a second listener and leaks the
   * first, and has no way to ask whether the bind succeeded, so it reports a
   * dead platform as connected.
   */
  get running(): boolean {
    return this.#http !== undefined;
  }

  /** @internal */
  buildHandler(): CallHandler {
    return this.#handlerFactory();
  }

  /** @internal */
  release(callId: string): void {
    this.#calls.delete(callId);
  }

  /**
   * How an outbound call ended, reported by StandIn.
   *
   * Only reached when a plugin asked for it. The route carries the only signal
   * that nobody answered, and without it an unanswered call waits out the ring
   * timeout before anything can be said about it.
   *
   * The body is read and capped BEFORE the signature is checked, because v2
   * signs a hash of the body: there is nothing to verify until the bytes are in
   * hand. The cap is what stops that being a way to make the worker read an
   * unbounded request from an unauthenticated peer.
   */
  async #onOutcome(
    req: IncomingMessage,
    res: ServerResponse,
    path: string,
    callId: string,
  ): Promise<void> {
    const declared = Number(req.headers["content-length"] ?? NaN);
    if (Number.isFinite(declared) && declared > MAX_OUTCOME_BYTES) {
      res.writeHead(413).end();
      req.destroy();
      return;
    }
    const chunks: Buffer[] = [];
    let total = 0;
    let tooBig = false;
    for await (const chunk of req) {
      total += (chunk as Buffer).length;
      if (total > MAX_OUTCOME_BYTES) {
        tooBig = true;
        break;
      }
      chunks.push(chunk as Buffer);
    }
    if (tooBig) {
      res.writeHead(413).end();
      req.destroy();
      return;
    }
    const raw = Buffer.concat(chunks);

    const timestamp = String(req.headers[TIMESTAMP_HEADER] ?? "");
    const signature = String(req.headers[SIGNATURE_V2_HEADER] ?? "");
    const expected = signRequest(this.#secret, timestamp, "POST", path, raw);
    if (
      !signature ||
      !timingSafeEqualHex(signature.trim().toLowerCase(), expected)
    ) {
      logger.warn(
        `standin: refused an unsigned call outcome for ${safe(callId)}`,
      );
      res.writeHead(401).end();
      return;
    }
    const sent = Number(timestamp);
    if (!Number.isFinite(sent) || Math.abs(nowMs() - sent) > REPLAY_WINDOW_MS) {
      res.writeHead(401).end();
      return;
    }

    let outcome = "";
    try {
      const parsed: unknown = raw.length
        ? JSON.parse(raw.toString("utf8"))
        : {};
      if (typeof parsed === "object" && parsed !== null) {
        const body = parsed as Record<string, unknown>;
        outcome = String(body.outcome ?? body.reason ?? "");
      }
    } catch {
      outcome = "";
    }

    try {
      await this.#onCallOutcome?.(callId, outcome);
    } catch (err) {
      // A plugin failing to handle an outcome must not make StandIn retry
      // forever. Log it and acknowledge.
      logger.error(
        `standin: handling the outcome for ${safe(callId)} failed: ${String(err)}`,
      );
    }
    res.writeHead(204).end();
  }

  /**
   * Bind the listener. Transactional: either it is listening when this resolves,
   * or nothing of it survives.
   */
  async start(): Promise<void> {
    if (this.#http !== undefined)
      throw new StandInError("this listener is already running");
    const outcomePrefix = `${this.#wsPath}/outcome/`;
    const http = createServer((req: IncomingMessage, res: ServerResponse) => {
      if (req.url === "/healthz") {
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ ok: true, calls: this.#calls.size }));
        return;
      }
      const path = (req.url ?? "").split("?")[0] ?? "";
      if (
        this.#onCallOutcome !== undefined &&
        req.method === "POST" &&
        path.startsWith(outcomePrefix)
      ) {
        void this.#onOutcome(req, res, path, path.slice(outcomePrefix.length));
        return;
      }
      res.writeHead(404).end();
    });

    const wss = new WebSocketServer({
      noServer: true,
      maxPayload: MAX_PAYLOAD_BYTES,
    });
    http.on("upgrade", (req, socket, head) => {
      this.#onUpgrade(req, socket, head, wss);
    });

    try {
      await new Promise<void>((resolve, reject) => {
        http.once("error", reject);
        http.listen(this.#port, this.#host, () => {
          http.removeListener("error", reject);
          resolve();
        });
      });
    } catch (err) {
      http.close();
      throw err;
    }

    const address = http.address();
    if (address !== null && typeof address === "object")
      this.#port = address.port;
    this.#http = http;
    this.#wss = wss;
    this.#armReaper();
    logger.info(
      `standin: answering Microsoft Teams calls on ${this.#host}:${this.#port}${this.#wsPath}`,
    );
  }

  /**
   * End calls that nothing ever answered.
   *
   * An agent dispatch that never lands is the commonest misconfiguration there
   * is, and it is invisible to every other watchdog: session.start arrived,
   * onStart succeeded, and the caller keeps sending audio the whole time.
   * Without this the caller sits on a live call hearing nothing and the worker
   * holds the slot until somebody notices.
   */
  #armReaper(): void {
    const stale = this.staleCallReaperMs;
    if (stale <= 0) return;
    const interval = Math.max(
      REAPER_MIN_INTERVAL_MS,
      Math.min(REAPER_CHECK_INTERVAL_MS, stale),
    );
    const reaped = new Set<string>();
    this.#reaper = setInterval(() => {
      const now = monotonicMs();
      // A SNAPSHOT: ending a call removes it from the registry being iterated.
      const live = [...this.#calls.entries()];
      // Bounded by what is actually running, so a long-lived worker does not
      // accumulate ids for ever.
      for (const id of [...reaped]) {
        if (!this.#calls.has(id)) reaped.delete(id);
      }
      for (const [callId, call] of live) {
        if (reaped.has(callId) || !isUnanswered(call, stale, now)) continue;
        reaped.add(callId);
        logger.warn(
          `standin: nothing answered call ${safe(callId)} within ${stale}ms; ending it`,
        );
        void call.aclose("no-agent-answered");
      }
    }, interval);
    this.#reaper.unref?.();
  }

  /**
   * Drain live calls, then stop listening. Awaits the calls' REAL teardown - a
   * close that early-returns on an in-flight closer would let the process exit
   * with teardown still pending, leaking sessions.
   */
  async aclose(): Promise<void> {
    clearInterval(this.#reaper);
    this.#reaper = undefined;
    const calls = [...this.#calls.values()];
    await Promise.allSettled(calls.map((c) => c.aclose("server-shutdown")));
    this.#calls.clear();

    const wss = this.#wss;
    this.#wss = undefined;
    if (wss !== undefined)
      await new Promise<void>((resolve) => wss.close(() => resolve()));

    const http = this.#http;
    this.#http = undefined;
    if (http !== undefined)
      await new Promise<void>((resolve) => http.close(() => resolve()));
  }

  #reject(socket: Duplex, status: number, text: string): void {
    const body = Buffer.from(text, "utf8");
    socket.write(
      `HTTP/1.1 ${status} ${text}\r\n` +
        "connection: close\r\n" +
        "content-type: text/plain; charset=utf-8\r\n" +
        `content-length: ${body.length}\r\n\r\n`,
    );
    socket.end(body);
    socket.destroy();
  }

  #onUpgrade(
    req: IncomingMessage,
    socket: Duplex,
    head: Buffer,
    wss: WebSocketServer,
  ): void {
    const prefix = `${this.#wsPath}/`;
    let callId: string;
    try {
      const url = new URL(req.url ?? "/", "http://localhost");
      if (!url.pathname.startsWith(prefix)) {
        this.#reject(socket, 404, "not found");
        return;
      }
      callId = decodeURIComponent(url.pathname.slice(prefix.length));
    } catch {
      // This event listener runs before authentication. A malformed URL or
      // percent escape must reject one request, never escape and kill Node.
      this.#reject(socket, 400, "malformed request path");
      return;
    }
    if (!callId || callId.includes("/")) {
      this.#reject(socket, 400, "missing callId");
      return;
    }

    // Draining: live calls continue, new ones are refused so a worker that is
    // winding down does not accept calls it will never serve.
    if (this.draining) {
      this.#reject(socket, 503, "draining");
      return;
    }

    // Capacity is checked BEFORE any crypto, so a flood cannot make us spend CPU
    // on signatures for calls we were never going to accept.
    if (this.#calls.size >= this.#maxConnections) {
      logger.warn(`standin: refusing ${safe(callId)}, at capacity`);
      this.#reject(socket, 503, "at capacity");
      return;
    }

    const timestamp = req.headers[TIMESTAMP_HEADER] as string | undefined;
    const signature = req.headers[SIGNATURE_HEADER] as string | undefined;
    if (!verifyHandshake(this.#secret, timestamp, callId, signature)) {
      this.#reject(socket, 401, "unauthorized");
      return;
    }

    // Single-use handshake: a correctly signed upgrade replayed inside the
    // freshness window must not open a second socket. The fingerprint uses the
    // NORMALIZED signature - verify accepts case/whitespace variants, so keying
    // on the raw header would let the same capture replay once per casing.
    const sigNorm = (signature ?? "").trim().toLowerCase();
    const fingerprint = `${timestamp}.${sigNorm}`;
    const now = nowMs();
    if (this.#usedSignatures.has(fingerprint)) {
      this.#reject(socket, 401, "handshake already used");
      return;
    }
    // Key on the SIGNING timestamp, never the arrival time: verification accepts
    // a timestamp up to REPLAY_WINDOW_MS in the FUTURE, so an entry aged from
    // arrival can be pruned while its signature is still valid - reopening the
    // exact replay this guard exists to close.
    this.#usedSignatures.set(fingerprint, timestamp ? Number(timestamp) : now);
    // Prune on a time throttle, not a size threshold: rebuilding once the map
    // passes a watermark makes every later request O(n). Only correctly signed,
    // not-yet-seen handshakes reach this line, so the map tracks StandIn's real
    // call rate rather than attacker traffic.
    if (now - this.#lastPrune >= PRUNE_INTERVAL_MS) {
      this.#lastPrune = now;
      const cutoff = now - REPLAY_WINDOW_MS;
      for (const [fp, ts] of this.#usedSignatures) {
        if (ts < cutoff) this.#usedSignatures.delete(fp);
      }
    }

    if (this.#calls.has(callId)) {
      this.#reject(socket, 409, "call already has a live session");
      return;
    }

    wss.handleUpgrade(req, socket, head, (ws) => {
      const call = new Call(this, callId, ws);
      this.#calls.set(callId, call);
      logger.info(`standin: call ${safe(callId)} connected`);
      void call.run();
    });
  }
}
