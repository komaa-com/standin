// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One Cartesia Line agent stream, as a socket.
 *
 * This plugin is a transport and nothing more, and that is Cartesia's
 * design rather than a shortcut here: the agent itself - the model, the tools,
 * the conversation logic - is YOUR code, deployed on Cartesia's platform. There
 * is no client-side tool channel on this wire, so unlike the other providers
 * there are no call capabilities to declare.
 *
 * Audio is pinned to `pcm_16000`, which is exactly what StandIn speaks, so
 * nothing here resamples anything.
 *
 * The long-lived API key never touches the agent socket. It mints a short-lived
 * token over HTTPS and that token authenticates the socket, so a key cannot leak
 * from a per-call connection.
 *
 * The Python twin is `standin.plugins.cartesia.agent`.
 */

import { randomUUID } from "node:crypto";

import { WebSocket } from "ws";

import { logger } from "../../log.js";
import type { CartesiaConfig } from "./config.js";

/** The one wire rate: StandIn's PCM16 at 16 kHz is Line's pcm_16000. */
export const WIRE_SAMPLE_RATE_HZ = 16_000;
const INPUT_FORMAT = "pcm_16000";

/**
 * Line drops an idle connection after about three minutes, and a caller who is
 * simply listening sends nothing. A protocol ping keeps it alive.
 */
const KEEPALIVE_INTERVAL_MS = 60_000;

const REST_TIMEOUT_MS = 10_000;

/** Per-call tokens are minted with the maximum lifetime the API allows. */
const ACCESS_TOKEN_TTL_S = 3600;

const MAX_SEND_BUFFER_BYTES = 1024 * 1024;

/** An event from Cartesia. Only `event` is meaningful. */
export type AgentMessage = Record<string, unknown>;

/** Caller details handed to the Line agent. */
export interface CallerContext {
  callerName: string;
  tenantId: string;
  direction: string;
}

/**
 * The `start` event, sent once as the first message on the socket.
 *
 * Caller details ALWAYS ride `metadata`, which the Line agent's own code
 * receives. They are appended to the system prompt only when you set one here,
 * because a plugin must never silently rewrite the prompt you wrote on
 * Cartesia's side.
 */
export function buildStart(
  streamId: string,
  config: CartesiaConfig,
  caller: CallerContext,
  callId: string,
): Record<string, unknown> {
  const streamConfig: Record<string, unknown> = { input_format: INPUT_FORMAT };
  if (config.voiceId) streamConfig.voice_id = config.voiceId;

  const agent: Record<string, unknown> = {};
  if (config.introduction) agent.introduction = config.introduction;
  if (config.systemPrompt) {
    agent.system_prompt =
      `${config.systemPrompt.trim()}\n\n` +
      `Call context: you are speaking with ${caller.callerName} ` +
      `(tenant: ${caller.tenantId}) on an ${caller.direction} Microsoft Teams call.`;
  }

  const start: Record<string, unknown> = {
    event: "start",
    stream_id: streamId,
    config: streamConfig,
    metadata: {
      from: "msteams",
      callId,
      callerName: caller.callerName,
      tenantId: caller.tenantId,
      direction: caller.direction,
    },
  };
  if (Object.keys(agent).length > 0) start.agent = agent;
  return start;
}

/** Mint a short-lived token for one call. */
export async function mintAccessToken(config: CartesiaConfig): Promise<string> {
  const controller = new AbortController();
  const deadline = setTimeout(() => controller.abort(), REST_TIMEOUT_MS);
  try {
    const response = await fetch(`https://${config.apiHost}/access-token`, {
      method: "POST",
      signal: controller.signal,
      headers: {
        authorization: `Bearer ${config.apiKey}`,
        "cartesia-version": config.version,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        grants: { agent: true },
        expires_in: ACCESS_TOKEN_TTL_S,
      }),
    });
    if (!response.ok) {
      throw new Error(
        `minting a Cartesia token failed: HTTP ${response.status}`,
      );
    }
    const body = (await response.json()) as { token?: string };
    if (!body.token)
      throw new Error("the Cartesia token response carried no token");
    return body.token;
  } finally {
    clearTimeout(deadline);
  }
}

/** What the relay needs from a stream. Tests substitute their own. */
export interface AgentPort {
  readonly isOpen: boolean;
  readonly streamId: string;
  sendStart(start: Record<string, unknown>): void;
  sendAudioChunk(pcmBase64: string): void;
  sendDtmf(digit: string): void;
  sendCustom(metadata: Record<string, unknown>): void;
  aclose(): Promise<void>;
}

/** The socket to one Cartesia Line agent stream. */
export class AgentSocket implements AgentPort {
  readonly streamId = randomUUID().replace(/-/g, "");
  #ws: WebSocket | undefined;
  #keepalive: NodeJS.Timeout | undefined;
  #pendingBytes = 0;
  #closedByUs = false;

  private constructor(private readonly config: CartesiaConfig) {}

  /** Open the stream, retrying once on a transient failure. */
  static async connect(
    config: CartesiaConfig,
    onMessage: (message: AgentMessage) => void | Promise<void>,
    /** Agent audio, base64 PCM16 at the wire rate. */
    onAudio: (payloadBase64: string) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): Promise<AgentSocket> {
    const socket = new AgentSocket(config);
    try {
      await socket.#open();
    } catch (err) {
      logger.warn(
        `standin: Cartesia connect failed (${String(err)}); retrying once`,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      await socket.#open();
    }
    socket.#listen(onMessage, onAudio, onClose);
    socket.#keepalive = setInterval(
      () => socket.#ws?.ping(),
      KEEPALIVE_INTERVAL_MS,
    );
    socket.#keepalive.unref?.();
    return socket;
  }

  async #open(): Promise<void> {
    const token = await mintAccessToken(this.config);
    const ws = new WebSocket(
      `wss://${this.config.apiHost}/agents/stream/${this.config.agentId}`,
      {
        headers: {
          authorization: `Bearer ${token}`,
          "cartesia-version": this.config.version,
        },
        maxPayload: 16 * 1024 * 1024,
      },
    );
    await new Promise<void>((resolve, reject) => {
      const fail = setTimeout(() => {
        ws.terminate();
        reject(new Error("the Cartesia socket did not open in time"));
      }, REST_TIMEOUT_MS);
      fail.unref?.();
      ws.once("open", () => {
        clearTimeout(fail);
        resolve();
      });
      ws.once("error", (err) => {
        clearTimeout(fail);
        reject(err);
      });
    });
    this.#ws = ws;
  }

  #listen(
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onAudio: (payloadBase64: string) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): void {
    const ws = this.#ws;
    if (ws === undefined) return;

    ws.on("message", (raw: Buffer, isBinary: boolean) => {
      if (isBinary) {
        // This wire is JSON only: audio rides base64 inside events.
        logger.warn(
          "standin: Cartesia sent an unexpected binary frame; dropping",
        );
        return;
      }
      let message: unknown;
      try {
        message = JSON.parse(raw.toString());
      } catch {
        logger.warn("standin: Cartesia sent an unparseable frame; dropping");
        return;
      }
      if (typeof message !== "object" || message === null) return;
      const typed = message as Record<string, unknown>;

      // The hot path first: agent audio goes straight out without touching the
      // rest of the dispatch.
      if (typed.event === "media_output") {
        const media = typed.media as { payload?: unknown } | undefined;
        if (typeof media?.payload === "string" && media.payload) {
          void Promise.resolve(onAudio(media.payload)).catch((err: unknown) => {
            logger.error(
              `standin: handling Cartesia audio failed: ${String(err)}`,
            );
          });
        }
        return;
      }
      void Promise.resolve(onMessage(typed)).catch((err: unknown) => {
        logger.error(
          `standin: handling Cartesia ${String(typed.event ?? "event")} failed: ${String(err)}`,
        );
      });
    });

    ws.on("close", (code, reason) => {
      if (this.#keepalive) clearInterval(this.#keepalive);
      void onClose(code, reason.toString());
    });
    ws.on("error", (err) => {
      logger.warn(`standin: the Cartesia socket failed: ${String(err)}`);
    });
  }

  get isOpen(): boolean {
    return this.#ws !== undefined && this.#ws.readyState === WebSocket.OPEN;
  }

  #send(message: Record<string, unknown>, droppable = false): void {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) return;
    const payload = JSON.stringify(message);
    // Stale caller audio is worth nothing by the time it lands.
    if (droppable && this.#pendingBytes > MAX_SEND_BUFFER_BYTES) return;
    this.#pendingBytes += payload.length;
    ws.send(payload, () => {
      this.#pendingBytes -= payload.length;
    });
  }

  sendStart(start: Record<string, unknown>): void {
    this.#send(start);
  }

  /** The caller's voice, as a media_input event. */
  sendAudioChunk(pcmBase64: string): void {
    this.#send(
      {
        event: "media_input",
        stream_id: this.streamId,
        media: { payload: pcmBase64 },
      },
      true,
    );
  }

  sendDtmf(digit: string): void {
    this.#send({ event: "dtmf", stream_id: this.streamId, digit });
  }

  /**
   * Hand arbitrary context to the Line agent's own code.
   *
   * The only channel this wire has for call context, which is why participant
   * counts and recording changes arrive here rather than as a prompt update.
   */
  sendCustom(metadata: Record<string, unknown>): void {
    this.#send({ event: "custom", stream_id: this.streamId, metadata });
  }

  /** Close the stream. Safe to call twice. */
  async aclose(): Promise<void> {
    if (this.#closedByUs) return;
    this.#closedByUs = true;
    if (this.#keepalive) clearInterval(this.#keepalive);
    const ws = this.#ws;
    if (ws !== undefined && ws.readyState === WebSocket.OPEN)
      ws.close(1000, "session-end");
  }
}
