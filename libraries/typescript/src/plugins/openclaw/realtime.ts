// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One Microsoft Teams call, bridged to OpenClaw's realtime speech-to-speech session.
 *
 * This file is the reason the plugin cannot be a standalone worker. It does not
 * drive a model: it CONSUMES the host's realtime session
 * (`createRealtimeVoiceBridgeSession`), the host's provider registry and the
 * host's config, all of which are in-process objects inside the OpenClaw gateway.
 * Nothing here opens a provider socket, and nothing here knows which vendor is on
 * the other end.
 *
 * What it does own is the audio boundary between two rates and two framings:
 *
 *     caller   16 kHz --> 24 kHz --> host realtime session
 *     model    24 kHz --> 16 kHz --> session.sendAudio
 *
 * and the three things that make a spoken call feel right rather than merely
 * work: the echo guard, the playout clock the guard keys on, and barge-in.
 *
 * Everything the SDK owns is deliberately absent here: the socket, the sequence
 * number, the outbound timeline, the frame loop and the watchdogs.
 */

import {
  FrameAligner,
  REALTIME_SAMPLE_RATE_HZ,
  SAMPLE_RATE_HZ,
  frameDurationMs,
  resamplePcm16,
  type CallSession,
} from "../../index.js";
import type { OpenClawConfig } from "openclaw/plugin-sdk/config-contracts";
import {
  REALTIME_VOICE_AUDIO_FORMAT_PCM16_24KHZ,
  createRealtimeVoiceBridgeSession,
  type RealtimeVoiceProviderConfig,
  type RealtimeVoiceProviderPlugin,
} from "openclaw/plugin-sdk/realtime-voice";

import { shouldSuppressEcho, type EchoGuardOptions } from "../../echoGuard.js";
import { isVerbalInterrupt } from "../../gate.js";

/**
 * The logging surface this file needs, and no more.
 *
 * Structural, not OpenClaw's `PluginLogger`, so a test can pass `{}` and the call
 * path stays identical. Every method is optional for the same reason.
 */
export interface CallLogger {
  debug?(message: string): void;
  info?(message: string): void;
  warn?(message: string): void;
  error?(message: string): void;
}

/** What the runtime hands one call. Resolved once at startup, shared by every call. */
export interface RealtimeCallDeps {
  /** The host's realtime voice provider, already resolved and credentialed. */
  provider: RealtimeVoiceProviderPlugin;
  providerConfig: RealtimeVoiceProviderConfig;
  /** The gateway's own config, which the host session reads for model defaults. */
  cfg?: OpenClawConfig;
  /** System instructions for this call. */
  instructions?: string;
  /** Spoken on pickup. Undefined means the agent waits for the caller to speak. */
  greetingInstructions?: string;
  /** Withhold caller media until Microsoft Teams reports recording active. */
  requireRecordingStatus: boolean;
  echo: EchoGuardOptions;
  logger?: CallLogger;
}

/** One bridged call, as {@link TeamsCallHandler} drives it. */
export interface RealtimeCall {
  /** Open the provider session. Rejects if the model never comes up. */
  connect(): Promise<void>;
  /** One frame of caller audio, PCM16 16 kHz mono LE, straight off the wire. */
  pushAudio(pcm16k: Buffer): void;
  /** Put a plain sentence in front of the model without interrupting it. */
  pushContext(text: string): void;
  /** Microsoft Teams recording status changed. Opens or closes the media gate. */
  setRecordingActive(active: boolean): void;
  /** Cut the model off mid-turn. Call before {@link RealtimeCall.say}. */
  interrupt(): void;
  /** Speak this line in the agent's own voice, now. */
  say(text: string): void;
  /** Release the provider session. Idempotent. */
  close(): void;
}

/**
 * Build the bridge for one call.
 *
 * `session` is the SDK's {@link CallSession}: the only route to the caller, and
 * the owner of the sequence number and the outbound timeline, so nothing here
 * tracks either.
 */
export function createRealtimeCall(params: {
  session: CallSession;
  deps: RealtimeCallDeps;
}): RealtimeCall {
  const { session, deps } = params;
  const { logger } = deps;
  const callId = session.callId;

  let closed = false;

  /**
   * Estimated epoch ms at which the audio already sent finishes PLAYING.
   *
   * The model generates faster than realtime and the service queues what we send,
   * so send-time is not play-time. Summing each chunk's own duration tracks the
   * playout clock instead, which is what the echo guard has to be keyed on: a
   * guard armed off last-send disarms in the middle of the sentence it exists to
   * cover.
   */
  let playbackEndAt = 0;

  /**
   * False until the caller has genuinely spoken once. See
   * {@link EchoGuardOptions.allowBargeIn}: the opening greeting echoing off a
   * speakerphone will clear the barge-in RMS, so the loudness exception has to
   * stay off until there is a real caller turn to measure against.
   */
  let callerTurnStarted = false;

  /**
   * Microsoft Teams recording status, seeded from `session.start` and updated by context.
   * Gates every media-derived path (audio in, DTMF, transcripts) when
   * `requireRecordingStatus` is on.
   */
  let recordingActive = session.start.recordingStatus === "active";
  const recordingGateBlocks = (): boolean =>
    deps.requireRecordingStatus && !recordingActive;

  /**
   * Whole-frame alignment for the downlink.
   *
   * A 24 kHz delta resampled to 16 kHz does not divide evenly into the wire's
   * 640-byte frame, and dropping the remainder clips a few milliseconds off every
   * chunk seam - audible over a call as clipped word endings. The SDK owns this;
   * do not hand-roll it.
   */
  const aligner = new FrameAligner();

  /**
   * The host's audio sink is SYNCHRONOUS and `session.sendAudio` is not, so the
   * sends are chained rather than awaited. A chain, not fire-and-forget: two
   * unordered sends would put the model's voice out of order on the wire, which
   * is worse than late audio. The catch is inside the chain so one failed send
   * cannot poison every send after it.
   */
  let sendChain: Promise<void> = Promise.resolve();
  const enqueue = (op: () => Promise<void>): void => {
    sendChain = sendChain.then(op).catch((err: unknown) => {
      logger?.debug?.(
        `standin-msteams: send failed on ${callId} - ${err instanceof Error ? err.message : String(err)}`,
      );
    });
  };

  /** Flush the held-back tail of a finished turn, zero-padded to a whole frame. */
  const flushTail = (): void => {
    const tail = aligner.flush();
    if (tail) enqueue(() => session.sendAudio(tail));
  };

  /**
   * Barge-in. THE ORDER HERE IS THE WHOLE POINT.
   *
   * `cancelPlayback` is the only lever that un-sends audio the service has already
   * buffered, so it goes FIRST: cancel the model first and the caller still hears
   * the rest of the interrupted turn play out, which is exactly the "it kept
   * talking over me" complaint barge-in exists to fix. The aligner's residual
   * belongs to that same dead turn, so it is dropped rather than flushed.
   */
  const flushPlayback = (): void => {
    aligner.reset();
    enqueue(() => session.cancelPlayback());
    // The flush stops playout immediately, so the playout estimate collapses to now.
    playbackEndAt = Date.now();
  };

  const realtime = createRealtimeVoiceBridgeSession({
    provider: deps.provider,
    providerConfig: deps.providerConfig,
    cfg: deps.cfg,
    audioFormat: REALTIME_VOICE_AUDIO_FORMAT_PCM16_24KHZ,
    instructions: deps.instructions,
    initialGreetingInstructions: deps.greetingInstructions,
    triggerGreetingOnReady: Boolean(deps.greetingInstructions),
    autoRespondToAudio: true,
    interruptResponseOnInputAudio: true,
    audioSink: {
      isOpen: () => !closed,
      sendAudio: (pcm24k: Buffer) => {
        if (closed || pcm24k.length === 0) {
          return;
        }
        const pcm16k = resamplePcm16(
          pcm24k,
          REALTIME_SAMPLE_RATE_HZ,
          SAMPLE_RATE_HZ,
        );
        playbackEndAt =
          Math.max(playbackEndAt, Date.now()) + frameDurationMs(pcm16k);
        for (const frame of aligner.push(pcm16k)) {
          enqueue(() => session.sendAudio(frame));
        }
      },
      // CANCEL SITE 1: the model truncated its own turn because it heard the
      // caller start speaking. Upstream is already cancelled - the model is the
      // one telling us - so the only thing left to undo is the audio the service
      // still has queued.
      clearAudio: () => {
        flushPlayback();
      },
    },
    onTranscript: (role, text, isFinal) => {
      // The caller's first real speech ends the opening echo-only window and
      // restores normal RMS barge-in for the rest of the call.
      if (role === "user" && text.trim().length > 0) {
        callerTurnStarted = true;
      }
      if (role === "assistant" && isFinal) {
        // End of turn: the residual is real audio, not a fragment to discard.
        flushTail();
      }
      if (role !== "user" || !isFinal) {
        return;
      }
      // CANCEL SITE 2: a deterministic verbal interrupt ("stop", "hold on").
      // Handled here rather than left to the model because the model is
      // mid-generation when it arrives; matching the phrase ourselves is what
      // makes the cut feel instant. Only while we are actually still speaking -
      // otherwise "stop" is just a word in a sentence.
      if (Date.now() < playbackEndAt && isVerbalInterrupt(text)) {
        logger?.debug?.(
          `standin-msteams: verbal interrupt on ${callId} - flushing playback`,
        );
        flushPlayback();
      }
    },
    onError: (error: Error) => {
      logger?.warn?.(
        `standin-msteams: realtime session error on ${callId} - ${error.message}`,
      );
    },
  });

  return {
    connect: () => realtime.connect(),

    pushAudio: (pcm16k: Buffer) => {
      if (closed || pcm16k.length === 0) {
        return;
      }
      // Recording gate: caller audio is call media, and the Microsoft Media Access
      // API says a bot does not process it before recording status is active. A
      // no-op when requireRecordingStatus is off.
      if (recordingGateBlocks()) {
        return;
      }
      if (
        shouldSuppressEcho(pcm16k, playbackEndAt, {
          ...deps.echo,
          allowBargeIn: callerTurnStarted,
        })
      ) {
        return;
      }
      realtime.sendAudio(
        resamplePcm16(pcm16k, SAMPLE_RATE_HZ, REALTIME_SAMPLE_RATE_HZ),
      );
    },

    pushContext: (text: string) => {
      if (closed || !text.trim()) {
        return;
      }
      // DTMF and participant counts are media-derived too, so they sit behind the
      // same gate as audio. Recording-status context itself is handled by the
      // handler before it reaches here - that one must never be gated on itself.
      if (recordingGateBlocks()) {
        return;
      }
      realtime.sendUserMessage(text);
    },

    setRecordingActive: (active: boolean) => {
      recordingActive = active;
    },

    interrupt: () => {
      if (closed) return;
      try {
        realtime.handleBargeIn();
      } catch (err) {
        // Non-fatal: handleBargeIn throws when nothing is speaking, which is a
        // perfectly ordinary state to interrupt from.
        logger?.debug?.(
          `standin-msteams: barge-in on ${callId} - ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    },

    say: (text: string) => {
      if (closed || !text.trim()) return;
      try {
        // Same path as the greeting: inject the line as an instruction and trigger
        // a spoken response. There is no separate "speak this" surface.
        realtime.triggerGreeting(text);
      } catch (err) {
        logger?.warn?.(
          `standin-msteams: say failed on ${callId} - ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    },

    close: () => {
      if (closed) return;
      closed = true;
      aligner.reset();
      try {
        realtime.close();
      } catch (err) {
        logger?.debug?.(
          `standin-msteams: realtime close on ${callId} - ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    },
  };
}
