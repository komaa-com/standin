// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The messages lane: Microsoft Teams chat, without a bot credential.
 *
 * Managed connections only. StandIn owns the Microsoft Teams bot, authenticates the
 * activity, resolves it to your connection and strips the bot @mention. Your
 * handler returns text and StandIn performs the Microsoft Teams send, so your agent never
 * holds a Bot Framework credential.
 *
 * Same shape as the call lane: the worker dials OUT and StandIn pushes messages
 * down that socket, so there is no listener, no port to expose and no tunnel.
 *
 *     Microsoft Teams message
 *          |
 *          v
 *     StandIn gateway        (authenticates, normalizes, signs)
 *          |   pushed down the worker's outbound socket
 *          v
 *     ChatChannel            (this class)
 *          |   your async handler returns reply text
 *          v
 *     back up the same socket; StandIn sends it to Microsoft Teams
 *
 * The twin of the Python SDK's `standin/sdk/chat.py`, method for method, with
 * this language's casing. The shared conformance vectors assert both parse and
 * build identical wire payloads.
 */

import { WebSocket } from "ws";

import { StandInError } from "./errors.js";
import {
  SIGNATURE_HEADER,
  TIMESTAMP_HEADER,
  nowMs,
  signHandshake,
} from "./hmac.js";
import { logger } from "./log.js";

/**
 * chat-schema.yaml SCHEMA_VERSION. A MAJOR version: additive evolution does not
 * bump it, because the schema already requires receivers to ignore unknown
 * fields. An integer above ours therefore means incompatible semantics.
 */
export const SCHEMA_VERSION = 1;

export const DEFAULT_CHAT_URL =
  "wss://teams.standin.komaa.com/api/chat/channel";

/**
 * What may be rendered inline in a reply. Raster only, and the decoded bytes
 * must actually carry the type's signature.
 *
 * `image/svg+xml` is deliberately absent and is not a gap to fill later. SVG is
 * scriptable XML, which is precisely what "an image" must not be.
 */
export const OUTBOUND_IMAGE_CONTENT_TYPES = [
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
] as const;

/** What an inline image may weigh, decoded. */
export const OUTBOUND_IMAGE_MAX_BYTES = 1024 * 1024;

/**
 * The first bytes each allowed type must begin with. A declared type is a claim;
 * this is the check. Without it an HTML page or an SVG labelled image/png is
 * posted under this bot's name.
 */
const IMAGE_MAGIC: Record<string, Buffer[]> = {
  "image/png": [Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])],
  "image/jpeg": [Buffer.from([0xff, 0xd8, 0xff])],
  "image/gif": [Buffer.from("GIF87a"), Buffer.from("GIF89a")],
  "image/webp": [Buffer.from("RIFF")],
};

/** A filename reaches a chat as a download. One path-free segment, bounded. */
const MAX_IMAGE_NAME_CHARS = 200;

/**
 * A picture to render inline in a reply, carried as bytes.
 *
 * Bytes rather than a link, because a link is a beacon: an off-domain image
 * loads with no click, under this bot's name, and what it serves can be swapped
 * after anyone looked at it. Bytes can be checked, and {@link outboundImage}
 * checks them.
 */
export interface OutboundImage {
  readonly contentType: string;
  readonly contentBase64: string;
  readonly name?: string;
}

/**
 * One path-free segment, bounded, or undefined.
 *
 * A filename reaches a chat as a download under this bot's identity, and the
 * model that chose it is being steered by whoever is in the conversation.
 */
export function sanitizeImageName(
  name: string | undefined,
): string | undefined {
  if (!name) return undefined;
  let segment = name.replace(/\\/g, "/").split("/").pop() ?? "";
  segment = [...segment]
    .filter((ch) => ch >= " " && !'<>:"|?*'.includes(ch))
    .join("")
    .trim();
  if (segment === "" || segment === "." || segment === "..") return undefined;
  if (segment.length > MAX_IMAGE_NAME_CHARS) {
    const dot = segment.lastIndexOf(".");
    const extension = dot > 0 ? segment.slice(dot + 1) : "";
    const keep = MAX_IMAGE_NAME_CHARS - extension.length - 1;
    segment =
      extension !== "" && keep > 0
        ? `${segment.slice(0, keep)}.${extension}`
        : segment.slice(0, MAX_IMAGE_NAME_CHARS);
  }
  return segment;
}

/** What these bytes actually are, or undefined. */
export function sniffImageType(data: Buffer): string | undefined {
  for (const [contentType, signatures] of Object.entries(IMAGE_MAGIC)) {
    if (!signatures.some((sig) => data.subarray(0, sig.length).equals(sig)))
      continue;
    // A RIFF container is only a webp when it says so.
    if (
      contentType === "image/webp" &&
      data.subarray(8, 12).toString("ascii") !== "WEBP"
    )
      continue;
    return contentType;
  }
  return undefined;
}

/**
 * Check a picture and build the wire form. Throws on anything it will not send.
 *
 * Three checks, and the second is the one that matters. A declared type is a
 * claim made by whatever produced the bytes; the signature is what they are.
 * Without it an HTML document or an SVG labelled `image/png` is posted into
 * somebody's chat under this bot's name.
 */
export function outboundImage(
  data: Buffer | string,
  contentType: string,
  name?: string,
): OutboundImage {
  const raw = typeof data === "string" ? Buffer.from(data, "base64") : data;
  let declared = contentType.trim().toLowerCase();
  // A common spelling that is not a media type.
  if (declared === "image/jpg") declared = "image/jpeg";
  if (!(OUTBOUND_IMAGE_CONTENT_TYPES as readonly string[]).includes(declared)) {
    throw new Error(
      `${JSON.stringify(contentType)} cannot be sent inline; it must be one of ` +
        OUTBOUND_IMAGE_CONTENT_TYPES.join(", "),
    );
  }
  if (raw.length > OUTBOUND_IMAGE_MAX_BYTES) {
    throw new Error(
      `that image is ${raw.length} bytes, over the ${OUTBOUND_IMAGE_MAX_BYTES} limit`,
    );
  }
  const actual = sniffImageType(raw);
  if (actual !== declared) {
    throw new Error(
      `those bytes are not ${declared}: they look like ${actual ?? "something else"}`,
    );
  }
  const safe = sanitizeImageName(name);
  return {
    contentType: declared,
    contentBase64: raw.toString("base64"),
    ...(safe === undefined ? {} : { name: safe }),
  };
}

function imageToWire(image: OutboundImage): Record<string, unknown> {
  const wire: Record<string, unknown> = {
    contentType: image.contentType,
    contentBase64: image.contentBase64,
  };
  if (image.name) wire.name = image.name;
  return wire;
}

/** How long one turn may run before it is abandoned. Serialization means a hung
 * turn would wedge its conversation forever, so every turn is bounded. Generous:
 * agent turns legitimately run long. */
const TURN_TIMEOUT_MS = 300_000;

/** 2 MB bounds a single inbound message, matching the call lane. */
const MAX_PAYLOAD_BYTES = 2 * 1024 * 1024;

/**
 * One user message, already authenticated and resolved to your connection.
 *
 * Reserved bot commands are handled by StandIn and never arrive here. In group
 * and channel scope only messages that @mention the bot are relayed, and the
 * mention is already stripped from `text`.
 */
export interface InboundMessage {
  readonly tenantId: string;
  readonly conversationId: string;
  readonly activityId: string;
  readonly scope: string;
  readonly text: string;
  readonly senderName?: string;
  readonly senderAadId?: string;
  readonly senderIsGuest: boolean;
  readonly senderIsLinkedOwner: boolean;
  readonly attachments: Record<string, unknown>[];
  readonly mentions: string[];
  readonly locale?: string;
  /**
   * Submit payload of an Action.Submit on a card this agent sent. `text` is
   * empty on these messages.
   */
  readonly cardAction?: Record<string, unknown>;
  /**
   * Which StandIn connection this conversation resolved to. Stable for a
   * tenant, and {@link buildReply} echoes it: one tenant can have several
   * connections, so the tenant alone no longer says who a reply is from.
   */
  readonly bindingId?: string;
}

/** True when the message came from a 1:1 chat rather than a group or channel. */
export function isPersonal(message: InboundMessage): boolean {
  return message.scope === "personal";
}

/**
 * How long a remembered 1:1 chat stays usable as a delivery target: 12 hours.
 *
 * Long enough to cover a working day, short enough that a conversation from
 * last week is not treated as evidence of who is on the phone today.
 */
export const CHAT_FALLBACK_WINDOW_MS = 12 * 60 * 60 * 1000;

/** How many people are remembered at once, oldest first out. */
const MAX_REMEMBERED_CHATS = 512;

/** Somebody's own 1:1 chat with this bot, as last seen. */
export interface PersonalChat {
  readonly conversationId: string;
  readonly tenantId: string;
  /** The sender's AAD object id, or "" when the message carried none. */
  readonly aadId: string;
  readonly displayName: string;
  /** When this chat was last seen, in epoch milliseconds. */
  readonly atMs: number;
}

/** What {@link PersonalChats.forCaller} needs to admit a chat. */
export interface ForCallerOptions {
  /** The caller's AAD object id, from the call. */
  callerAadId?: string;
  /** The tenant this worker is bound to. Never the caller's own tenant id. */
  tenantId: string;
  /** Now, in epoch milliseconds. Defaults to the clock. */
  nowMs?: number;
  /**
   * Drop the identity rule alone, for a single-operator install where the only
   * person who ever chats with the bot is the only person who ever calls it.
   * Default false, and every use is warned about by name, because with it on an
   * unidentified caller's minutes go to whoever messaged this bot last.
   */
  allowUnidentified?: boolean;
}

/**
 * The 1:1 chats this bot has been messaged in, so a call can be answered in one.
 *
 * A 1:1 CALL carries no thread id and no conversation to post into, so the only
 * honest source of the caller's own chat is a message they sent the bot from
 * it. Feed this from the chat lane and read it from the call lane:
 *
 * ```ts
 * const chats = new PersonalChats();
 * const channel = new ChatChannel({ respond, chats });
 * // later, on a call:
 * const mine = chats.forCaller({ callerAadId, tenantId });
 * ```
 *
 * Both lanes have to be in one process for that to work, or the memory has to
 * be shared some other way. Until it is, a 1:1 recap can only be delivered to
 * somebody who has messaged this bot inside {@link CHAT_FALLBACK_WINDOW_MS}.
 *
 * Nothing is remembered from a group or a channel. That is decided by the
 * message's SCOPE and by nothing else: an @mention in a team channel would
 * otherwise make that channel the caller's "chat" and put a private escalation
 * in front of their whole team.
 */
export class PersonalChats {
  /** Keyed by tenant and person, in order of last seen, and bounded. */
  readonly #chats = new Map<string, PersonalChat>();
  readonly #windowMs: number;

  constructor(windowMs: number = CHAT_FALLBACK_WINDOW_MS) {
    this.#windowMs = windowMs;
  }

  /**
   * Record a 1:1 chat. Anything else is ignored, including a message whose
   * conversation id merely looks personal.
   */
  remember(message: InboundMessage, atMs: number = nowMs()): void {
    // Scope, and only scope. A bot's personal chat is addressed "a:1..." while
    // "19:..." is precisely the group and channel shape this excludes, so an
    // id-prefix test rejects every real personal chat and admits nothing.
    if (!isPersonal(message)) return;
    const conversationId = (message.conversationId ?? "").trim();
    const tenantId = (message.tenantId ?? "").trim();
    if (conversationId === "" || tenantId === "") return;

    const aadId = (message.senderAadId ?? "").trim();
    const key = chatKey(tenantId, aadId);
    // Re-inserted rather than overwritten: a Map keeps insertion order, and
    // that order is what decides who is forgotten first.
    this.#chats.delete(key);
    this.#chats.set(key, {
      conversationId,
      tenantId,
      aadId,
      displayName: message.senderName ?? "",
      atMs,
    });
    if (this.#chats.size > MAX_REMEMBERED_CHATS) {
      const oldest = this.#chats.keys().next();
      if (!oldest.done) this.#chats.delete(oldest.value);
    }
  }

  /**
   * The chat a call may be answered in, or undefined when there is not one.
   *
   * Four things have to hold, and posting call content into the wrong
   * conversation is the failure all four exist to prevent: the chat was a 1:1
   * chat, it is in this tenant, it was seen inside
   * {@link CHAT_FALLBACK_WINDOW_MS}, and the call names a caller whose AAD id
   * is the one that sent it. Without that last rule every anonymous caller
   * collapses onto whoever chatted last.
   */
  forCaller(options: ForCallerOptions): PersonalChat | undefined {
    const tenantId = (options.tenantId ?? "").trim();
    if (tenantId === "") return undefined;
    const now = options.nowMs ?? nowMs();

    const callerAadId = (options.callerAadId ?? "").trim();
    if (callerAadId !== "") {
      const chat = this.#chats.get(chatKey(tenantId, callerAadId));
      return chat !== undefined && this.#fresh(chat, now) ? chat : undefined;
    }

    if (options.allowUnidentified !== true) return undefined;
    logger.warn(
      "standin: allowUnidentified is on, so this call is being matched to whoever " +
        "last messaged this bot in the tenant rather than to an identified caller",
    );
    let best: PersonalChat | undefined;
    for (const chat of this.#chats.values()) {
      if (chat.tenantId !== tenantId || !this.#fresh(chat, now)) continue;
      if (best === undefined || chat.atMs >= best.atMs) best = chat;
    }
    return best;
  }

  #fresh(chat: PersonalChat, now: number): boolean {
    return now - chat.atMs <= this.#windowMs;
  }
}

/** A tenant and a person. The space cannot appear in either half. */
function chatKey(tenantId: string, aadId: string): string {
  return `${tenantId} ${aadId}`;
}

/**
 * Parse and validate an inbound message. Throws naming the problem; the caller
 * maps that to HTTP 400.
 */
export function parseInbound(body: string): InboundMessage {
  let raw: unknown;
  try {
    raw = JSON.parse(body);
  } catch {
    throw new StandInError("malformed json");
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new StandInError("body must be an object");
  }
  const rec = raw as Record<string, unknown>;

  for (const key of ["tenantId", "conversationId", "activityId"] as const) {
    if (typeof rec[key] !== "string" || rec[key] === "") {
      throw new StandInError(`${key} is required`);
    }
  }

  const version = rec.schemaVersion ?? SCHEMA_VERSION;
  if (
    typeof version === "number" &&
    Number.isInteger(version) &&
    version > SCHEMA_VERSION
  ) {
    throw new StandInError(
      `unsupported schemaVersion ${version} (this plugin speaks ${SCHEMA_VERSION})`,
    );
  }

  const rawSender = rec.sender;
  const sender: Record<string, unknown> =
    typeof rawSender === "object" && rawSender !== null
      ? (rawSender as Record<string, unknown>)
      : {};

  const scope = rec.scope;
  const str = (v: unknown): string | undefined =>
    typeof v === "string" ? v : undefined;

  return {
    tenantId: rec.tenantId as string,
    conversationId: rec.conversationId as string,
    activityId: rec.activityId as string,
    // ChatScope is an OPEN enum: an unknown value relays as a group chat rather
    // than being rejected.
    scope: typeof scope === "string" && scope ? scope : "personal",
    text: typeof rec.text === "string" ? rec.text : "",
    senderName: str(sender.displayName),
    senderAadId: str(sender.aadObjectId),
    senderIsGuest: Boolean(sender.isGuest ?? false),
    senderIsLinkedOwner: Boolean(sender.isLinkedOwner ?? false),
    attachments: Array.isArray(rec.attachments)
      ? (rec.attachments as Record<string, unknown>[])
      : [],
    mentions: Array.isArray(rec.mentions) ? (rec.mentions as string[]) : [],
    locale: str(rec.locale),
    cardAction:
      typeof rec.cardAction === "object" &&
      rec.cardAction !== null &&
      !Array.isArray(rec.cardAction)
        ? (rec.cardAction as Record<string, unknown>)
        : undefined,
    bindingId: str(rec.bindingId),
  };
}

/**
 * The gateway-bound reply. tenantId and conversationId echo the inbound
 * EXACTLY: the gateway rejects a mismatch, and that check is the cross-tenant
 * leak guard the whole relay rests on.
 */
export function buildReply(
  message: InboundMessage,
  text: string,
  kind: string = "message",
  image?: OutboundImage,
): Record<string, unknown> {
  const reply: Record<string, unknown> = {
    schemaVersion: SCHEMA_VERSION,
    tenantId: message.tenantId,
    conversationId: message.conversationId,
    replyToId: message.activityId,
    kind,
    idempotencyKey: `${message.activityId}:${kind}`,
  };
  if (message.bindingId) {
    // Which connection this reply is FROM. One tenant can have several, so the
    // tenant alone no longer identifies the sender.
    reply.bindingId = message.bindingId;
  }
  if (kind !== "typing") {
    reply.text = text;
    // A typing indicator carries neither: it is a state, not a message.
    if (image !== undefined) reply.image = imageToWire(image);
  }
  return reply;
}

/**
 * At-least-once dedupe on the schema's activityId idempotency key. Bounded LRU:
 * an aged-out redelivery running again is acceptable at-least-once behaviour, a
 * fresh double-run is not.
 */
class Seen {
  readonly #capacity: number;
  readonly #seen = new Set<string>();

  constructor(capacity = 2048) {
    this.#capacity = capacity;
  }

  markFirst(key: string): boolean {
    if (this.#seen.has(key)) return false;
    this.#seen.add(key);
    if (this.#seen.size > this.#capacity) {
      // Set preserves insertion order, so the first entry is the oldest.
      const oldest = this.#seen.values().next();
      if (!oldest.done) this.#seen.delete(oldest.value);
    }
    return true;
  }
}

/** Options for {@link ChatChannel}. */
export interface ChatChannelOptions {
  /**
   * Async callable taking an {@link InboundMessage} and returning the reply
   * text. An empty string makes the channel say so rather than leaving the user
   * watching a typing indicator forever.
   */
  respond: (message: InboundMessage) => Promise<string>;
  /** Your StandIn connection secret, defaulting to `STANDIN_SECRET`. */
  secret?: string;
  /** The chat channel URL, defaulting to `STANDIN_CHAT_URL`. */
  url?: string;
  /**
   * Remembers who has a 1:1 chat with this bot, fed on every inbound message.
   * Share one instance with the call lane and a 1:1 call has somewhere to post
   * its minutes.
   */
  chats?: PersonalChats;
}

/**
 * Answer Microsoft Teams messages with your agent.
 *
 * Dialed out from the worker, like the call lane, so nothing listens and there
 * is nothing to expose. Managed connections only, and that needs no flag: the
 * socket authenticates with your connection secret, so if it opens at all you
 * are managed.
 *
 * ```ts
 * const chat = new ChatChannel({
 *   respond: async (msg) => `You said: ${msg.text}`,
 * });
 * await chat.start();
 * ```
 */
export class ChatChannel {
  readonly #respond: (message: InboundMessage) => Promise<string>;
  readonly #secret: string;
  readonly #url: string;
  readonly #seen = new Seen();
  readonly #chats: PersonalChats | undefined;
  #ws: WebSocket | undefined;
  #closed = false;
  /**
   * Per-conversation chains. The schema promises per-conversation ORDERING;
   * independent tasks would let replies overtake each other.
   */
  readonly #chains = new Map<string, Promise<void>>();

  constructor(options: ChatChannelOptions) {
    this.#secret =
      options.secret ??
      process.env.STANDIN_CHAT_SECRET ??
      process.env.STANDIN_SECRET ??
      "";
    if (!this.#secret) {
      throw new StandInError(
        "a StandIn connection secret is required: pass secret or set STANDIN_SECRET",
      );
    }
    this.#respond = options.respond;
    this.#url = options.url ?? process.env.STANDIN_CHAT_URL ?? DEFAULT_CHAT_URL;
    this.#chats = options.chats;
  }

  /** Dial StandIn and begin taking messages. */
  async start(): Promise<void> {
    const timestamp = nowMs();
    const ws = new WebSocket(this.#url, {
      headers: {
        [TIMESTAMP_HEADER]: String(timestamp),
        // The channel NAME is what the handshake signs here, not the body. The
        // POST relay lane signs the body instead; the two are different lanes
        // and both are correct. Do not "fix" one into the other.
        [SIGNATURE_HEADER]: signHandshake(this.#secret, timestamp, "chat"),
      },
      maxPayload: MAX_PAYLOAD_BYTES,
    });

    try {
      await new Promise<void>((resolve, reject) => {
        ws.once("open", () => resolve());
        ws.once("error", reject);
      });
    } catch (err) {
      ws.terminate();
      throw err;
    }

    this.#ws = ws;
    ws.on("message", (data: Buffer, isBinary: boolean) => {
      if (isBinary) return;
      this.#onMessage(data.toString("utf8"));
    });
    ws.on("error", (err) => {
      logger.error(`standin: chat channel failed: ${String(err)}`);
    });
    logger.info("standin: chat channel open");
  }

  /** Stop taking messages and close the socket. */
  async aclose(): Promise<void> {
    this.#closed = true;
    // Let in-flight turns settle so a reply already handed to the agent is not
    // silently dropped on shutdown.
    await Promise.allSettled([...this.#chains.values()]);
    this.#chains.clear();

    const ws = this.#ws;
    this.#ws = undefined;
    if (ws !== undefined && ws.readyState === WebSocket.OPEN) {
      try {
        ws.close();
      } catch {
        ws.terminate();
      }
    }
  }

  #onMessage(body: string): void {
    if (this.#closed) return;
    let inbound: InboundMessage;
    try {
      inbound = parseInbound(body);
    } catch (err) {
      logger.warn(`standin: dropping malformed chat message: ${String(err)}`);
      return;
    }
    // Before the dedupe: a redelivery is still evidence that this person has a
    // chat with this bot, and remembering it twice changes nothing.
    this.#chats?.remember(inbound);
    // The turn, though, runs once: StandIn is at-least-once, and a redelivery
    // must not start a second turn for the same activity.
    const key = `${inbound.tenantId}:${inbound.conversationId}:${inbound.activityId}`;
    if (this.#seen.markFirst(key)) this.#enqueue(inbound);
  }

  #enqueue(message: InboundMessage): void {
    const chainKey = `${message.tenantId}:${message.conversationId}`;
    const previous = this.#chains.get(chainKey) ?? Promise.resolve();

    // A failed turn must not dam the chain, so the previous rejection is
    // swallowed HERE rather than propagated into this turn.
    const next = previous
      .catch(() => undefined)
      .then(() => this.#process(message));

    this.#chains.set(chainKey, next);
    void next.finally(() => {
      if (this.#chains.get(chainKey) === next) this.#chains.delete(chainKey);
    });
  }

  async #process(message: InboundMessage): Promise<void> {
    // Typing is a courtesy, so it must not sit in FRONT of the turn. Send it and
    // let the agent think; the indicator still lands first.
    this.#send(buildReply(message, "", "typing"));
    try {
      const text = await withTimeout(this.#respond(message), TURN_TIMEOUT_MS);
      if (text && text.trim()) {
        this.#send(buildReply(message, text));
      } else {
        // After a typing indicator, silence looks exactly like a hang.
        logger.warn("standin: chat handler returned an empty answer");
        this.#send(
          buildReply(
            message,
            "I couldn't come up with an answer to that - try rephrasing, or ask something else.",
            "error",
          ),
        );
      }
    } catch (err) {
      logger.error(`standin: chat turn failed: ${String(err)}`);
      this.#send(
        buildReply(
          message,
          "Something went wrong answering that - please try again.",
          "error",
        ),
      );
    }
  }

  /**
   * Post into a Microsoft Teams conversation with no inbound message to answer.
   *
   * Useful from inside a call. Best-effort: returns false rather than throwing,
   * because a failed post must never break a live call.
   */
  async send(options: {
    tenantId: string;
    conversationId: string;
    text: string;
    image?: OutboundImage;
    bindingId?: string;
    idempotencyKey?: string;
  }): Promise<boolean> {
    const payload: Record<string, unknown> = {
      schemaVersion: SCHEMA_VERSION,
      tenantId: options.tenantId,
      conversationId: options.conversationId,
      kind: "message",
      text: options.text,
    };
    if (options.image !== undefined) payload.image = imageToWire(options.image);
    if (options.bindingId) payload.bindingId = options.bindingId;
    if (options.idempotencyKey) payload.idempotencyKey = options.idempotencyKey;
    return this.#send(payload);
  }

  #send(reply: Record<string, unknown>): boolean {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) {
      logger.warn("standin: chat channel is not open; dropping a reply");
      return false;
    }
    try {
      ws.send(JSON.stringify(reply));
      return true;
    } catch (err) {
      logger.warn(`standin: chat reply failed: ${String(err)}`);
      return false;
    }
  }
}

/** Reject after `ms`, so one hung turn cannot wedge its conversation forever. */
function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const expiry = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new StandInError("chat turn timed out")),
      ms,
    );
    timer.unref?.();
  });
  return Promise.race([promise, expiry]).finally(() =>
    clearTimeout(timer),
  ) as Promise<T>;
}
