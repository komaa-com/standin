// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The seam adapter: one {@link CallHandler} per Microsoft Teams call.
 *
 * This is the whole surface between StandIn and OpenClaw, and it is deliberately
 * thin. Everything framework-independent - the socket, the HMAC handshake and its
 * replay guard, capacity, the wire protocol, the outbound sequence number and
 * audio timeline, the pre-start and audio-idle watchdogs, idempotent teardown -
 * belongs to the SDK's `CallServer`. Everything model-shaped belongs to
 * {@link createRealtimeCall}. What is left here is the decision of whether to
 * take the call at all, and the wiring between the two.
 *
 * There are four refusal paths, and each is one line, because `session.end()` is
 * safe to call from inside `onStart`: it returns immediately and the close runs
 * once `onStart` unwinds, so refusing a call needs no dance around a teardown
 * deadlock.
 */

import {
  contextSentences,
  type CallHandler,
  type CallSession,
  type ChatChannel,
  type PersonalChats,
} from "../../index.js";
import type { OpenClawConfig } from "openclaw/plugin-sdk/config-contracts";
import type {
  RealtimeVoiceProviderConfig,
  RealtimeVoiceProviderPlugin,
} from "openclaw/plugin-sdk/realtime-voice";

import { describeInboundRejection, isInboundCallAllowed } from "./allowlist.js";
import { sessionKey, type ResolvedPluginConfig } from "./config.js";
import {
  createRealtimeCall,
  type CallLogger,
  type RealtimeCall,
} from "./realtime.js";
import { enqueueMeetingRecap } from "./recap.js";

/** The host realtime provider, resolved once at startup and shared by every call. */
export interface ResolvedRealtime {
  provider: RealtimeVoiceProviderPlugin;
  providerConfig: RealtimeVoiceProviderConfig;
}

/**
 * Concurrency and shutdown, owned by the runtime.
 *
 * The SDK caps connections too, and that cap is the one that protects the worker.
 * This one is the OPERATOR's number: it is what `maxConcurrentCalls` in the
 * plugin config means, it is enforced after the handshake so a refused call gets
 * a spoken-protocol close rather than a dropped socket, and it is the hook the
 * gateway's shutdown uses to end live calls before the listener goes away.
 */
export interface CallRegistry {
  /** Reserve a slot. False when the operator's cap is already reached. */
  acquire(callId: string): boolean;
  /** Attach the live call so shutdown can reach it. */
  bind(callId: string, call: RealtimeCall): void;
  /** Free the slot. Idempotent - teardown runs on every path. */
  release(callId: string): void;
}

/** Everything one handler needs. Built once by the runtime and closed over. */
export interface HandlerDeps {
  config: ResolvedPluginConfig;
  /**
   * Undefined when no realtime provider resolved at startup. Not an error at
   * construction time: the gateway may be brought up before its credentials are,
   * and refusing each call with a named reason beats refusing to boot.
   */
  realtime?: ResolvedRealtime;
  /** The gateway's own config, passed through to the host session. */
  cfg?: OpenClawConfig;
  registry: CallRegistry;
  logger?: CallLogger;
  /** Outbound chat lane, started only when meetingRecap is on. */
  chat?: Pick<ChatChannel, "send">;
  chats?: PersonalChats;
  /** Text agent used to write minutes. The first argument is the session-scope key. */
  consult?: (sessionKey: string, prompt: string) => Promise<string>;
}

/**
 * One Microsoft Teams call.
 *
 * Implements four of the SDK's five methods. `onCallerAudio` is the hot path and
 * does no work of its own - it hands the frame to the realtime bridge, which owns
 * the recording gate, the echo guard and the resampling.
 */
export class TeamsCallHandler implements CallHandler {
  readonly #deps: HandlerDeps;
  #call: RealtimeCall | undefined;
  #session: CallSession | undefined;
  #sessionKey = "";
  #callId = "";
  /** Set only once a slot is actually reserved, so a refusal never frees someone else's. */
  #held = false;

  constructor(deps: HandlerDeps) {
    this.#deps = deps;
  }

  async onStart(session: CallSession): Promise<void> {
    const { config, realtime, registry, logger } = this.#deps;
    this.#callId = session.callId;
    this.#session = session;
    this.#sessionKey = sessionKey(config.voice.sessionScope, {
      callId: session.callId,
      threadId: session.start.threadId,
      caller: { aadId: session.start.caller.aadId },
    });

    // REFUSAL 1: no realtime provider. The startup log already said this would
    // happen; the close reason is what makes it visible on the call itself
    // instead of as silence.
    if (!realtime) {
      logger?.error?.(
        `standin-msteams: no realtime voice provider resolved - refusing call ${session.callId}`,
      );
      await session.end("realtime-unavailable");
      return;
    }

    // REFUSAL 2: inbound policy. Checked before the slot is taken, so a refused
    // caller cannot consume capacity by dialling repeatedly.
    const from = session.start.caller.aadId ?? "";
    if (
      !isInboundCallAllowed(
        config.voice.inboundPolicy,
        config.voice.allowFrom,
        from,
      )
    ) {
      logger?.warn?.(
        `standin-msteams: ${describeInboundRejection(config.voice.inboundPolicy, from)}`,
      );
      await session.end("not-allowed");
      return;
    }

    // REFUSAL 3: the operator's concurrency cap.
    if (!registry.acquire(session.callId)) {
      logger?.warn?.(
        `standin-msteams: at maxConcurrentCalls (${config.media.maxConcurrentCalls}) - refusing call ${session.callId}`,
      );
      await session.end("busy");
      return;
    }
    this.#held = true;

    const call = createRealtimeCall({
      session,
      deps: {
        provider: realtime.provider,
        providerConfig: realtime.providerConfig,
        cfg: this.#deps.cfg,
        instructions: config.voice.realtime.instructions,
        greetingInstructions: config.voice.inboundGreeting,
        requireRecordingStatus: config.voice.requireRecordingStatus,
        echo: config.voice.realtime,
        logger,
      },
    });

    // REFUSAL 4: the model never came up.
    //
    // Awaited, not fired off: the SDK bounds onStart and holds caller audio until
    // it resolves, so a failed connect becomes a refusal with a reason rather than
    // an answered call that never speaks.
    try {
      await call.connect();
    } catch (err) {
      logger?.error?.(
        `standin-msteams: realtime connect failed on ${session.callId} - ${err instanceof Error ? err.message : String(err)}`,
      );
      call.close();
      await session.end("realtime-unavailable");
      return;
    }

    this.#call = call;
    registry.bind(session.callId, call);
    logger?.info?.(
      `standin-msteams: call ${session.callId} live` +
        (session.start.caller.displayName
          ? ` with ${session.start.caller.displayName}`
          : "") +
        ` (${this.#sessionKey})`,
    );
  }

  /** PCM16, 16 kHz, mono, little-endian, already validated by the SDK. */
  async onCallerAudio(pcm: Buffer): Promise<void> {
    this.#call?.pushAudio(pcm);
  }

  /**
   * Participant counts, DTMF and recording status, as plain sentences.
   *
   * Recording status is the one that is not just text: it opens and closes the
   * media gate, so it is matched rather than forwarded. Matched against the SDK's
   * own {@link contextSentences} rather than a string literal of our own, so the
   * two cannot drift apart the next time the wording is improved.
   */
  async onContext(text: string): Promise<void> {
    if (!this.#call) return;
    if (text === contextSentences.recording("active")) {
      this.#call.setRecordingActive(true);
      return;
    }
    if (text === contextSentences.recording("inactive")) {
      this.#call.setRecordingActive(false);
      return;
    }
    this.#call.pushContext(text);
  }

  /**
   * StandIn is ending the call and wants this line spoken first.
   *
   * Interrupt, then say. A goodbye queued behind a long answer is a goodbye the
   * caller never hears, and teardown follows within seconds.
   */
  async onGoodbye(text: string): Promise<void> {
    if (!this.#call) return;
    this.#call.interrupt();
    this.#call.say(text);
  }

  /** Always called exactly once, on every path, before the slot is freed. */
  async aclose(reason: string): Promise<void> {
    const session = this.#session;
    const transcript = this.#call?.transcript;
    this.#call?.close();
    this.#call = undefined;
    this.#session = undefined;
    if (this.#held) {
      this.#deps.registry.release(this.#callId);
      this.#held = false;
    }
    this.#deps.logger?.info?.(
      `standin-msteams: call ${this.#callId} ended (${reason})`,
    );
    // Detached on purpose. The SDK awaits aclose before it sends session.end,
    // closes the socket and frees the connection slot, and a recap consult can
    // take tens of seconds. The transcript is on disk before enqueue returns, so
    // a process restart still posts. enqueueMeetingRecap never throws, so
    // nothing in aclose is lost by not awaiting it.
    if (session && transcript) {
      void enqueueMeetingRecap({
        enabled: this.#deps.config.voice.meetingRecap,
        session,
        transcript,
        chat: this.#deps.chat,
        chats: this.#deps.chats,
        sessionKey: this.#sessionKey,
        summarise: this.#deps.consult
          ? (prompt) => this.#deps.consult!(this.#sessionKey, prompt)
          : undefined,
        logger: this.#deps.logger,
      });
    }
  }
}
