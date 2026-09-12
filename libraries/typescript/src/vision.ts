// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The vision lane: what the caller shows you, and what you show back.
 *
 * A Microsoft Teams call carries more than voice. StandIn samples the caller's
 * camera and their screen share and forwards single JPEG frames, and it will
 * draw an image you send onto the bot's own tile. This module is both halves of
 * that: {@link parseVideoFrame} reads what arrives, {@link displayImage} builds
 * what goes back.
 *
 * Frames arrive **sparsely and best-effort**. StandIn drops a frame rather than
 * queueing it when the socket is busy, so this is not a video stream and must
 * not be treated as one. The useful shape is the one every provider plugin
 * ends up with: keep the latest frame per source and send it to a vision model
 * only when something asks to look. {@link CallSession.latestVideoFrame} does
 * that for you, so a plugin that only wants on-demand vision implements
 * no callback at all.
 *
 * {@link FrameDescriber} is the other way round, and the one most voice
 * providers need: a speech-to-speech model that hears but cannot see gets a
 * sentence of text instead of a picture. The frame goes to a vision model of
 * your choosing, transiently, and only the description comes back.
 *
 * Identical in shape to the Python SDK's `standin.vision`, translated to TS
 * naming.
 */

import { createHash } from "node:crypto";

import { encode } from "./protocolRuntime.js";
import { TYPE_DISPLAY_FRAME, TYPE_DISPLAY_IMAGE } from "./protocol.js";

/** The two things a caller can show: their camera, or their screen share. */
export const VIDEO_SOURCES = ["camera", "screenshare"] as const;

/** Which lane a frame came from. */
export type VideoSource = (typeof VIDEO_SOURCES)[number];

/** What StandIn will draw on the bot tile. JPEG or PNG, nothing else. */
export const DISPLAY_IMAGE_MIME_TYPES = ["image/jpeg", "image/png"] as const;

/** A MIME type StandIn will draw. */
export type DisplayImageMime = (typeof DISPLAY_IMAGE_MIME_TYPES)[number];

/** `"fullscreen"` replaces the tile; `"overlay"` draws a picture-in-picture inset. */
export type DisplayImageMode = "fullscreen" | "overlay";

/**
 * One wire message is bounded at 2 MB by both SDKs, and base64 costs a third on
 * top of the raw bytes. Refusing an oversized image here names the real problem,
 * rather than letting the service close the socket mid-call.
 */
export const MAX_IMAGE_BYTES = 1_400_000;

/**
 * One sampled frame of what the caller is showing.
 *
 * `participantId` and `participantName` are best-effort and absent for guest and
 * anonymous participants, so a group-call prompt that says who is sharing must
 * tolerate not knowing.
 */
export interface VideoFrame {
  /** Which lane this came from. */
  readonly source: VideoSource;
  /** Capture time in milliseconds. */
  readonly ts: number;
  /** Pixel width, already downscaled by StandIn before sending. */
  readonly width: number;
  /** Pixel height. */
  readonly height: number;
  /** Image MIME type. StandIn sends `image/jpeg`. */
  readonly mime: string;
  /**
   * The image, base64-encoded, exactly as it arrived.
   *
   * Kept in this form because it is the form most providers want back: a `data:`
   * URL for a vision model costs one template string from here, while
   * {@link VideoFrame.data} costs a decode.
   */
  readonly dataBase64: string;
  /** Whose frame this is, when StandIn could tell. */
  readonly participantId?: string;
  /** Display name matching {@link VideoFrame.participantId}. */
  readonly participantName?: string;
  /** The decoded image bytes, for an API that uploads a file. */
  readonly data: Buffer;
  /** The frame as a `data:` URL, which is what most vision models take. */
  readonly dataUrl: string;
}

function text(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed === "" ? undefined : trimmed;
}

function positiveInt(value: unknown): number | undefined {
  return typeof value === "number" && Number.isInteger(value) && value > 0
    ? value
    : undefined;
}

function decodeStrict(dataBase64: string): Buffer | undefined {
  const data = Buffer.from(dataBase64, "base64");
  // Buffer.from never throws on bad input, it silently discards what it cannot
  // read. Re-encoding is the only way to know the bytes about to reach a
  // provider really are the payload that was sent.
  return data.length > 0 && data.toString("base64") === dataBase64
    ? data
    : undefined;
}

/**
 * Read a `video.frame`, or return `undefined` if it is unusable.
 *
 * Never throws. A frame that fails any check is a frame to drop: the call is
 * healthy, the caller is still talking, and one malformed image is not worth
 * ending a conversation over. That is the same leniency the rest of the wire
 * contract is built on, where a receiver ignores what it cannot use.
 */
export function parseVideoFrame(
  msg: Record<string, unknown>,
): VideoFrame | undefined {
  const source = text(msg.source);
  if (source !== "camera" && source !== "screenshare") return undefined;

  const dataBase64 = msg.dataBase64;
  if (typeof dataBase64 !== "string" || dataBase64 === "") return undefined;
  const data = decodeStrict(dataBase64);
  if (data === undefined) return undefined;

  const width = positiveInt(msg.width);
  const height = positiveInt(msg.height);
  if (width === undefined || height === undefined) return undefined;

  const ts = msg.ts;
  const mime = text(msg.mime) ?? "image/jpeg";
  return {
    source,
    ts: typeof ts === "number" && Number.isInteger(ts) && ts >= 0 ? ts : 0,
    width,
    height,
    mime,
    dataBase64,
    participantId: text(msg.participantId),
    participantName: text(msg.participantName),
    data,
    dataUrl: `data:${mime};base64,${dataBase64}`,
  };
}

function encodeImage(
  image: Buffer | string,
  mime: string,
  label: string,
): string {
  if (!(DISPLAY_IMAGE_MIME_TYPES as readonly string[]).includes(mime)) {
    throw new Error(
      `${label} mime must be one of ${DISPLAY_IMAGE_MIME_TYPES.join(", ")}, got ${mime}`,
    );
  }
  let size: number;
  let dataBase64: string;
  if (typeof image === "string") {
    // Already base64: measure the decoded size, because that is what the 2 MB
    // envelope actually bounds.
    const decoded = decodeStrict(image);
    if (decoded === undefined)
      throw new Error(`${label} data is not valid base64`);
    size = decoded.length;
    dataBase64 = image;
  } else {
    size = image.length;
    dataBase64 = image.toString("base64");
  }
  if (size === 0) throw new Error(`${label} carries no image data`);
  if (size > MAX_IMAGE_BYTES) {
    throw new Error(
      `${label} is ${size} bytes, over the ${MAX_IMAGE_BYTES} limit`,
    );
  }
  return dataBase64;
}

/** Options for {@link displayImage}. */
export interface DisplayImageOptions {
  /** `image/jpeg` (the default) or `image/png`. */
  mime?: string;
  /** How long to show it. StandIn applies its own default when omitted. */
  durationMs?: number;
  /** `fullscreen` (StandIn's default) or `overlay`. */
  mode?: DisplayImageMode;
  /** Short label drawn along the bottom of the image. */
  caption?: string;
}

/**
 * Build a `display.image`: show the caller a still, then return to the avatar.
 *
 * `image` is a Buffer or an already-base64 string.
 */
export function displayImage(
  image: Buffer | string,
  options: DisplayImageOptions = {},
): string {
  const mime = options.mime ?? "image/jpeg";
  const message: Record<string, unknown> = {
    type: TYPE_DISPLAY_IMAGE,
    dataBase64: encodeImage(image, mime, "display.image"),
    mime,
    // The wire reserves a timeline anchor this lane does not use. Both SDKs
    // send 0 rather than one omitting it, so a single conformance vector
    // covers both and neither can drift.
    ts: 0,
  };
  if (options.durationMs !== undefined && options.durationMs > 0) {
    message.durationMs = options.durationMs;
  }
  if (options.mode) message.mode = options.mode;
  if (options.caption) message.caption = options.caption;
  return encode(message);
}

/** Options for {@link displayFrame}. */
export interface DisplayFrameOptions {
  /** Frame encoding; senders send `image/jpeg`. */
  mime?: string;
  /** Source pixel width (informational). */
  width?: number;
  /** Source pixel height (informational). */
  height?: number;
}

/**
 * Build a `display.frame`: one frame of continuous avatar video.
 *
 * Latest wins. There is no handshake, the first frames start the stream and
 * silence ends it, and a sender under backpressure MUST drop frames rather than
 * buffer them, exactly as it does for hot-path audio.
 *
 * `ts` belongs to the sender's own media timeline, the same one its outbound
 * audio is stamped on, so the two streams share a clock.
 */
export function displayFrame(
  seq: number,
  ts: number,
  image: Buffer | string,
  options: DisplayFrameOptions = {},
): string {
  const mime = options.mime ?? "image/jpeg";
  const message: Record<string, unknown> = {
    type: TYPE_DISPLAY_FRAME,
    seq,
    ts,
    mime,
    dataBase64: encodeImage(image, mime, "display.frame"),
  };
  if (options.width !== undefined) message.width = options.width;
  if (options.height !== undefined) message.height = options.height;
  return encode(message);
}

/** Hard bound on the vision round trip. The caller hears this as silence. */
const DESCRIBE_TIMEOUT_MS = 20_000;

/** Enough for a sentence or two read aloud. A voice agent cannot relay an essay. */
const DESCRIBE_MAX_TOKENS = 300;

/** How to reach the vision model. */
export interface FrameDescriberOptions {
  /** Chat-completions endpoint, for example `https://api.openai.com/v1/chat/completions`. */
  url: string;
  /** The vision model to ask. */
  model: string;
  /** Sent as a bearer token when set. A local endpoint usually needs none. */
  apiKey?: string;
}

/**
 * Turn a frame into a sentence, using a vision model you choose.
 *
 * Most speech-to-speech providers hear but cannot see. This is what lets one
 * answer "what is on my screen?": the frame goes to any OpenAI-compatible
 * chat-completions endpoint that accepts image input (OpenAI, Azure OpenAI,
 * Ollama, vLLM, whatever you run), and what comes back is text the agent can say
 * out loud.
 *
 * The frame is sent for inference and not stored, which is the difference
 * between this and uploading it into a provider's own conversation history.
 *
 * Deliberately NOT put through the guard in `fetch.ts`: this URL is yours, set
 * by you in the environment, and a vision model on localhost is a normal way to
 * run one. That is the opposite of an image URL a model chose.
 */
/**
 * A short, stable fingerprint of one frame.
 *
 * For asking "is this the same screen as last time?" without keeping the
 * picture. A hash of the encoded form is enough: two encodes of an unchanged
 * screen are byte-identical.
 */
export function frameDigest(dataBase64: string): string {
  return createHash("sha256")
    .update(dataBase64 ?? "", "ascii")
    .digest("hex")
    .slice(0, 32);
}

/** Who is showing this, when the wire said. */
export function frameOwner(frame: VideoFrame): string | undefined {
  const name = (frame.participantName ?? "").trim();
  return name === "" ? undefined : name;
}

/**
 * Who to say it is when nobody was named.
 *
 * Attribution that degrades rather than vanishing: "a participant's screen" is
 * worth more to a model than an unlabelled picture.
 */
export function fallbackOwner(source: string): string {
  return source === "screenshare" ? "a participant" : "the caller";
}

/** The sentence that goes beside a frame, so a model knows whose it is. */
export function frameCaption(owner: string): string {
  return owner === "a participant"
    ? `screen shared by ${owner}`
    : `camera of ${owner}`;
}

export class FrameDescriber {
  readonly url: string;
  readonly model: string;
  readonly apiKey: string | undefined;

  constructor(options: FrameDescriberOptions) {
    this.url = options.url;
    this.model = options.model;
    this.apiKey = options.apiKey;
  }

  /**
   * Build one from `STANDIN_VISION_API_URL` and `STANDIN_VISION_MODEL`.
   *
   * Returns `undefined` when they are not set, which is the signal a
   * plugin uses to tell an agent that looking is not available here.
   */
  static fromEnv(): FrameDescriber | undefined {
    const url = (process.env.STANDIN_VISION_API_URL ?? "").trim();
    const model = (process.env.STANDIN_VISION_MODEL ?? "").trim();
    if (!url || !model) return undefined;
    const apiKey = (process.env.STANDIN_VISION_API_KEY ?? "").trim();
    return new FrameDescriber({ url, model, apiKey: apiKey || undefined });
  }

  /**
   * Ask the model about one frame. Throws on anything that goes wrong, so a
   * caller can hand the reason back to the agent that asked.
   */
  async describe(frame: VideoFrame, question: string): Promise<string> {
    const who = frameOwner(frame) ?? fallbackOwner(frame.source);
    const seeing = frameCaption(who);
    const headers: Record<string, string> = {
      "content-type": "application/json",
    };
    if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;

    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), DESCRIBE_TIMEOUT_MS);
    try {
      const response = await fetch(this.url, {
        method: "POST",
        headers,
        signal: controller.signal,
        body: JSON.stringify({
          model: this.model,
          max_tokens: DESCRIBE_MAX_TOKENS,
          messages: [
            {
              role: "user",
              content: [
                {
                  type: "text",
                  text:
                    `This is a live frame from a Microsoft Teams call (${seeing}). ` +
                    `Answer concisely, for a voice agent to say out loud. Question: ${question}`,
                },
                { type: "image_url", image_url: { url: frame.dataUrl } },
              ],
            },
          ],
        }),
      });
      if (!response.ok) {
        throw new Error(`the vision model returned HTTP ${response.status}`);
      }
      const data = (await response.json()) as {
        choices?: Array<{ message?: { content?: string } }>;
      };
      const text = (data.choices?.[0]?.message?.content ?? "").trim();
      if (!text) throw new Error("the vision model returned nothing");
      return text;
    } finally {
      clearTimeout(deadline);
    }
  }
}
