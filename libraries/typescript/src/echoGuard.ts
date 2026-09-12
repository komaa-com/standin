// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The half-duplex echo guard.
 *
 * A Microsoft Teams call is one acoustic loop. When the caller is on a speakerphone, the
 * agent's own voice comes back up the caller leg loudly enough for a realtime
 * model's server-side VAD to hear it as a turn - so the model answers itself,
 * interrupts itself, and re-greets, with the caller silent the whole time. That
 * is not a hypothetical: it is the failure this guard was written to stop.
 *
 * The fix is a time window rather than acoustic cancellation: while our own audio
 * is still PLAYING OUT on the call, caller input below a loudness threshold is
 * treated as our own echo and dropped, and anything above it is treated as a real
 * barge-in and passed through.
 *
 * It lives in the plugin rather than the SDK core: the SDK owns the wire,
 * not the room's acoustics.
 */

/**
 * How long after our audio finishes playing the guard stays armed. Playout is not
 * instantaneous and neither is the echo path.
 */
export const ECHO_SUPPRESSION_WINDOW_MS = 600;

/** Normalized RMS above which in-window caller input is a real barge-in, not echo. */
export const ECHO_BARGE_IN_RMS = 0.04;

// The one definition lives in the SDK's core audio module, because an echo
// guard, a barge-in check and a voice segmenter all want it. Imported for use
// below and re-exported so this module keeps its historical surface.
import { pcm16Rms } from "./audio.js";

export { pcm16Rms };

/** Tunables, straight off the plugin's `realtime` config block. */
export interface EchoGuardOptions {
  /** Set false to disable the guard entirely. */
  suppressInputDuringPlayback?: boolean;
  /** Window in ms; default {@link ECHO_SUPPRESSION_WINDOW_MS}. */
  echoSuppressionWindowMs?: number;
  /** Barge-in RMS gate 0..1; default {@link ECHO_BARGE_IN_RMS}. */
  echoBargeInRms?: number;
  /**
   * False drops the loudness exception and suppresses ALL in-window input.
   *
   * Used until the caller's first real turn. On a speakerphone the opening
   * greeting echoes back loud enough to clear the barge-in RMS, so allowing
   * barge-in from the first frame is exactly what starts the echo loop. Once the
   * caller has genuinely spoken once, normal RMS barge-in resumes for the rest of
   * the call.
   */
  allowBargeIn?: boolean;
}

/**
 * True when this frame of caller audio should be withheld from the model.
 *
 * `playbackActiveUntil` is the estimated epoch-ms at which the audio we have
 * already sent finishes PLAYING - not the time we last sent a chunk. A realtime
 * model generates faster than realtime, so send-time and play-time diverge by
 * seconds on a long answer, and a guard keyed on send-time disarms in the middle
 * of the sentence it is supposed to cover.
 */
export function shouldSuppressEcho(
  pcm16k: Buffer,
  playbackActiveUntil: number,
  opts?: EchoGuardOptions,
): boolean {
  if (opts?.suppressInputDuringPlayback === false) {
    return false;
  }
  const inPlaybackWindow =
    Date.now() <
    playbackActiveUntil +
      (opts?.echoSuppressionWindowMs ?? ECHO_SUPPRESSION_WINDOW_MS);
  if (!inPlaybackWindow) {
    return false;
  }
  // Before the caller's first real turn, every in-window frame is echo.
  if (opts?.allowBargeIn === false) {
    return true;
  }
  return pcm16Rms(pcm16k) < (opts?.echoBargeInRms ?? ECHO_BARGE_IN_RMS);
}
