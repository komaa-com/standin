// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One Deepgram Voice Agent conversation, as a socket.
 *
 * Thin on purpose: framing and send helpers only. What any of it means to a
 * Microsoft Teams call is in `handler.ts`.
 *
 * Two things about this wire are worth knowing before reading the code. Audio
 * travels as raw BINARY frames in both directions, not base64 inside JSON, so
 * the hot path here is a copy rather than an encode. And the session is pinned
 * to `linear16` at 16 kHz both ways, which is exactly what StandIn speaks, so
 * nothing in this plugin resamples anything.
 *
 * The Python twin is `standin.plugins.deepgram.agent`.
 */

import { WebSocket } from "ws";

import { logger } from "../../log.js";
import type { DeepgramConfig } from "./config.js";

/** The one wire rate: StandIn's PCM16 at 16 kHz is Deepgram's linear16 at 16000. */
export const WIRE_SAMPLE_RATE_HZ = 16_000;

/**
 * The socket idles out when no audio is flowing, which happens whenever the
 * caller is simply listening. Cheap to send, and sent for the whole call.
 */
const KEEPALIVE_INTERVAL_MS = 8_000;

const CONNECT_TIMEOUT_MS = 10_000;

/** How long to wait for the server's Welcome. Settings may not be sent before it. */
const WELCOME_TIMEOUT_MS = 10_000;

const MAX_SEND_BUFFER_BYTES = 1024 * 1024;

/** A JSON frame from Deepgram. Only `type` is guaranteed. */
export type AgentMessage = Record<string, unknown> & { type: string };

/** Caller details folded into the prompt. */
export interface CallerContext {
  callerName: string;
  tenantId: string;
  direction: string;
}

/**
 * Assemble the agent prompt: your instructions, who is calling, and what has
 * happened on the call so far.
 *
 * The live notes are here rather than in their own message because the Voice
 * Agent API has no non-interrupting context channel. Context rides an updated
 * prompt instead, which is why the handler keeps that list bounded.
 */
export function buildPrompt(
  config: DeepgramConfig,
  caller: CallerContext,
  notes: readonly string[] = [],
): string {
  const lines = [
    config.instructions,
    "",
    `Call context: you are speaking with ${caller.callerName} ` +
      `(tenant: ${caller.tenantId}) on an ${caller.direction} call.`,
  ];
  if (notes.length > 0) {
    lines.push(
      "",
      "Live call context (most recent last):",
      ...notes.map((n) => `- ${n}`),
    );
  }
  return lines.join("\n");
}

/** The Settings message, sent once per call right after Welcome. */
export function buildSettings(
  config: DeepgramConfig,
  prompt: string,
  functions: Array<Record<string, unknown>>,
): Record<string, unknown> {
  const think: Record<string, unknown> = {
    provider: { type: config.thinkProvider, model: config.thinkModel },
    prompt,
    functions,
  };
  if (config.thinkEndpointUrl) {
    const endpoint: Record<string, unknown> = { url: config.thinkEndpointUrl };
    if (Object.keys(config.thinkEndpointHeaders).length > 0) {
      endpoint.headers = config.thinkEndpointHeaders;
    }
    think.endpoint = endpoint;
  }

  const agent: Record<string, unknown> = {
    listen: {
      provider: {
        type: "deepgram",
        model: config.listenModel,
        language: config.language,
      },
    },
    think,
    speak: {
      provider: {
        type: "deepgram",
        model: config.speakModel,
        language: config.language,
      },
    },
  };
  if (config.greeting) agent.greeting = config.greeting;

  return {
    type: "Settings",
    audio: {
      // "container" is an output-side field: every official Settings example
      // omits it on input, and sending it there is rejected.
      input: { encoding: "linear16", sample_rate: WIRE_SAMPLE_RATE_HZ },
      output: {
        encoding: "linear16",
        sample_rate: WIRE_SAMPLE_RATE_HZ,
        container: "none",
      },
    },
    agent,
  };
}

/** What the relay needs from an agent connection. Tests substitute their own. */
export interface AgentPort {
  readonly isOpen: boolean;
  sendSettings(settings: Record<string, unknown>): void;
  sendAudio(pcm: Buffer): void;
  updatePrompt(prompt: string): void;
  injectAgentMessage(text: string): void;
  sendFunctionResult(callId: string, name: string, content: string): void;
  aclose(): Promise<void>;
}

/** The socket to one Deepgram Voice Agent conversation. */
export class AgentSocket implements AgentPort {
  #ws: WebSocket | undefined;
  #keepalive: NodeJS.Timeout | undefined;
  #pendingBytes = 0;
  #dropped = 0;
  #lastDropWarning = 0;
  #closedByUs = false;

  private constructor(private readonly config: DeepgramConfig) {}

  /** Open the socket and wait for Welcome. Retries once on a transient failure. */
  static async connect(
    config: DeepgramConfig,
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onAudio: (pcm: Buffer) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): Promise<AgentSocket> {
    const socket = new AgentSocket(config);
    try {
      await socket.#open();
    } catch (err) {
      logger.warn(
        `standin: Deepgram connect failed (${String(err)}); retrying once`,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      await socket.#open();
    }
    socket.#listen(onMessage, onAudio, onClose);
    socket.#keepalive = setInterval(() => {
      socket.#send({ type: "KeepAlive" });
    }, KEEPALIVE_INTERVAL_MS);
    socket.#keepalive.unref?.();
    return socket;
  }

  async #open(): Promise<void> {
    const ws = new WebSocket(
      `wss://${this.config.agentHost}/v1/agent/converse`,
      {
        headers: { authorization: `Token ${this.config.apiKey}` },
        maxPayload: 16 * 1024 * 1024,
      },
    );
    await new Promise<void>((resolve, reject) => {
      const fail = setTimeout(() => {
        ws.terminate();
        reject(new Error("the Deepgram socket did not open in time"));
      }, CONNECT_TIMEOUT_MS);
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
    await this.#awaitWelcome(ws);
    this.#ws = ws;
  }

  async #awaitWelcome(ws: WebSocket): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup();
        ws.terminate();
        reject(new Error("no Welcome from Deepgram within the timeout"));
      }, WELCOME_TIMEOUT_MS);
      timer.unref?.();

      const onMessage = (
        raw: Buffer | ArrayBuffer | Buffer[],
        isBinary: boolean,
      ): void => {
        if (isBinary) return; // audio cannot arrive before Settings; ignore it
        let message: unknown;
        try {
          message = JSON.parse(raw.toString());
        } catch {
          return;
        }
        if (typeof message !== "object" || message === null) return;
        const typed = message as Record<string, unknown>;
        if (typed.type === "Welcome") {
          cleanup();
          resolve();
          return;
        }
        if (typed.type === "Error") {
          // A bad key or a bad config must fail HERE with the real reason,
          // rather than being swallowed until the Welcome timeout reports
          // something generic ten seconds later.
          cleanup();
          ws.terminate();
          reject(
            new Error(
              `Deepgram rejected the session: ${String(typed.code ?? "unknown")}: ` +
                String(typed.description ?? "no description"),
            ),
          );
        }
      };
      const onClose = (): void => {
        cleanup();
        reject(new Error("the socket closed before Welcome"));
      };
      const cleanup = (): void => {
        clearTimeout(timer);
        ws.off("message", onMessage);
        ws.off("close", onClose);
      };
      ws.on("message", onMessage);
      ws.once("close", onClose);
    });
  }

  #listen(
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onAudio: (pcm: Buffer) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): void {
    const ws = this.#ws;
    if (ws === undefined) return;

    ws.on("message", (raw: Buffer, isBinary: boolean) => {
      if (isBinary) {
        void Promise.resolve(onAudio(Buffer.from(raw))).catch(
          (err: unknown) => {
            logger.error(
              `standin: handling Deepgram audio failed: ${String(err)}`,
            );
          },
        );
        return;
      }
      let message: unknown;
      try {
        message = JSON.parse(raw.toString());
      } catch {
        logger.warn("standin: Deepgram sent an unparseable frame; dropping");
        return;
      }
      if (typeof message !== "object" || message === null) return;
      const typed = message as Record<string, unknown>;
      if (typeof typed.type !== "string") return;
      void Promise.resolve(onMessage(typed as AgentMessage)).catch(
        (err: unknown) => {
          logger.error(
            `standin: handling Deepgram ${typed.type as string} failed: ${String(err)}`,
          );
        },
      );
    });

    ws.on("close", (code, reason) => {
      if (this.#keepalive) clearInterval(this.#keepalive);
      void onClose(code, reason.toString());
    });
    ws.on("error", (err) => {
      logger.warn(`standin: the Deepgram socket failed: ${String(err)}`);
    });
  }

  get isOpen(): boolean {
    return this.#ws !== undefined && this.#ws.readyState === WebSocket.OPEN;
  }

  #send(message: Record<string, unknown>): void {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) return;
    const payload = JSON.stringify(message);
    this.#pendingBytes += payload.length;
    ws.send(payload, () => {
      this.#pendingBytes -= payload.length;
    });
  }

  /**
   * The caller's voice, as a raw binary frame.
   *
   * Dropped rather than queued past the buffer ceiling: on a stalled socket the
   * alternative is an unbounded pile of stale audio, and stale caller audio is
   * worth nothing by the time it arrives.
   */
  sendAudio(pcm: Buffer): void {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) return;
    if (this.#pendingBytes > MAX_SEND_BUFFER_BYTES) {
      this.#dropped += 1;
      const now = Date.now();
      if (now - this.#lastDropWarning >= 1000) {
        logger.warn(
          `standin: Deepgram send backpressure, dropped ${this.#dropped} frame(s)`,
        );
        this.#lastDropWarning = now;
        this.#dropped = 0;
      }
      return;
    }
    this.#pendingBytes += pcm.length;
    ws.send(pcm, { binary: true }, () => {
      this.#pendingBytes -= pcm.length;
    });
  }

  sendSettings(settings: Record<string, unknown>): void {
    this.#send(settings);
  }

  /** Replace the agent's prompt mid-call, which is how context arrives. */
  updatePrompt(prompt: string): void {
    this.#send({ type: "UpdatePrompt", prompt });
  }

  /** Make the agent say this, now, interrupting whatever it was saying. */
  injectAgentMessage(text: string): void {
    this.#send({ type: "InjectAgentMessage", content: text });
  }

  sendFunctionResult(callId: string, name: string, content: string): void {
    this.#send({ type: "FunctionCallResponse", id: callId, name, content });
  }

  /** Close the conversation. Safe to call twice. */
  async aclose(): Promise<void> {
    if (this.#closedByUs) return;
    this.#closedByUs = true;
    if (this.#keepalive) clearInterval(this.#keepalive);
    const ws = this.#ws;
    if (ws !== undefined && ws.readyState === WebSocket.OPEN)
      ws.close(1000, "session-end");
  }
}
