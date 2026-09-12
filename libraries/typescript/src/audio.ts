// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The audio the wire carries, and the two helpers every plugin needs.
 *
 * The StandIn wire is PCM 16 kHz, 16-bit, mono, little-endian in both
 * directions. Almost nothing else is. The realtime speech-to-speech models speak
 * 24 kHz, most TTS vendors emit 22.05 or 24 kHz, and none of them chunk on the
 * wire's frame boundary. So every plugin that is not a pure passthrough
 * ends up writing the same two things:
 *
 * - a resampler, because the rates differ
 * - a frame aligner, because a resampled buffer does not divide evenly into the
 *   wire's 640-byte frame, and dropping the remainder clips the end of every turn
 *
 * They live here rather than in each plugin because they are properties of the
 * WIRE, not of any framework - the same reason the sequence number and the
 * outbound timeline live in {@link CallServer}. A plugin that has to reimplement
 * them is a plugin the SDK failed.
 *
 * Byte-for-byte equivalent to the Python SDK's `standin/sdk/audio.py`; the
 * shared conformance vectors assert both produce identical output.
 */

import { SAMPLE_RATE_HZ } from "./protocol.js";

/** 16-bit mono. */
export const BYTES_PER_SAMPLE = 2;

/** The nominal frame StandIn sends: 20 ms of PCM16 at 16 kHz = 320 samples. */
export const FRAME_MS = 20;
export const FRAME_BYTES =
  (SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * FRAME_MS) / 1000; // 640

/**
 * What the realtime speech-to-speech models speak. Named here rather than in a
 * plugin because the RATIO is what forces the residual buffer below, and that is
 * an audio concern rather than a provider one.
 */
export const REALTIME_SAMPLE_RATE_HZ = 24_000;

/**
 * Duration of a PCM16 mono buffer in milliseconds.
 *
 * Use this for a playout clock rather than counting frames: outbound chunk
 * lengths are NOT fixed, so a frame count drifts against real time.
 */
export function frameDurationMs(pcm: Buffer): number {
  return (pcm.length / (SAMPLE_RATE_HZ * BYTES_PER_SAMPLE)) * 1000.0;
}

/**
 * Linear-interpolation resample of PCM16 mono.
 *
 * An odd trailing byte is dropped rather than throwing: a truncated frame is a
 * glitch, but an exception in the audio path is a dropped call.
 */
export function resamplePcm16(
  pcm: Buffer,
  srcHz: number,
  dstHz: number,
): Buffer {
  if (srcHz === dstHz || pcm.length === 0) return pcm;

  const usable = pcm.length % 2 === 0 ? pcm : pcm.subarray(0, pcm.length - 1);
  const nIn = usable.length / 2;
  if (nIn === 0) return Buffer.alloc(0);

  // Math.round matches Python's round() for the .5 cases these rates produce
  // (2:3 and 3:2 never land exactly on .5), so both SDKs agree on n_out.
  const nOut = Math.max(1, Math.round((nIn * dstHz) / srcHz));
  const step = nIn / nOut;
  const out = Buffer.alloc(nOut * 2);

  for (let i = 0; i < nOut; i++) {
    const pos = i * step;
    const j = Math.trunc(pos);
    let value: number;
    if (j >= nIn - 1) {
      value = usable.readInt16LE((nIn - 1) * 2);
    } else {
      const a = usable.readInt16LE(j * 2);
      const b = usable.readInt16LE((j + 1) * 2);
      // Math.trunc, not Math.floor: Python's int() truncates toward zero, and
      // interpolating between a negative and a less-negative sample makes the
      // difference visible.
      value = Math.trunc(a + (b - a) * (pos - j));
    }
    out.writeInt16LE(value, i * 2);
  }
  return out;
}

/**
 * Chops arbitrary-length PCM buffers into whole wire frames, carrying the
 * remainder.
 *
 * Resampled 24 kHz deltas do not divide evenly into the wire's 640-byte frame,
 * so without a residual the leftover bytes are dropped and every turn loses a
 * few milliseconds at the seams. Over a call that is audible as clipped word
 * endings.
 *
 * ```ts
 * const aligner = new FrameAligner();
 * for (const chunk of providerAudio) {          // arbitrary lengths
 *   for (const frame of aligner.push(chunk)) {  // whole 640-byte frames
 *     await session.sendAudio(frame);
 *   }
 * }
 * const tail = aligner.flush();                 // end of turn
 * if (tail) await session.sendAudio(tail);
 * ```
 */
export class FrameAligner {
  readonly #frameBytes: number;
  #buf: Buffer = Buffer.alloc(0);

  constructor(frameBytes: number = FRAME_BYTES) {
    this.#frameBytes = frameBytes;
  }

  /** Bytes held back, waiting for a whole frame. */
  get pending(): number {
    return this.#buf.length;
  }

  /** Add a buffer; return whatever whole frames are now available. */
  push(pcm: Buffer): Buffer[] {
    this.#buf =
      this.#buf.length === 0
        ? Buffer.from(pcm)
        : Buffer.concat([this.#buf, pcm]);
    const out: Buffer[] = [];
    while (this.#buf.length >= this.#frameBytes) {
      out.push(this.#buf.subarray(0, this.#frameBytes));
      this.#buf = this.#buf.subarray(this.#frameBytes);
    }
    return out;
  }

  /**
   * Zero-pad and return the residual at end of turn, or undefined when empty.
   *
   * Padding rather than dropping: the tail of the last word matters more than a
   * few milliseconds of silence.
   */
  flush(): Buffer | undefined {
    if (this.#buf.length === 0) return undefined;
    const tail = Buffer.concat([this.#buf], this.#frameBytes);
    this.#buf = Buffer.alloc(0);
    return tail;
  }

  /**
   * Drop the residual without emitting it - use on a barge-in, where the
   * held-back bytes belong to a turn the caller just interrupted.
   */
  reset(): void {
    this.#buf = Buffer.alloc(0);
  }
}

/**
 * Root-mean-square amplitude of PCM16 mono little-endian, normalised to 0.0 - 1.0.
 *
 * How loud a frame is, which is what an echo guard, a barge-in check and a voice
 * segmenter each need. It lives here because all three want it and every plugin
 * that wanted it had been writing it again.
 *
 * Dependency-free for the same reason the resampler above is: this runs on every
 * inbound frame of every call.
 *
 * An odd trailing byte is dropped rather than throwing. A truncated frame is a
 * glitch; an exception in the audio path is a dropped call.
 */
export function pcm16Rms(pcm: Buffer): number {
  const samples = Math.floor(pcm.length / 2);
  if (samples === 0) return 0;
  let sum = 0;
  for (let i = 0; i < samples; i += 1) {
    const sample = pcm.readInt16LE(i * 2) / 32768;
    sum += sample * sample;
  }
  return Math.sqrt(sum / samples);
}
