// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What somebody attached to a chat message, turned into something an agent can
 * use.
 *
 * A Microsoft Teams message can carry a pasted screenshot, a dragged-in file, or
 * a voice note. Without this the handler gets `InboundMessage.attachments` as
 * raw objects, so the best it can do is read a JSON blob to a model, and the
 * worst is answer a message about a picture as though nothing had been sent.
 *
 * {@link buildChatTurn} is the whole thing in one call: the text, the images
 * fetched and ready to hand to a vision model, a voice note transcribed, and a
 * plain sentence naming anything that could not be read.
 *
 * This is deliberately NOT in `chat.ts`. That module owns the socket, the
 * duplicate check and the per-conversation ordering, and none of it changes
 * here. Fetching is optional work that must never be able to wedge the
 * transport, so it lives beside it: a handler that fetches nothing pays nothing.
 *
 * Every fetch is pinned to the one origin the messages themselves arrived from,
 * and fails CLOSED. An attachment URL is signed, but not by us and not for us:
 * it arrives inside a message somebody else wrote, and the pin is the only thing
 * bounding where this worker can be told to go.
 *
 * Identical in shape to the Python SDK's `standin.attachments`.
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { DEFAULT_CHAT_URL, type InboundMessage } from "./chat.js";
import { logger } from "./log.js";

/**
 * What one image may weigh. Matches the per-attachment ceiling the relay itself
 * applies, so a larger local number could never be reached anyway.
 */
export const MAX_IMAGE_BYTES = 4 * 1024 * 1024;

/** Images kept from one message. Each becomes a base64 blob in front of a model. */
export const MAX_IMAGES = 4;

/** How long one image has to arrive. */
export const IMAGE_FETCH_TIMEOUT_MS = 10_000;

/**
 * How many images may be REQUESTED, whatever the outcome. The accept cap counts
 * only successes, so a message naming fifty attachments that all time out still
 * costs fifty timeouts and blows the turn budget. This makes the worst case
 * arithmetic: eight tries at ten seconds.
 */
export const IMAGE_FETCH_ATTEMPTS = 8;

/** A voice note is minutes of audio, so it gets a bigger budget than a picture. */
export const MAX_CLIP_BYTES = 16 * 1024 * 1024;
export const MAX_CLIPS = 2;
export const CLIP_FETCH_TIMEOUT_MS = 20_000;
export const CLIP_FETCH_ATTEMPTS = 4;

/** A card's submit payload is model input, bounded because it reaches a model. */
export const CARD_PAYLOAD_MAX_CHARS = 4096;

/** Lines in the "what was attached" note. */
export const ATTACHMENT_NOTE_MAX_LINES = 10;

/**
 * Extensions that make a relayed FILE worth trying as an image. A pasted
 * screenshot arrives as an image; the same file dragged in from disk arrives as
 * a file whose declared type is a bare extension, so gating on the kind alone
 * makes an attached picture invisible while the note says one was sent.
 */
const IMAGE_EXTENSIONS = [
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "bmp",
  "heic",
  "heif",
];

/** The same for a voice note, which also arrives as a file in practice. */
const AUDIO_EXTENSIONS = [
  "wav",
  "mp3",
  "m4a",
  "mp4",
  "ogg",
  "oga",
  "opus",
  "aac",
  "amr",
  "webm",
  "mov",
  "3gp",
];

const SCHEME_FOR_FETCH: Record<string, string> = {
  "ws:": "http",
  "wss:": "https",
  "http:": "http",
  "https:": "https",
};
const DEFAULT_PORT: Record<string, number> = { http: 80, https: 443 };

/** One picture from a message, ready for a vision model. */
export interface ChatImage {
  readonly dataBase64: string;
  readonly mime: string;
  readonly name?: string;
  readonly sizeBytes: number;
}

/** One voice note, as bytes, before anything has transcribed it. */
export interface ChatAudio {
  readonly data: Buffer;
  readonly mime: string;
  readonly name: string;
}

/** One inbound message, assembled into what an agent is actually asked. */
export interface ChatTurn {
  /** The text to put in front of the model, including every note below. */
  readonly query: string;
  readonly images: ChatImage[];
  readonly voiceNote: string;
  readonly attachmentNote: string;
}

/** The bytes of an image, since an interface carries no accessors. */
export function chatImageData(image: ChatImage): Buffer {
  return Buffer.from(image.dataBase64, "base64");
}

/** The `data:` form most vision APIs take directly. */
export function chatImageDataUrl(image: ChatImage): string {
  return `data:${image.mime};base64,${image.dataBase64}`;
}

/**
 * Turn a voice note into words. Supplied by the handler or a speech plugin: the
 * core ships none and reads no provider key.
 */
export type Transcriber = (data: Buffer, mime: string) => Promise<string>;

/** Options shared by the fetchers. */
export interface FetchOptions {
  origin: string | undefined;
  maxBytes?: number;
  timeoutMs?: number;
  fetchFn?: typeof fetch;
}

/**
 * The one origin attachments may be fetched from, or undefined.
 *
 * Derived from the chat channel's own URL rather than configured separately, so
 * it is right by construction for a self-hosted or local gateway and there is no
 * second setting to get wrong.
 *
 * undefined means fetch nothing. That is the safe direction: an unset origin
 * read as "anywhere" turns a configuration typo into a fetcher that a message
 * can point at any address it likes.
 */
export function chatAttachmentOrigin(url?: string): string | undefined {
  const raw = (url ?? process.env.STANDIN_CHAT_URL ?? DEFAULT_CHAT_URL).trim();
  let parts: URL;
  try {
    parts = new URL(raw);
  } catch {
    return undefined;
  }
  const scheme = SCHEME_FOR_FETCH[parts.protocol];
  if (scheme === undefined || parts.hostname === "") return undefined;
  const port = parts.port === "" ? DEFAULT_PORT[scheme]! : Number(parts.port);
  const host = parts.hostname.toLowerCase();
  return port === DEFAULT_PORT[scheme]
    ? `${scheme}://${host}`
    : `${scheme}://${host}:${port}`;
}

/** Whether this URL is the origin we were told about. Fails closed. */
function sameOrigin(url: string, origin: string | undefined): boolean {
  if (!origin) return false;
  let got: URL;
  let want: URL;
  try {
    got = new URL(url);
    want = new URL(origin);
  } catch {
    return false;
  }
  if (got.protocol !== "http:" && got.protocol !== "https:") return false;
  const key = (u: URL): string => {
    const scheme = u.protocol.replace(":", "");
    const port = u.port === "" ? DEFAULT_PORT[scheme] : Number(u.port);
    return `${scheme}|${u.hostname.toLowerCase()}|${port}`;
  };
  return key(got) === key(want);
}

function looksLike(
  name: string,
  contentType: string,
  extensions: string[],
): boolean {
  if (extensions.includes(contentType.trim().toLowerCase())) return true;
  const suffix = name.includes(".")
    ? name.split(".").pop()!.trim().toLowerCase()
    : "";
  return extensions.includes(suffix);
}

/** Attachments worth a request, in wire order. */
function candidates(
  attachments: readonly Record<string, unknown>[],
  kind: string,
  extensions: string[],
): Array<{ index: number; item: Record<string, unknown> }> {
  const out: Array<{ index: number; item: Record<string, unknown> }> = [];
  attachments.forEach((item, index) => {
    if (typeof item !== "object" || item === null) return;
    if (item.relayable === false) return;
    if (typeof item.url !== "string" || item.url === "") return;
    const itemKind = String(item.kind ?? "");
    if (
      itemKind === kind ||
      (itemKind === "file" &&
        looksLike(
          String(item.name ?? ""),
          String(item.contentType ?? ""),
          extensions,
        ))
    ) {
      out.push({ index, item });
    }
  });
  return out;
}

/**
 * The real media type.
 *
 * What the RESPONSE said wins. The declared value arrived inside the message,
 * which somebody else wrote, so letting it decide would let a message claim
 * `image/png` for a page of HTML and have it read as a picture.
 *
 * The declared value is the fallback, and only when it IS a media type: a
 * relayed file declares a bare extension, and taking that literally fails every
 * `audio/` check.
 */
function resolveMime(declared: string, header: string): string {
  for (const candidate of [header, declared]) {
    const value = String(candidate ?? "")
      .split(";")[0]!
      .trim()
      .toLowerCase();
    if (value.includes("/")) return value;
  }
  return "";
}

/**
 * The body, or undefined when it is too big.
 *
 * Checked while reading, not after. A content-length that lies, or is simply
 * absent, otherwise gets to allocate whatever it likes before a later check
 * objects.
 */
async function readCapped(
  response: Response,
  maxBytes: number,
): Promise<Buffer | undefined> {
  const declared = response.headers.get("content-length");
  if (
    declared !== null &&
    /^\d+$/.test(declared) &&
    Number(declared) > maxBytes
  )
    return undefined;
  const body = response.body;
  if (body === null) return undefined;
  const reader = body.getReader();
  const chunks: Buffer[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > maxBytes) {
        await reader.cancel().catch(() => undefined);
        return undefined;
      }
      chunks.push(Buffer.from(value));
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks);
}

interface Taken {
  index: number;
  body: Buffer;
  mime: string;
  name: string;
}

/** The shared fetch loop. Never throws, and returns what it got. */
async function fetchAll(
  attachments: readonly Record<string, unknown>[],
  spec: {
    kind: string;
    extensions: string[];
    prefixes: string[];
    maxItems: number;
    maxAttempts: number;
    defaultTimeoutMs: number;
    defaultMaxBytes: number;
  },
  options: FetchOptions,
): Promise<Taken[]> {
  const call = options.fetchFn ?? fetch;
  const maxBytes = options.maxBytes ?? spec.defaultMaxBytes;
  const timeoutMs = options.timeoutMs ?? spec.defaultTimeoutMs;
  const taken: Taken[] = [];
  let attempts = 0;

  for (const { index, item } of candidates(
    attachments,
    spec.kind,
    spec.extensions,
  )) {
    if (taken.length >= spec.maxItems || attempts >= spec.maxAttempts) break;
    const url = String(item.url);
    if (!sameOrigin(url, options.origin)) {
      logger.warn("standin: refusing an attachment from another origin");
      continue;
    }
    attempts += 1;
    try {
      // No redirects. The URL is same-origin and signed, so a redirect off it is
      // already anomalous, and following one reopens the door the origin pin
      // just closed: the pin is checked on the URL in the message, not on
      // wherever a 302 points.
      const response = await call(url, {
        redirect: "error",
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) continue;
      const mime = resolveMime(
        String(item.contentType ?? ""),
        response.headers.get("content-type") ?? "",
      );
      // Judged BEFORE a byte is read: an error page would otherwise be base64'd
      // in front of a model as though it were a picture.
      if (!spec.prefixes.some((prefix) => mime.startsWith(prefix))) continue;
      const body = await readCapped(response, maxBytes);
      if (body !== undefined && body.length > 0) {
        taken.push({ index, body, mime, name: String(item.name ?? "") });
      }
    } catch (err) {
      logger.warn(`standin: could not fetch an attachment: ${String(err)}`);
    }
  }
  return taken;
}

/**
 * Pictures from a message, ready to put in front of a vision model.
 *
 * Best-effort per attachment: one that will not load costs that attachment,
 * never the answer.
 */
export async function fetchChatImages(
  attachments: readonly Record<string, unknown>[],
  options: FetchOptions & { maxImages?: number },
): Promise<ChatImage[]> {
  const got = await fetchAll(
    attachments,
    {
      kind: "image",
      extensions: IMAGE_EXTENSIONS,
      prefixes: ["image/"],
      maxItems: options.maxImages ?? MAX_IMAGES,
      maxAttempts: IMAGE_FETCH_ATTEMPTS,
      defaultTimeoutMs: IMAGE_FETCH_TIMEOUT_MS,
      defaultMaxBytes: MAX_IMAGE_BYTES,
    },
    options,
  );
  return got.map((t) => ({
    dataBase64: t.body.toString("base64"),
    mime: t.mime,
    name: t.name || undefined,
    sizeBytes: t.body.length,
  }));
}

/**
 * Voice notes from a message, as bytes.
 *
 * `video/` is accepted as well as `audio/`: some clients label a voice or video
 * note with a container type that speech-to-text reads perfectly well.
 */
export async function fetchChatAudio(
  attachments: readonly Record<string, unknown>[],
  options: FetchOptions & { maxClips?: number },
): Promise<ChatAudio[]> {
  const got = await fetchAll(
    attachments,
    {
      kind: "audio",
      extensions: AUDIO_EXTENSIONS,
      prefixes: ["audio/", "video/"],
      maxItems: options.maxClips ?? MAX_CLIPS,
      maxAttempts: CLIP_FETCH_ATTEMPTS,
      defaultTimeoutMs: CLIP_FETCH_TIMEOUT_MS,
      defaultMaxBytes: MAX_CLIP_BYTES,
    },
    options,
  );
  return got.map((t) => ({ data: t.body, mime: t.mime, name: t.name }));
}

/**
 * What the voice notes said, as one block of text.
 *
 * Empty when there are none, when no transcriber was supplied, or when every one
 * failed. A transcriber that throws costs that clip and nothing else.
 */
export async function transcribeVoiceMessages(
  attachments: readonly Record<string, unknown>[],
  options: FetchOptions & { transcribe?: Transcriber; maxClips?: number },
): Promise<string> {
  if (options.transcribe === undefined) return "";
  const said: string[] = [];
  for (const clip of await fetchChatAudio(attachments, options)) {
    try {
      const text = (await options.transcribe(clip.data, clip.mime)).trim();
      if (text !== "") said.push(text);
    } catch (err) {
      logger.warn(
        `standin: could not transcribe a voice message: ${String(err)}`,
      );
    }
  }
  return said.join("\n");
}

/**
 * A plain sentence naming what came with the message.
 *
 * Worth saying even when nothing could be read: a model that is told a picture
 * was attached and could not be opened says something useful, and a model told
 * nothing answers as if the message were empty.
 */
export function attachmentsNote(
  attachments: readonly Record<string, unknown>[],
  status: Map<number, string> = new Map(),
): string {
  if (attachments.length === 0) return "";
  const lines: string[] = [];
  for (const [index, item] of attachments.entries()) {
    if (lines.length >= ATTACHMENT_NOTE_MAX_LINES) {
      lines.push(`and ${attachments.length - lines.length} more`);
      break;
    }
    if (typeof item !== "object" || item === null) continue;
    const name = String(item.name ?? "").trim() || "an unnamed file";
    const kind = String(item.kind ?? "file").trim() || "file";
    const mark = status.get(index);
    lines.push(`- ${name} [${kind}]${mark ? ` (${mark})` : ""}`);
  }
  if (lines.length === 0) return "";
  return `[Attached to this message]\n${lines.join("\n")}`;
}

/**
 * What a button press on one of this agent's own cards submitted.
 *
 * A card message arrives with EMPTY text, so without this the agent is asked
 * nothing at all and answers as though the person said nothing.
 */
export function cardActionNote(
  cardAction: Record<string, unknown> | undefined,
): string {
  if (cardAction === undefined || cardAction === null) return "";
  let payload: string;
  try {
    payload = JSON.stringify(cardAction, Object.keys(cardAction).sort());
  } catch {
    payload = String(cardAction);
  }
  return `[The person used a button on your card]\n${payload.slice(0, CARD_PAYLOAD_MAX_CHARS)}`;
}

/** Options for {@link buildChatTurn}. */
export interface ChatTurnOptions {
  origin?: string;
  images?: boolean;
  transcribe?: Transcriber;
  fetchFn?: typeof fetch;
}

/**
 * One inbound message, assembled into what to ask an agent.
 *
 * The order is fixed, and it is the order a person would say it in: what they
 * typed, what they pressed, what they said out loud, then what they attached.
 *
 * ```ts
 * const turn = await buildChatTurn(message, { transcribe: myStt });
 * const answer = await agent.ask(turn.query, turn.images.map(chatImageDataUrl));
 * ```
 *
 * Never throws. Anything that will not load is named in the note rather than
 * failing the turn.
 */
export async function buildChatTurn(
  message: InboundMessage,
  options: ChatTurnOptions = {},
): Promise<ChatTurn> {
  const origin =
    options.origin !== undefined ? options.origin : chatAttachmentOrigin();
  const attachments = (message.attachments ?? []) as Record<string, unknown>[];
  const shared = { origin, fetchFn: options.fetchFn };

  const images =
    options.images === false ? [] : await fetchChatImages(attachments, shared);
  const voiceNote = await transcribeVoiceMessages(attachments, {
    ...shared,
    transcribe: options.transcribe,
  });

  // Which ones actually made it, so the note can say so rather than implying the
  // model has seen something it has not.
  const readable = new Set(images.map((i) => i.name).filter(Boolean));
  const status = new Map<number, string>();
  attachments.forEach((item, index) => {
    if (typeof item !== "object" || item === null) return;
    status.set(
      index,
      readable.has(String(item.name ?? "")) ? "attached" : "unreadable",
    );
  });

  const card = cardActionNote(
    message.cardAction as Record<string, unknown> | undefined,
  );
  const attachmentNote = attachmentsNote(attachments, status);
  const spoken = voiceNote ? `[They sent a voice message]\n${voiceNote}` : "";

  const query = [message.text.trim(), card, spoken, attachmentNote]
    .filter(Boolean)
    .join("\n\n");
  return { query, images, voiceNote, attachmentNote };
}

/**
 * Put a voice note on disk for an engine that only takes a path.
 *
 * Removed on the way out, on every path. A transcription engine is handed
 * somebody's voice, and leaving it in a temporary directory is a copy nobody
 * decided to keep.
 */
export async function spoolClip<T>(
  clip: ChatAudio,
  use: (path: string) => Promise<T>,
): Promise<T> {
  const dir = mkdtempSync(join(tmpdir(), "standin-clip-"));
  const suffix = clip.mime.split("/").pop() || "bin";
  const path = join(dir, `clip.${suffix}`);
  try {
    writeFileSync(path, clip.data);
    return await use(path);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
