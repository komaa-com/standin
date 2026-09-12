// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Putting your agent's own face on the bot's video tile.
 *
 * StandIn renders an avatar by default. When your agent already produces video
 * of its own - an avatar worker, a rendered face, a camera - this streams that
 * onto the tile instead, as a continuous run of `display.frame` messages.
 *
 * The awkward part is not sending frames. It is sending them at a rate that does
 * not hurt the call, and that is what {@link TileStream} owns:
 *
 * **Latest wins, and each frame goes at most once.** Frames are offered into a
 * single slot, never a queue. A ticker takes whatever is newest and sends it.
 * That means a source producing faster than the wire drops the middle frames
 * rather than falling behind, and a source that STOPS producing goes quiet
 * rather than repeating one stale frame forever. Silence is how a stream ends.
 *
 * **The timestamp is the audio clock.** `ts` comes from
 * {@link CallSession.mediaTimeMs}, the same timeline the outbound audio is
 * stamped on. A wall clock keeps ticking through listening silence while the
 * audio clock does not, so a wall-clock stamp makes the two streams look like
 * they are drifting apart when they are in step.
 *
 * **Video yields to audio.** The budget here is tighter than the audio one on
 * purpose. Both streams share a socket, and a caller forgives a dropped frame
 * far more readily than a break in the voice.
 *
 * Encoding is yours to supply or ours to find. Pass an `encoder` and the stream
 * uses it; pass nothing and it looks for sharp. If neither is there, the tile
 * relay stays off with one line in the log and **the call is unaffected**.
 *
 * Identical in shape to the Python SDK's `standin.tile`.
 */

import type { CallSession } from "./handler.js";
import { logger } from "./log.js";

/**
 * The tile size frames are encoded to. Shipping an avatar's native resolution
 * only spends bandwidth on pixels the tile will not show.
 */
export const TILE_WIDTH = 640;
export const TILE_HEIGHT = 360;

/**
 * Encoder quality. Chosen where a talking head still looks right and the frame
 * still fits comfortably inside the wire envelope.
 */
const JPEG_QUALITY = 58;

/**
 * A sender-side sanity clamp, not a protocol limit. A talking-head tile gains
 * nothing above this, and a higher rate only spends local CPU on encoding and
 * base64.
 */
export const MAX_TILE_FPS = 20;

/** Tighter than the audio buffer cap, deliberately. See the module comment. */
const VIDEO_BACKPRESSURE_BYTES = 320 * 1024;

/** What an encoder is: packed RGB in, JPEG bytes out. */
export type Encoder = (
  rgb: Buffer,
  width: number,
  height: number,
) => Promise<Buffer>;

/**
 * Find an encoder, or return undefined having said why.
 *
 * sharp is an optional peer precisely because most deployments never put their
 * own video on the tile. A missing encoder is a tile relay that does not run,
 * which is a smaller problem than a dependency every install pays for.
 */
export async function jpegEncoder(): Promise<Encoder | undefined> {
  try {
    // A variable specifier, so the compiler does not require the optional
    // module to be present; it is resolved only at runtime when enabled.
    const specifier = "sharp";
    const mod = (await import(specifier)).default as unknown as (
      input: Buffer,
      opts: { raw: { width: number; height: number; channels: 3 } },
    ) => {
      resize(
        w: number,
        h: number,
        o: { fit: "fill" },
      ): {
        jpeg(o: { quality: number }): { toBuffer(): Promise<Buffer> };
      };
    };
    return async (rgb, width, height) =>
      mod(rgb, { raw: { width, height, channels: 3 } })
        .resize(TILE_WIDTH, TILE_HEIGHT, { fit: "fill" })
        .jpeg({ quality: JPEG_QUALITY })
        .toBuffer();
  } catch {
    logger.warn(
      "standin: the avatar tile relay needs sharp to encode frames (npm install sharp); " +
        "the relay is off and audio is unaffected",
    );
    return undefined;
  }
}

/** Options for {@link TileStream}. */
export interface TileStreamOptions {
  /** Frames per second, clamped to {@link MAX_TILE_FPS}. */
  fps?: number;
  /** Supply your own, or leave it and sharp is looked for. */
  encoder?: Encoder;
  /** Outbound bytes past which a frame is dropped rather than sent. */
  maxBufferedBytes?: number;
}

/**
 * A paced run of `display.frame` messages onto the bot's video tile.
 *
 * Built by a plugin that has video, driven by whatever produces it:
 *
 * ```ts
 * const tile = new TileStream(session, { fps: 12 });
 * await tile.start();
 * tile.offerRgb(rgb, width, height);   // as often as you like
 * await tile.aclose();
 * ```
 *
 * {@link offerRgb} and {@link offerJpeg} never block and never throw. They are
 * meant to be called from a drain loop that must not be slowed down by the wire.
 */
export class TileStream {
  readonly #session: CallSession;
  readonly #fps: number;
  readonly #periodMs: number;
  readonly #maxBuffered: number;
  #encoder: Encoder | undefined;
  #resolvedEncoder = false;
  /** The single newest frame awaiting a send. Never a queue. */
  #latest:
    { data: Buffer; width: number; height: number; jpeg: boolean } | undefined;
  #ticker: NodeJS.Timeout | undefined;
  #encoding = false;
  #closed = false;
  #sent = 0;
  #dropped = 0;
  #lastDropLog = 0;

  constructor(session: CallSession, options: TileStreamOptions = {}) {
    this.#session = session;
    this.#fps = Math.max(
      1,
      Math.min(Math.trunc(options.fps ?? 12), MAX_TILE_FPS),
    );
    this.#periodMs = Math.max(1, Math.round(1000 / this.#fps));
    this.#encoder = options.encoder;
    this.#maxBuffered = options.maxBufferedBytes ?? VIDEO_BACKPRESSURE_BYTES;
  }

  /** How many frames have reached the tile. */
  get framesSent(): number {
    return this.#sent;
  }

  /** How many were dropped for backpressure. A healthy call has some. */
  get framesDropped(): number {
    return this.#dropped;
  }

  /** Offer packed RGB. Replaces whatever was waiting. */
  offerRgb(rgb: Buffer, width: number, height: number): void {
    if (!this.#closed && rgb.length > 0) {
      this.#latest = { data: rgb, width, height, jpeg: false };
    }
  }

  /**
   * Offer an already-encoded frame, skipping the encoder entirely.
   *
   * For a source that hands you JPEG already. Nothing is re-encoded, and no
   * encoder needs to be installed.
   */
  offerJpeg(jpeg: Buffer, width = TILE_WIDTH, height = TILE_HEIGHT): void {
    if (!this.#closed && jpeg.length > 0) {
      this.#latest = { data: jpeg, width, height, jpeg: true };
    }
  }

  /** Begin sending. Returns once armed; the pacing runs in the background. */
  async start(): Promise<void> {
    if (this.#ticker !== undefined || this.#closed) return;
    if (this.#encoder === undefined && !this.#resolvedEncoder) {
      this.#resolvedEncoder = true;
      this.#encoder = await jpegEncoder();
    }
    this.#ticker = setInterval(() => void this.#tick(), this.#periodMs);
    this.#ticker.unref?.();
    logger.info(
      `standin: avatar tile relay armed at ${this.#fps} fps, ${TILE_WIDTH}x${TILE_HEIGHT}`,
    );
  }

  /** Stop sending. Safe to call twice, and on every teardown path. */
  async aclose(): Promise<void> {
    this.#closed = true;
    this.#latest = undefined;
    if (this.#ticker !== undefined) {
      clearInterval(this.#ticker);
      this.#ticker = undefined;
    }
  }

  async #tick(): Promise<void> {
    if (this.#closed || this.#latest === undefined || this.#encoding) return;
    const frame = this.#latest;
    // Consume the slot. Each offered frame is sent at most once, so a source
    // that goes quiet leaves a silent wire rather than a frozen repeat.
    this.#latest = undefined;
    if (this.#overBudget()) return;

    let data = frame.data;
    let { width, height } = frame;
    if (!frame.jpeg) {
      const encoder = this.#encoder;
      if (encoder === undefined) return;
      this.#encoding = true;
      try {
        data = await encoder(frame.data, frame.width, frame.height);
        width = TILE_WIDTH;
        height = TILE_HEIGHT;
      } catch (err) {
        logger.warn(`standin: encoding an avatar frame failed: ${String(err)}`);
        return;
      } finally {
        this.#encoding = false;
      }
      if (this.#closed) return;
      // Re-check after the encode yielded: audio may have filled the socket
      // while we were off the loop, and a video frame must not be what starves
      // the voice.
      if (this.#overBudget()) return;
    }

    this.#sent += 1;
    try {
      // The session owns the sequence and the timestamp, the same way it owns
      // them for audio, so the two streams share one clock.
      await this.#session.sendTileFrame(data, width, height);
    } catch {
      // A dying socket is the close handler's business, not the ticker's.
    }
  }

  #overBudget(): boolean {
    let buffered = 0;
    try {
      buffered = this.#session.bufferedBytes;
    } catch {
      buffered = 0;
    }
    if (buffered <= this.#maxBuffered) return false;
    this.#dropped += 1;
    const now = Date.now();
    if (now - this.#lastDropLog >= 5000) {
      logger.info(
        `standin: avatar tile is dropping frames to protect the audio (${this.#dropped} so far)`,
      );
      this.#lastDropLog = now;
    }
    return true;
  }
}
