// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Showing the model what the caller is showing, without being asked.
 *
 * `VisionTools` answers when a model reaches for `look`. That is the right shape
 * most of the time, and it has a blind spot: the model has to know there is
 * something to look at. Somebody who shares a deck and says "what do you think?"
 * has told it nothing it can act on.
 *
 * Ambient vision closes that by pushing what changed on screen into the
 * conversation between turns. It is OFF unless a plugin turns it on, because it
 * spends money on every scene change and not every deployment wants that.
 *
 * Three things keep it from being expensive or creepy:
 *
 * **A recording gate**, checked before a frame is even stored. Sending
 * somebody's screen to a model continuously is a different promise from
 * glancing at it once, and the recording is what told them their call is being
 * kept.
 *
 * **Change detection**, so a screen nobody touched costs nothing. The latch is
 * the digest of the last frame actually DELIVERED for that source, not the last
 * one seen: a delivery that failed has to be retried, not skipped.
 *
 * **A reserve**, so the ambient lane cannot spend the whole budget and leave the
 * caller's own "look at this" with nothing left.
 *
 * What to DO with an image stays in the plugin, because only it knows how to
 * hand one to its provider without forcing a reply.
 *
 * Identical in shape to the Python SDK's `standin.ambient`.
 */

import { logger } from "./log.js";
import {
  fallbackOwner,
  frameCaption,
  frameDigest,
  frameOwner,
  type VideoFrame,
} from "./vision.js";
import { VisionBudget } from "./visionTools.js";

/**
 * How often to look again even when no frame arrived. A screen share can go
 * quiet without ending, and the last thing on it is still what is being
 * discussed.
 */
export const AMBIENT_BACKSTOP_MS = 6_000;

/**
 * Images held while the provider's socket is not up yet. Bounded: a sink that
 * never comes up would otherwise hold the whole call's video.
 */
export const MAX_QUEUED_AMBIENT_IMAGES = 6;

/**
 * Screen share first. Somebody presenting is nearly always talking about the
 * screen rather than about their face.
 */
export const AMBIENT_SOURCE_ORDER = ["screenshare", "camera"] as const;

/** The default ceiling when a plugin gives no budget of its own. */
export const DEFAULT_AMBIENT_MAX_PER_MINUTE = 30;

/** One frame, ready to hand to a provider, with who it came from. */
export interface AmbientImage {
  readonly source: string;
  readonly mime: string;
  readonly dataBase64: string;
  readonly width: number;
  readonly height: number;
  readonly ts: number;
  /** Who is showing it. Degrades to "the caller" rather than vanishing. */
  readonly owner: string;
  /** A sentence to put beside the image, so the model knows whose screen it is. */
  readonly caption: string;
}

/** The `data:` form most vision APIs take directly. */
export function ambientImageDataUrl(image: AmbientImage): string {
  return `data:${image.mime};base64,${image.dataBase64}`;
}

/**
 * Hand one image to the provider.
 *
 * It MUST NOT make the agent reply: ambient vision is context, and an agent that
 * answers every scene change talks over the person presenting. It MUST reject on
 * failure, because a silent failure latches a frame that never arrived and the
 * model never sees that screen again.
 */
export type AmbientSink = (image: AmbientImage) => Promise<void>;

/** What a session has to offer for ambient vision to gate on it. */
export interface AmbientSession {
  readonly recordingActive: boolean;
}

/** Options for {@link AmbientVision}. */
export interface AmbientVisionOptions {
  enabled?: boolean;
  budget?: VisionBudget;
  requireRecording?: boolean;
  sinkReady?: () => boolean;
  changeKey?: (frame: VideoFrame) => string;
  onDelivered?: (image: AmbientImage) => void;
  backstopMs?: number;
  queueMax?: number;
}

/**
 * Push what changed on screen into the conversation, between turns.
 *
 * Built by a plugin that knows how to deliver an image without forcing a reply.
 * {@link offer} is synchronous and never blocks: it runs on the receive path of
 * a live call, and the vision work happens off it.
 */
export class AmbientVision {
  readonly #session: AmbientSession;
  readonly #deliver: AmbientSink;
  readonly #enabled: boolean;
  readonly #requireRecording: boolean;
  readonly #sinkReady: (() => boolean) | undefined;
  readonly #changeKey: (frame: VideoFrame) => string;
  readonly #onDelivered: ((image: AmbientImage) => void) | undefined;
  readonly #backstopMs: number;
  readonly #queueMax: number;
  readonly #budget: VisionBudget;

  readonly #latest = new Map<string, VideoFrame>();
  readonly #latched = new Map<string, string>();
  #queue: AmbientImage[] = [];
  #flushing = false;
  #dirty = false;
  #released = false;
  #delivered = 0;
  #saidHolding = false;
  #backstop: NodeJS.Timeout | undefined;

  constructor(
    session: AmbientSession,
    deliver: AmbientSink,
    options: AmbientVisionOptions = {},
  ) {
    this.#session = session;
    this.#deliver = deliver;
    this.#enabled = options.enabled ?? false;
    this.#requireRecording = options.requireRecording ?? true;
    this.#sinkReady = options.sinkReady;
    this.#changeKey =
      options.changeKey ?? ((frame) => frameDigest(frame.dataBase64));
    this.#onDelivered = options.onDelivered;
    this.#backstopMs = Math.max(500, options.backstopMs ?? AMBIENT_BACKSTOP_MS);
    this.#queueMax = Math.max(1, options.queueMax ?? MAX_QUEUED_AMBIENT_IMAGES);

    if (options.budget === undefined) {
      this.#budget = new VisionBudget(DEFAULT_AMBIENT_MAX_PER_MINUTE);
      logger.debug(
        "standin: ambient vision has its own budget, so ambient and explicit looks " +
          "are capped separately",
      );
    } else {
      this.#budget = options.budget;
      if (this.#enabled && options.budget.maxPerMinute === 0) {
        logger.warn(
          "standin: ambient vision is on with an uncapped budget; every scene change " +
            "will be charged. Set maxPerMinute, or leave enabled off.",
        );
      }
    }
  }

  /** Images held because the provider was not ready for them. */
  get queued(): number {
    return this.#queue.length;
  }

  get delivered(): number {
    return this.#delivered;
  }

  /** Take one frame. Synchronous, non-blocking, and never throws. */
  offer(frame: VideoFrame): void {
    if (!this.#enabled || this.#released) return;
    if (this.#requireRecording && !this.#session.recordingActive) {
      // Gated before it is STORED, not just before it is sent. Keeping a frame
      // captured while the gate was shut would let opening the gate surface
      // something from before the caller was told.
      return;
    }
    this.#latest.set(frame.source, frame);
    this.#armBackstop();
    this.flush();
  }

  /** Look now. Idempotent, and safe to call from anywhere. */
  flush(): void {
    if (!this.#enabled || this.#released) return;
    if (this.#flushing) {
      // One more pass after this one, rather than two at once.
      this.#dirty = true;
      return;
    }
    this.#flushing = true;
    // Scheduled, not started here. Calling an async function runs its body up to
    // the first await SYNCHRONOUSLY, so a pass begun inside offer() would see
    // only the frames offered so far and deliver them out of order. It would
    // also do vision work on the receive path, which offer() promises not to.
    void Promise.resolve().then(() => this.#run());
  }

  /** Stop, and stay stopped. */
  async close(): Promise<void> {
    this.#released = true;
    this.#latest.clear();
    this.#latched.clear();
    this.#queue = [];
    clearInterval(this.#backstop);
    this.#backstop = undefined;
  }

  async #run(): Promise<void> {
    try {
      for (;;) {
        await this.#pass();
        if (!this.#dirty || this.#released) return;
        this.#dirty = false;
      }
    } catch (err) {
      logger.error(`standin: the ambient vision pass failed: ${String(err)}`);
    } finally {
      this.#flushing = false;
    }
  }

  async #pass(): Promise<void> {
    if (this.#released) return;
    const ready = this.#sinkReady === undefined || this.#sinkReady();
    if (ready && this.#queue.length > 0) {
      // What was held goes first, oldest first: the model should see the screen
      // change in the order it happened.
      await this.#drain();
    }

    for (const source of AMBIENT_SOURCE_ORDER) {
      if (this.#released) return;
      const frame = this.#latest.get(source);
      if (frame === undefined) continue;
      const key = this.#changeKey(frame);
      // Nothing changed. A frozen screen costs nothing.
      if (this.#latched.get(source) === key) continue;

      const token = this.#budget.tryConsumeAmbient();
      // Out of the ambient allowance. Stop the pass rather than trying the next
      // source, which would spend the same exhausted budget.
      if (token === undefined) return;

      const image = this.#image(frame);
      if (!ready) {
        // Charged and latched: it is going to be sent, just not yet. Refunding
        // here would re-send the same screen when the sink comes up.
        this.#latched.set(source, key);
        this.#hold(image);
        continue;
      }
      try {
        await this.#deliver(image);
      } catch (err) {
        // The latch is untouched, so the same screen is tried again.
        this.#budget.refund(token);
        logger.debug(
          `standin: an ambient frame did not reach the model: ${String(err)}`,
        );
        continue;
      }
      if (this.#released) return;
      this.#latched.set(source, key);
      this.#delivered += 1;
      this.#note(image);
    }
  }

  async #drain(): Promise<void> {
    const held = this.#queue;
    this.#queue = [];
    for (const image of held) {
      if (this.#released) return;
      try {
        await this.#deliver(image);
      } catch (err) {
        // Already charged and already latched. Dropped rather than re-queued,
        // or a dead sink grows the queue for ever.
        logger.debug(
          `standin: a held ambient frame was dropped: ${String(err)}`,
        );
        continue;
      }
      this.#delivered += 1;
      this.#note(image);
    }
  }

  #note(image: AmbientImage): void {
    if (this.#onDelivered === undefined) return;
    try {
      this.#onDelivered(image);
    } catch {
      // A bookkeeping hook must not fail a delivery that already happened.
    }
  }

  #hold(image: AmbientImage): void {
    if (!this.#saidHolding) {
      logger.info(
        "standin: holding ambient frames until the provider is ready",
      );
      this.#saidHolding = true;
    }
    this.#queue.push(image);
    // Oldest out: what is on screen NOW is worth more than what was.
    if (this.#queue.length > this.#queueMax) {
      this.#queue = this.#queue.slice(-this.#queueMax);
    }
  }

  #image(frame: VideoFrame): AmbientImage {
    const owner = frameOwner(frame) ?? fallbackOwner(frame.source);
    return {
      source: frame.source,
      mime: frame.mime,
      dataBase64: frame.dataBase64,
      width: frame.width,
      height: frame.height,
      ts: frame.ts,
      owner,
      caption: frameCaption(owner),
    };
  }

  /** Armed on the first accepted frame, so a call with no video has no timer. */
  #armBackstop(): void {
    if (this.#backstop !== undefined || this.#released) return;
    this.#backstop = setInterval(() => this.flush(), this.#backstopMs);
    this.#backstop.unref?.();
  }
}
