// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One OpenAI Realtime session, as a socket.
 *
 * The one place in the SDK where the wire rate and the model rate disagree.
 * StandIn speaks PCM16 at 16 kHz; the Realtime API speaks PCM at 24 kHz and
 * nothing else. This file owns that conversion in both directions, so the
 * handler above it and the call below it both stay at their own rate and
 * neither has to think about it.
 *
 * The Python SDK's `standin.audio.resample_pcm16` is the same resampler, driven
 * by the same conformance vectors, so a call sounds identical whichever
 * language the plugin is written in.
 */

import { WebSocket } from "ws";

import { REALTIME_SAMPLE_RATE_HZ, resamplePcm16 } from "../../audio.js";
import { toolSchemas } from "../../callTools.js";
import { logger } from "../../log.js";
import { SAMPLE_RATE_HZ } from "../../protocol.js";
import type { OpenAIConfig } from "./config.js";

const CONNECT_TIMEOUT_MS = 10_000;

/** A stalled socket must not accumulate an unbounded queue of caller audio. */
const MAX_SEND_BUFFER_BYTES = 1024 * 1024;

/** An event from the Realtime API. Only `type` is guaranteed. */
export type AgentMessage = Record<string, unknown> & { type: string };

/** Caller details folded into the session instructions. */
export interface CallerContext {
  callerName: string;
  tenantId: string;
  direction: string;
}

/**
 * The call capabilities every session gets, declared as tools.
 *
 * The list is the SDK's, in the Realtime API's shape, so every provider offers
 * the same agent and a new capability needs no edit here.
 */
export const BUILT_IN_TOOLS: Array<Record<string, unknown>> =
  toolSchemas("openai");

/** The names above, for the shadowing check. */
export const BUILT_IN_TOOL_NAMES: ReadonlySet<string> = new Set(
  BUILT_IN_TOOLS.map((tool) => tool.name as string),
);

/**
 * Normalise a remote MCP tool entry for the session's tools array.
 *
 * The Realtime API dials the server itself and runs those tools server-side, so
 * nothing here executes at call time. Approval defaults to never because a live
 * voice call has no approval interface, and a pending approval is a caller
 * listening to silence.
 */
export function mcpTool(
  entry: Record<string, unknown>,
): Record<string, unknown> {
  if (
    typeof entry.server_label !== "string" ||
    typeof entry.server_url !== "string"
  ) {
    throw new Error("an MCP tool entry needs server_label and server_url");
  }
  return { require_approval: "never", ...entry, type: "mcp" };
}

/** Compose the effective instructions: your prompt, the caller, and tool guidance. */
export function buildInstructions(base: string, caller: CallerContext): string {
  return [
    base.trim(),
    `The caller's name is ${caller.callerName}. Their organization id is ` +
      `${caller.tenantId}. This is an ${caller.direction} call.`,
    "Use the end_call tool when the conversation is over. Use the look tool when the " +
      "caller refers to something on their camera or shared screen. Use show_image to put " +
      "a picture on your video tile, and express to show an emotion on your avatar.",
  ].join("\n\n");
}

/** Per-call session configuration, the Realtime equivalent of a conversation init. */
export function buildSessionUpdate(
  config: OpenAIConfig,
  instructions: string,
  extraTools: Array<Record<string, unknown>> = [],
): Record<string, unknown> {
  const input: Record<string, unknown> = {
    format: { type: "audio/pcm", rate: REALTIME_SAMPLE_RATE_HZ },
    // interrupt_response cancels the in-flight response server-side the moment
    // the caller speaks. The handler mirrors that to the call, which is what
    // actually stops the bot talking.
    turn_detection: {
      type: config.vadType,
      create_response: true,
      interrupt_response: true,
    },
  };
  if (config.transcriptionModel)
    input.transcription = { model: config.transcriptionModel };

  const output: Record<string, unknown> = {
    format: { type: "audio/pcm", rate: REALTIME_SAMPLE_RATE_HZ },
  };
  if (config.voice) output.voice = config.voice;

  return {
    type: "session.update",
    session: {
      type: "realtime",
      output_modalities: ["audio"],
      instructions,
      audio: { input, output },
      tools: [...BUILT_IN_TOOLS, ...extraTools],
      tool_choice: "auto",
    },
  };
}

/** What the relay needs from a session. Tests substitute their own. */
export interface AgentPort {
  readonly isOpen: boolean;
  sendSessionUpdate(update: Record<string, unknown>): void;
  /** Caller audio at the WIRE rate. This resamples up to the model's rate. */
  sendAudio(pcm: Buffer): void;
  sendContext(text: string): void;
  sendUserTurn(text: string): void;
  cancelResponse(): void;
  sendToolResult(callId: string, output: string): void;
  aclose(): Promise<void>;
}

/** The socket to one OpenAI Realtime session. */
export class AgentSocket implements AgentPort {
  #ws: WebSocket | undefined;
  #pendingBytes = 0;
  #dropped = 0;
  #lastDropWarning = 0;
  #closedByUs = false;

  private constructor() {}

  /** Open the session, retrying once on a transient failure. */
  static async connect(
    config: OpenAIConfig,
    onMessage: (message: AgentMessage) => void | Promise<void>,
    /** Agent audio, already resampled DOWN to the wire rate. */
    onAudio: (pcm: Buffer) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): Promise<AgentSocket> {
    const socket = new AgentSocket();
    const open = async (): Promise<WebSocket> => {
      const url = `wss://${config.host}/v1/realtime?model=${encodeURIComponent(config.model)}`;
      const ws = new WebSocket(url, {
        headers: { authorization: `Bearer ${config.apiKey}` },
        maxPayload: 16 * 1024 * 1024,
      });
      await new Promise<void>((resolve, reject) => {
        const fail = setTimeout(() => {
          ws.terminate();
          reject(new Error("the OpenAI Realtime socket did not open in time"));
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
      return ws;
    };

    try {
      socket.#ws = await open();
    } catch (err) {
      logger.warn(
        `standin: OpenAI connect failed (${String(err)}); retrying once`,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      socket.#ws = await open();
    }
    socket.#listen(onMessage, onAudio, onClose);
    return socket;
  }

  #listen(
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onAudio: (pcm: Buffer) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): void {
    const ws = this.#ws;
    if (ws === undefined) return;

    ws.on("message", (raw: Buffer) => {
      let message: unknown;
      try {
        message = JSON.parse(raw.toString());
      } catch {
        logger.warn("standin: OpenAI sent an unparseable frame; dropping");
        return;
      }
      if (typeof message !== "object" || message === null) return;
      const typed = message as Record<string, unknown>;
      if (typeof typed.type !== "string") return;

      // The hot path first: audio deltas are resampled here so nothing above
      // this line ever sees the model's rate.
      if (
        typed.type === "response.output_audio.delta" &&
        typeof typed.delta === "string"
      ) {
        const atModelRate = Buffer.from(typed.delta, "base64");
        if (atModelRate.length > 0) {
          const atWireRate = resamplePcm16(
            atModelRate,
            REALTIME_SAMPLE_RATE_HZ,
            SAMPLE_RATE_HZ,
          );
          void Promise.resolve(onAudio(atWireRate)).catch((err: unknown) => {
            logger.error(
              `standin: handling OpenAI audio failed: ${String(err)}`,
            );
          });
        }
        return;
      }
      void Promise.resolve(onMessage(typed as AgentMessage)).catch(
        (err: unknown) => {
          logger.error(
            `standin: handling OpenAI ${typed.type as string} failed: ${String(err)}`,
          );
        },
      );
    });

    ws.on("close", (code, reason) => {
      void onClose(code, reason.toString());
    });
    ws.on("error", (err) => {
      logger.warn(`standin: the OpenAI Realtime socket failed: ${String(err)}`);
    });
  }

  get isOpen(): boolean {
    return this.#ws !== undefined && this.#ws.readyState === WebSocket.OPEN;
  }

  #send(message: Record<string, unknown>, droppable = false): void {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) return;
    const payload = JSON.stringify(message);
    if (droppable && this.#pendingBytes > MAX_SEND_BUFFER_BYTES) {
      this.#dropped += 1;
      const now = Date.now();
      if (now - this.#lastDropWarning >= 1000) {
        logger.warn(
          `standin: OpenAI send backpressure, dropped ${this.#dropped} chunk(s)`,
        );
        this.#lastDropWarning = now;
        this.#dropped = 0;
      }
      return;
    }
    this.#pendingBytes += payload.length;
    ws.send(payload, () => {
      this.#pendingBytes -= payload.length;
    });
  }

  sendSessionUpdate(update: Record<string, unknown>): void {
    this.#send(update);
  }

  /** Caller audio at the wire rate, resampled up to the model's rate. */
  sendAudio(pcm: Buffer): void {
    const atModelRate = resamplePcm16(
      pcm,
      SAMPLE_RATE_HZ,
      REALTIME_SAMPLE_RATE_HZ,
    );
    this.#send(
      {
        type: "input_audio_buffer.append",
        audio: atModelRate.toString("base64"),
      },
      true,
    );
  }

  /**
   * Background context, with no response requested, so the model sees it on its
   * next turn rather than interrupting to acknowledge it.
   */
  sendContext(text: string): void {
    this.#send({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text }],
      },
    });
  }

  /** An interrupting user turn, which is how a goodbye gets spoken. */
  sendUserTurn(text: string): void {
    this.#send({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text }],
      },
    });
    this.#send({ type: "response.create" });
  }

  cancelResponse(): void {
    this.#send({ type: "response.cancel" });
  }

  /**
   * Answer a tool call, then ask for the turn that speaks the answer. Without
   * the second message the model has the result and says nothing about it.
   */
  sendToolResult(callId: string, output: string): void {
    this.#send({
      type: "conversation.item.create",
      item: { type: "function_call_output", call_id: callId, output },
    });
    this.#send({ type: "response.create" });
  }

  /** Close the session. Safe to call twice. */
  async aclose(): Promise<void> {
    if (this.#closedByUs) return;
    this.#closedByUs = true;
    const ws = this.#ws;
    if (ws !== undefined && ws.readyState === WebSocket.OPEN)
      ws.close(1000, "session-end");
  }
}
