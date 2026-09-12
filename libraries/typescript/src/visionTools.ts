// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What an agent can do about what it can see, and what it shows back.
 *
 * `vision.ts` is the lane: frames in, images out. This is the layer a model
 * actually reaches for, and it exists in the core rather than in one plugin
 * because every provider wants the same things and none of them wants to write
 * the guards again.
 *
 * None of these throw at a model. Every one returns a sentence, because the
 * caller is a tool result being read back to something that will say it out
 * loud, and an exception there is a silent tool and a confused agent.
 *
 * Identical in shape to the Python SDK's `standin.vision_tools`.
 */

import { randomUUID } from "node:crypto";

import { assertPublicHttpUrl, fetchPublicImage } from "./fetch.js";
import type { CallSession } from "./handler.js";
import { logger } from "./log.js";
import {
  DISPLAY_IMAGE_MIME_TYPES,
  MAX_IMAGE_BYTES,
  type DisplayImageMode,
  type FrameDescriber,
  type VideoFrame,
  type VideoSource,
  frameDigest,
} from "./vision.js";

/** How long an agent-supplied URL has to produce an image. */
const FETCH_TIMEOUT_MS = 10_000;

/**
 * A caption reaches the caller's screen, and a model's strings are as long as
 * whoever is steering it wants them to be.
 */
const MAX_CAPTION_CHARS = 200;

/** The only two ways a picture can be put on the tile. */
export const DISPLAY_MODES = ["fullscreen", "overlay"] as const;

/** How long each picture of a slideshow stays up before the next one. */
export const SLIDESHOW_HOLD_MS = 4_000;

/**
 * How much longer than the gap each picture is held for.
 *
 * Without it the tile blanks for the moment between one picture expiring and
 * the next arriving, and the caller sees a flicker rather than a slideshow.
 */
export const SLIDESHOW_OVERLAP_MS = 500;

/**
 * How many pictures one slideshow may hold.
 *
 * A model handed a folder will pass the whole folder. Ten at four seconds is
 * already most of a minute of a live call spent looking at pictures.
 */
export const MAX_SLIDESHOW_IMAGES = 10;

const MIN_HOLD_MS = 1_000;

/** How long a page has to render before the caller is told it did not. */
export const PAGE_RENDER_TIMEOUT_MS = 45_000;

/**
 * How long a rendered page stays on the tile. Longer than a chart: a page is
 * read rather than glanced at.
 */
export const PAGE_DISPLAY_MS = 15_000;

/** A URL makes a poor caption at any length, and a long one makes a worse one. */
const MAX_PAGE_CAPTION_CHARS = 80;
const MAX_HOLD_MS = 30_000;

/**
 * A filename safe to show somebody: a name, a dot, a short extension, and no
 * separators of any kind.
 */
const SAFE_NAME = /^[\w.-]{1,80}\.[A-Za-z0-9]{2,5}$/;

/**
 * The display mode this value means, or the default.
 *
 * One rule, shared by both SDKs and every plugin, because the value comes from
 * a model: `"pip"`, `"inset"`, `"full"` and nothing at all are all things a
 * model will say, and none of them is a mode.
 */
export function normalizeDisplayMode(
  value: unknown,
  fallback?: DisplayImageMode,
): DisplayImageMode | undefined {
  if (typeof value === "string") {
    const cleaned = value.trim().toLowerCase();
    if ((DISPLAY_MODES as readonly string[]).includes(cleaned))
      return cleaned as DisplayImageMode;
  }
  return fallback;
}

/**
 * A filename to show beside a picture.
 *
 * Taken from the source when it looks like a filename and nothing else. The
 * string came from a model steered by whoever is on the call, and it is about
 * to be shown to them.
 */
export function displayImageName(pathOrUrl: string, mime: string): string {
  const raw = String(pathOrUrl ?? "")
    .split("?")[0]!
    .split("#")[0]!;
  const candidate = raw.replace(/\\/g, "/").split("/").pop()!.trim();
  if (SAFE_NAME.test(candidate)) return candidate;
  const subtype = (mime || "").split("/").pop() || "bin";
  return `image.${subtype === "jpeg" ? "jpg" : subtype}`;
}

/** The last picture the caller actually saw. */
export class ShownImage {
  constructor(
    readonly image: Buffer | string,
    readonly mime: string,
    readonly name: string,
    readonly atMs: number,
  ) {}

  asBase64(): string {
    return typeof this.image === "string"
      ? this.image
      : this.image.toString("base64");
  }
}

/**
 * Renders one page to bytes plus a mime type. Supplied by a plugin whose host
 * already runs a browser; the core never gains one.
 */
export type PageRenderer = (
  url: string,
) => Promise<{ bytes: Buffer | string; mime: string }>;

/** Told apart from a renderer's own failure, so the sentence can name the cause. */
const TIMED_OUT = Symbol("standin.pageRenderTimeout");

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
    // The renderer keeps running; nothing here can cancel a host browser. The
    // timer is what must not keep the process alive after the call has moved on.
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** One picture in a slideshow. Bytes, base64, or an https URL. */
export interface ShowItem {
  image: Buffer | string;
  mime?: string;
  name?: string;
}

/**
 * A ceiling on how often one call may spend on vision.
 *
 * A model that can look can look in a loop, and each look is a paid inference
 * over somebody's screen. A sliding window rather than a total, so a long call
 * is not punished for having been long.
 *
 * Spending returns a token and refunding takes that token back. Two tool calls
 * can overlap, and a refund that simply dropped "the most recent charge" would
 * refund the wrong one and let the budget drift upward under exactly the load it
 * exists to bound.
 */
export class VisionBudget {
  readonly maxPerMinute: number;
  #spent = new Map<string, number>();

  constructor(maxPerMinute = 6) {
    this.maxPerMinute = Math.max(0, maxPerMinute);
  }

  /** Take one look's worth of budget, or undefined when there is none left. */
  tryConsume(): string | undefined {
    if (this.maxPerMinute === 0) return randomUUID();
    const now = Date.now();
    for (const [token, at] of this.#spent) {
      if (at <= now - 60_000) this.#spent.delete(token);
    }
    if (this.#spent.size >= this.maxPerMinute) return undefined;
    const token = randomUUID();
    this.#spent.set(token, now);
    return token;
  }

  /**
   * How much of the window only an explicit look may spend.
   *
   * Ambient vision spends on every scene change, which is exactly the load that
   * would leave a caller's own "look at this" with nothing left. The reserve is
   * what the ambient lane cannot touch.
   */
  get reserve(): number {
    if (!this.maxPerMinute) return 0;
    return Math.max(2, Math.floor(this.maxPerMinute / 4));
  }

  /**
   * Take one look's worth, from the ambient lane only.
   *
   * Refused once the window is down to the reserve. Refunded through the same
   * {@link refund}, with the same token, so a failed ambient push and a failed
   * explicit look are given back the same way.
   */
  tryConsumeAmbient(): string | undefined {
    if (!this.maxPerMinute) return this.tryConsume();
    if (this.spent >= this.maxPerMinute - this.reserve) return undefined;
    return this.tryConsume();
  }

  /** Give back a charge whose look never happened. Idempotent. */
  refund(token: string): void {
    this.#spent.delete(token);
  }

  /** Looks charged in the current window. */
  get spent(): number {
    const cutoff = Date.now() - 60_000;
    return [...this.#spent.values()].filter((at) => at > cutoff).length;
  }
}

/**
 * A short history of what the caller showed.
 *
 * The call session keeps the LATEST frame per source, which answers "what am I
 * looking at now". This answers "what was on that slide a moment ago".
 *
 * Bounded, and **gated on the call being recorded**. Keeping a history of
 * somebody's screen is a materially different promise from glancing at it once,
 * and the recording is the thing that told them their call is being kept.
 */
export class KeyframeStore {
  readonly #capacity: number;
  #frames: VideoFrame[] = [];

  readonly #last = new Map<string, string>();

  constructor(capacity = 16) {
    this.#capacity = Math.max(1, capacity);
  }

  /** Keep this frame, if the call is being recorded. Returns whether it was kept. */
  offer(frame: VideoFrame, recording: boolean): boolean {
    if (!recording) return false;
    // Per source, so an alternating camera and screen share each keep their own
    // history. A screen nobody touched would otherwise fill the whole store
    // with one picture.
    const digest = frameDigest(frame.dataBase64);
    if (this.#last.get(frame.source) === digest) return false;
    this.#last.set(frame.source, digest);
    this.#frames.push(frame);
    if (this.#frames.length > this.#capacity) {
      this.#frames = this.#frames.slice(-this.#capacity);
    }
    return true;
  }

  /** Frames kept so far, oldest first. */
  recent(source?: VideoSource): VideoFrame[] {
    return source === undefined
      ? [...this.#frames]
      : this.#frames.filter((f) => f.source === source);
  }

  /** Forget everything. Called on teardown. */
  clear(): void {
    this.#frames = [];
    this.#last.clear();
  }

  get size(): number {
    return this.#frames.length;
  }
}

/** One beat of a walkthrough: something to say, optionally something to show. */
export interface WalkthroughStep {
  /** The line spoken before the image appears. */
  say: string;
  image?: Buffer | string;
  mime?: string;
  caption?: string;
}

/**
 * Says one line and resolves when the caller has heard it. Supplied by the
 * plugin, because "finished speaking" is a thing only the provider knows.
 */
export type Speaker = (text: string) => Promise<void>;

/** Options for {@link VisionTools}. */
export interface VisionToolsOptions {
  describer?: FrameDescriber;
  budget?: VisionBudget;
  keyframes?: KeyframeStore;
  /**
   * What to use when the model says nothing. Left out, no mode is sent at all,
   * so the service's own default applies rather than one chosen here.
   */
  defaultDisplayMode?: string;
}

/**
 * The capabilities, bound to one call.
 *
 * Every method returns a sentence for a model to read out, including when it
 * failed. None of them throw.
 */
export class VisionTools {
  readonly #session: CallSession;
  readonly #describer: FrameDescriber | undefined;
  readonly budget: VisionBudget;
  readonly keyframes: KeyframeStore;
  readonly #defaultDisplay: DisplayImageMode | undefined;
  #lastShown: ShownImage | undefined;
  #slideshow: Promise<void> | undefined;
  #wake: (() => void) | undefined;
  #generation = 0;

  constructor(session: CallSession, options: VisionToolsOptions = {}) {
    this.#session = session;
    this.#describer = options.describer;
    this.budget = options.budget ?? new VisionBudget();
    this.keyframes = options.keyframes ?? new KeyframeStore();
    this.#defaultDisplay = normalizeDisplayMode(options.defaultDisplayMode);
  }

  /**
   * The picture the caller can see, if any.
   *
   * Recorded only after a send actually returned, so "send me that" attaches
   * what they saw rather than what was attempted. One slot, replaced each time:
   * a list would be a growing copy of everything shown on the call.
   */
  get lastShown(): ShownImage | undefined {
    return this.#lastShown;
  }

  /** The slideshow now running, if any. Await it to let one finish. */
  get slideshow(): Promise<void> | undefined {
    return this.#slideshow;
  }

  /** Forget what was shown and stop any slideshow. Call this on teardown. */
  async reset(): Promise<void> {
    await this.#stopSlideshow();
    this.#lastShown = undefined;
    this.keyframes.clear();
  }

  /**
   * Answer a question about what the caller is showing.
   *
   * Uses the newest frame, preferring the screen share, because an agent asked
   * to look is nearly always being asked about what is being shown rather than
   * who is showing it.
   */
  async look(question = "", source?: string): Promise<string> {
    if (this.#describer === undefined) {
      return (
        "looking is not available on this deployment: no vision model is configured " +
        "(set STANDIN_VISION_API_URL and STANDIN_VISION_MODEL)"
      );
    }
    const wanted =
      source === "camera" || source === "screenshare"
        ? (source as VideoSource)
        : undefined;
    const frame = this.#session.latestVideoFrame(wanted);
    if (frame === undefined) {
      return "there is nothing to look at: the caller is not sharing their camera or screen";
    }
    const token = this.budget.tryConsume();
    if (token === undefined) {
      return (
        "this call has reached its limit on looking at the screen; " +
        "ask the caller to describe what they are showing"
      );
    }
    try {
      return await this.#describer.describe(
        frame,
        question.trim() || "Describe what is visible.",
      );
    } catch (err) {
      // The charge is given back, or a flaky vision endpoint silently burns the
      // budget the caller paid nothing for.
      this.budget.refund(token);
      return `could not look at the screen: ${String(err)}`;
    }
  }

  /**
   * Answer about a frame the caller has already moved past.
   *
   * Only possible when the call is being recorded, because that is the only time
   * frames are kept at all.
   */
  async lookBack(question = ""): Promise<string> {
    const frames = this.keyframes.recent();
    if (frames.length === 0) {
      if (!this.#session.recordingActive) {
        return "I can only look back at earlier screens while the call is being recorded, and it is not";
      }
      return "nothing has been shown on this call yet";
    }
    if (this.#describer === undefined) {
      return "looking is not available on this deployment: no vision model is configured";
    }
    const token = this.budget.tryConsume();
    if (token === undefined)
      return "this call has reached its limit on looking at the screen";
    try {
      return await this.#describer.describe(
        frames[frames.length - 1]!,
        question.trim() || "Describe what was visible.",
      );
    } catch (err) {
      this.budget.refund(token);
      return `could not look back: ${String(err)}`;
    }
  }

  /** Put an image on the bot's video tile. */
  async show(
    image: Buffer | string,
    mime = "image/jpeg",
    caption?: string,
    durationMs?: number,
    display?: string,
    name?: string,
  ): Promise<string> {
    if (!(DISPLAY_IMAGE_MIME_TYPES as readonly string[]).includes(mime)) {
      return `that image is ${mime}; it must be one of ${DISPLAY_IMAGE_MIME_TYPES.join(", ")}`;
    }
    try {
      await this.#session.displayImage(image, {
        mime,
        durationMs,
        // The model's choice of fullscreen or overlay, and anything else falls
        // back to the configured default. When neither is set the field is
        // OMITTED rather than defaulted here, so the service decides.
        mode: normalizeDisplayMode(display, this.#defaultDisplay),
        caption: caption ? caption.slice(0, MAX_CAPTION_CHARS) : undefined,
      });
    } catch (err) {
      // The wire has a hard ceiling. A model must be told it in words, not by an
      // exception it cannot see.
      return `could not show that: ${String(err)}`;
    }
    // Only after it actually went. "Now send me that" must attach what the
    // caller saw, not what was attempted.
    this.#lastShown = new ShownImage(
      image,
      mime,
      name ?? displayImageName("", mime),
      Date.now(),
    );
    return "the caller can see it";
  }

  /**
   * Fetch an image the model chose, and show it.
   *
   * The URL comes from a model steered by whoever is on the call, so it goes
   * through the SDK's guard: public hosts only, and the address re-checked at
   * connect time.
   */
  async showUrl(
    url: string,
    caption?: string,
    display?: string,
  ): Promise<string> {
    if (!url.trim()) return "that needs a public https URL of a jpeg or png";
    let fetched;
    try {
      fetched = await fetchPublicImage(url, MAX_IMAGE_BYTES, FETCH_TIMEOUT_MS);
    } catch (err) {
      return `could not fetch that image: ${String(err)}`;
    }
    return await this.show(
      fetched.bytes,
      fetched.mime,
      caption,
      undefined,
      display,
      displayImageName(url, fetched.mime),
    );
  }

  /**
   * Put a web page on the tile, as a picture of it.
   *
   * The core has no browser and must never gain one. `render` is supplied by a
   * plugin whose host already runs one, and it returns bytes rather than a
   * path: reading a file chosen downstream of whoever is on the call is not a
   * primitive this belongs in.
   *
   * The guard runs HERE, before the renderer is reached, and it runs even when
   * that renderer is a browser advertising private-network protection of its
   * own. Such a browser assumes whoever wrote the URL already has a shell on
   * the machine. Here the URL was written by a model being steered by a
   * stranger, which is exactly the case that relaxation lets through.
   */
  async showPage(
    url: string,
    caption?: string,
    render?: PageRenderer,
    timeoutMs = PAGE_RENDER_TIMEOUT_MS,
  ): Promise<string> {
    if (!url.trim()) return "that needs a public https URL of a page";
    if (render === undefined)
      return "showing web pages is not available on this deployment";
    try {
      await assertPublicHttpUrl(url);
    } catch (err) {
      return `could not open that page: ${String(err)}`;
    }
    let rendered;
    try {
      rendered = await withTimeout(render(url), timeoutMs);
    } catch (err) {
      if (err === TIMED_OUT) {
        // Never "within 0 seconds", and never "1 seconds": this sentence is
        // read out loud to the person waiting for the page.
        const seconds = Math.max(1, Math.round(timeoutMs / 1000));
        return `that page did not finish loading within ${seconds} ${
          seconds === 1 ? "second" : "seconds"
        }`;
      }
      return `could not open that page: ${String(err)}`;
    }
    return await this.show(
      rendered.bytes,
      rendered.mime,
      caption ?? url.trim().slice(0, MAX_PAGE_CAPTION_CHARS),
      PAGE_DISPLAY_MS,
      undefined,
      displayImageName(url, rendered.mime),
    );
  }

  /**
   * Show several pictures in turn, without waiting for all of them.
   *
   * The FIRST one goes before this resolves, so the model can say "here it is"
   * and be right. The rest are paced from a detached chain: a model that waits
   * out a ten-picture slideshow before speaking leaves the caller in silence for
   * most of a minute.
   *
   * Never throws. The sentence says what is on screen now and what follows.
   */
  async showMany(
    items: readonly ShowItem[],
    caption?: string,
    display?: string,
    holdMs = SLIDESHOW_HOLD_MS,
  ): Promise<string> {
    if (items.length === 0) return "there was nothing to show";
    await this.#stopSlideshow();

    const hold = Math.max(MIN_HOLD_MS, Math.min(MAX_HOLD_MS, holdMs));
    const shown = items.slice(0, MAX_SLIDESHOW_IMAGES);
    const dropped = items.length - shown.length;
    const mode = normalizeDisplayMode(display, this.#defaultDisplay);

    const first = await this.#showItem(
      shown[0]!,
      caption,
      mode,
      shown.length > 1,
      hold,
    );
    if (first.startsWith("could not")) return first;

    if (shown.length > 1) {
      const generation = ++this.#generation;
      this.#slideshow = this.#pace(shown.slice(1), mode, hold, generation);
    }
    const of = shown.length > 1 ? `the first of ${shown.length}` : "it";
    const rest =
      shown.length > 1
        ? `; the rest follow every ${Math.floor(hold / 1000)} seconds`
        : "";
    const extra =
      dropped > 0
        ? ` (showing the first ${shown.length} of ${items.length})`
        : "";
    return `the caller can see ${of}${rest}${extra}`;
  }

  /** One frame of a slideshow, loaded late and sent. */
  async #showItem(
    item: ShowItem,
    caption: string | undefined,
    mode: DisplayImageMode | undefined,
    more: boolean,
    hold: number,
  ): Promise<string> {
    let image = item.image;
    let mime = item.mime ?? "image/jpeg";
    if (typeof image === "string" && /^https?:\/\//i.test(image.trim())) {
      const fetched = await fetchPublicImage(
        image,
        MAX_IMAGE_BYTES,
        FETCH_TIMEOUT_MS,
      );
      image = fetched.bytes;
      mime = fetched.mime;
    }
    return await this.show(
      image,
      mime,
      caption,
      // Held a little past the pacing gap, so the tile never blanks between
      // pictures. The LAST frame omits it, so the service's own default applies
      // to what stays on screen.
      more ? hold + SLIDESHOW_OVERLAP_MS : undefined,
      mode,
      item.name,
    );
  }

  /**
   * Send the remaining frames, one hold apart.
   *
   * Each is loaded inside this loop rather than up front, so a slideshow of ten
   * URLs does not fetch all ten before the first appears.
   */
  async #pace(
    rest: readonly ShowItem[],
    mode: DisplayImageMode | undefined,
    hold: number,
    generation: number,
  ): Promise<void> {
    for (const [index, item] of rest.entries()) {
      await this.#hold(hold);
      if (generation !== this.#generation) return; // a newer slideshow, or reset()
      try {
        const result = await this.#showItem(
          item,
          undefined,
          mode,
          index < rest.length - 1,
          hold,
        );
        if (result.startsWith("could not")) {
          // One bad picture skips that picture, never the rest.
          logger.debug(`standin: a slideshow frame was skipped: ${result}`);
        }
      } catch (err) {
        logger.debug(`standin: a slideshow frame was skipped: ${String(err)}`);
      }
    }
  }

  /**
   * The gap between two pictures, woken early when the slideshow is replaced.
   *
   * Without the wake, replacing a slideshow would wait out the old one's gap -
   * up to thirty seconds - before the new first picture went up.
   */
  async #hold(ms: number): Promise<void> {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.#wake = undefined;
        resolve();
      }, ms);
      this.#wake = () => {
        clearTimeout(timer);
        this.#wake = undefined;
        resolve();
      };
    });
  }

  async #stopSlideshow(): Promise<void> {
    this.#generation += 1;
    const running = this.#slideshow;
    this.#slideshow = undefined;
    // Woken rather than cancelled: the bumped generation is what stops it, and
    // a frame already being sent finishes being sent. Killing one mid-write
    // would leave half a picture on the wire.
    this.#wake?.();
    if (running !== undefined) await running.catch(() => undefined);
  }

  /**
   * Say and show several things in order, pausing for each.
   *
   * The pacing is here; the SPEAKING is not. Only the provider knows when a line
   * has finished being said, so `speak` is supplied by the plugin and awaited
   * before the next beat begins. Without that, a walkthrough talks over itself.
   *
   * `interrupted` is checked between beats. A caller who cuts in should stop the
   * tour, and the plugin is the only thing that knows they did.
   */
  async walkthrough(
    steps: readonly WalkthroughStep[],
    speak: Speaker,
    interrupted?: () => boolean,
    display?: string,
  ): Promise<string> {
    if (steps.length === 0) return "there was nothing to walk through";
    // One tile. A walkthrough and a slideshow running at once would fight over
    // it, and the caller would see neither properly.
    await this.#stopSlideshow();
    let shown = 0;
    for (const [index, step] of steps.entries()) {
      if (interrupted?.()) {
        return `stopped after ${shown} of ${steps.length}: the caller interrupted`;
      }
      try {
        await speak(step.say);
      } catch (err) {
        return `stopped at step ${index + 1}: ${String(err)}`;
      }
      if (step.image !== undefined) {
        const result = await this.show(
          step.image,
          step.mime ?? "image/jpeg",
          step.caption,
          undefined,
          display,
        );
        if (result.startsWith("could not"))
          return `stopped at step ${index + 1}: ${result}`;
      }
      shown = index + 1;
    }
    return `walked through all ${shown} steps`;
  }
}
