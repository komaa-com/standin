// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One ElevenLabs agent conversation, as a socket.
 *
 * Deliberately thin: this file opens the socket, parses frames, and offers send
 * helpers. What any of it MEANS to a Microsoft Teams call lives in `handler.ts`,
 * so the wire and the relay can be read, and tested, one at a time.
 *
 * The contract that makes this plugin simple is the audio format. An agent
 * configured for `pcm_16000` in both directions speaks exactly what StandIn
 * speaks, so nothing here resamples anything. An agent configured for something
 * else would produce a whole call of garbled audio, so it is caught at the
 * metadata frame and refused.
 *
 * The Python twin is `standin.plugins.elevenlabs.agent`.
 */

import { WebSocket } from "ws";

import { logger } from "../../log.js";
import type { ElevenLabsConfig } from "./config.js";

/** Bound on the REST calls and the socket open, so a hung API cannot wedge onStart. */
const REST_TIMEOUT_MS = 10_000;

/**
 * ElevenLabs pings roughly every ten seconds, so a minute of silence means the
 * peer is gone without a close frame.
 */
const RECEIVE_TIMEOUT_MS = 60_000;

/** A stalled socket must not accumulate an unbounded queue of caller audio. */
const MAX_SEND_BUFFER_BYTES = 1024 * 1024;

/** The only audio format this plugin speaks, and what StandIn speaks. */
const REQUIRED_FORMAT = "pcm_16000";

const EXT_FOR_MIME: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/jpg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
  "image/gif": "gif",
};

/** A frame from ElevenLabs. Only `type` is guaranteed. */
export type AgentMessage = Record<string, unknown> & { type: string };

/** The conversation_initiation_client_data that opens a call. */
export interface ConversationInit {
  dynamicVariables: Record<string, string>;
  firstMessage?: string;
  environment?: string;
  /**
   * A stable per-person id, which is what ElevenLabs keys analytics and memory
   * on. Pass the caller's directory id when there is one and NOTHING when there
   * is not: a shared default would make every anonymous caller the same person,
   * and one caller would read another's conversation memory.
   */
  userId?: string;
  branchId?: string;
}

/** Build the message that opens a conversation. */
export function buildConversationInit(
  init: ConversationInit,
): Record<string, unknown> {
  const message: Record<string, unknown> = {
    type: "conversation_initiation_client_data",
    dynamic_variables: init.dynamicVariables,
  };
  // Overrides are rejected unless the agent's own security settings allow them,
  // so send one only when it was actually configured.
  if (init.firstMessage) {
    message.conversation_config_override = {
      agent: { first_message: init.firstMessage },
    };
  }
  if (init.environment) message.environment = init.environment;
  if (init.userId) message.user_id = init.userId;
  if (init.branchId) message.branch_id = init.branchId;
  return message;
}

async function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  what: string,
): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error(`${what} timed out after ${ms}ms`)),
          ms,
        );
        timer.unref?.();
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Mint a short-lived signed URL. Expires in about fifteen minutes, so never cached. */
export async function getSignedUrl(config: ElevenLabsConfig): Promise<string> {
  const params = new URLSearchParams({ agent_id: config.agentId });
  if (config.environment) params.set("environment", config.environment);
  const url = `https://${config.host}/v1/convai/conversation/get-signed-url?${params}`;
  const response = await withTimeout(
    fetch(url, { headers: { "xi-api-key": config.apiKey } }),
    REST_TIMEOUT_MS,
    "get-signed-url",
  );
  if (!response.ok)
    throw new Error(`get-signed-url failed: HTTP ${response.status}`);
  const body = (await response.json()) as { signed_url?: string };
  if (!body.signed_url)
    throw new Error("get-signed-url returned no signed_url");
  return body.signed_url;
}

/**
 * Upload one frame to the live conversation and return its file id.
 *
 * This PERSISTS the caller's screen or face with ElevenLabs, which is why the
 * handler gates it on the call being recorded.
 */
export async function uploadConversationFile(
  config: ElevenLabsConfig,
  conversationId: string,
  data: Buffer,
  mime: string,
): Promise<string> {
  const ext = EXT_FOR_MIME[mime.toLowerCase()];
  if (!ext) throw new Error(`unsupported image type for upload: ${mime}`);
  const form = new FormData();
  form.append(
    "file",
    new Blob([new Uint8Array(data)], { type: mime }),
    `frame.${ext}`,
  );
  const url = `https://${config.host}/v1/convai/conversations/${encodeURIComponent(conversationId)}/files`;
  const response = await withTimeout(
    fetch(url, {
      method: "POST",
      body: form,
      headers: { "xi-api-key": config.apiKey },
    }),
    REST_TIMEOUT_MS,
    "file upload",
  );
  if (!response.ok)
    throw new Error(`file upload failed: HTTP ${response.status}`);
  const body = (await response.json()) as { file_id?: string };
  if (!body.file_id) throw new Error("file upload returned no file_id");
  return body.file_id;
}

/** What the relay needs from an agent connection. Tests substitute their own. */
export interface AgentPort {
  readonly isOpen: boolean;
  conversationId: string | undefined;
  sendConversationInit(init: Record<string, unknown>): void;
  sendAudioChunk(pcmBase64: string): void;
  sendPong(eventId: number): void;
  sendContextualUpdate(text: string): void;
  sendUserMessage(text: string): void;
  sendToolResult(toolCallId: string, result: string, isError?: boolean): void;
  attachImage(data: Buffer, mime: string, question: string): Promise<void>;
  aclose(): Promise<void>;
}

/** The socket to one ElevenLabs agent conversation. */
export class AgentSocket implements AgentPort {
  #config: ElevenLabsConfig;
  #ws: WebSocket | undefined;
  #idleTimer: NodeJS.Timeout | undefined;
  #pendingBytes = 0;
  #dropped = 0;
  #lastDropWarning = 0;
  #closedByUs = false;
  conversationId: string | undefined;

  private constructor(config: ElevenLabsConfig) {
    this.#config = config;
  }

  /**
   * Open the socket, retrying once with a fresh signed URL.
   *
   * Signed URLs are short-lived and minting one can fail transiently. One retry
   * costs a quarter of a second and saves a caller from a dropped call because a
   * URL expired between minting and connecting.
   */
  static async connect(
    config: ElevenLabsConfig,
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): Promise<AgentSocket> {
    const socket = new AgentSocket(config);
    try {
      await socket.#open();
    } catch (err) {
      logger.warn(
        `standin: ElevenLabs connect failed (${String(err)}); retrying with a fresh URL`,
      );
      await new Promise((resolve) => setTimeout(resolve, 250));
      await socket.#open();
    }
    socket.#listen(onMessage, onClose);
    return socket;
  }

  async #open(): Promise<void> {
    const signedUrl = await getSignedUrl(this.#config);
    const ws = new WebSocket(signedUrl, { maxPayload: 16 * 1024 * 1024 });
    await withTimeout(
      new Promise<void>((resolve, reject) => {
        ws.once("open", resolve);
        ws.once("error", reject);
      }),
      REST_TIMEOUT_MS,
      "the ElevenLabs socket open",
    );
    this.#ws = ws;
  }

  #listen(
    onMessage: (message: AgentMessage) => void | Promise<void>,
    onClose: (code: number, reason: string) => void | Promise<void>,
  ): void {
    const ws = this.#ws;
    if (ws === undefined) return;
    let closeReason = "";

    const armIdle = (): void => {
      if (this.#idleTimer) clearTimeout(this.#idleTimer);
      this.#idleTimer = setTimeout(() => {
        closeReason = "receive-timeout";
        ws.terminate();
      }, RECEIVE_TIMEOUT_MS);
      this.#idleTimer.unref?.();
    };
    armIdle();

    ws.on("message", (raw) => {
      armIdle();
      let message: unknown;
      try {
        message = JSON.parse(raw.toString());
      } catch {
        logger.warn("standin: ElevenLabs sent an unparseable frame; dropping");
        return;
      }
      if (typeof message !== "object" || message === null) return;
      const typed = message as Record<string, unknown>;
      if (typeof typed.type !== "string") return;

      if (
        typed.type === "conversation_initiation_metadata" &&
        !this.#readMetadata(typed)
      ) {
        closeReason = "audio-format-mismatch";
        void this.aclose();
        return;
      }
      // A handler error must never escape and kill the relay for the call.
      void (async () => {
        try {
          await onMessage(typed as AgentMessage);
        } catch (err) {
          logger.error(
            `standin: handling ElevenLabs ${typed.type} failed: ${String(err)}`,
          );
        }
      })();
    });

    ws.on("close", (code, reason) => {
      if (this.#idleTimer) clearTimeout(this.#idleTimer);
      void onClose(code, closeReason || reason.toString());
    });
    ws.on("error", (err) => {
      logger.warn(`standin: the ElevenLabs socket failed: ${String(err)}`);
    });
  }

  #readMetadata(message: Record<string, unknown>): boolean {
    const meta = (message.conversation_initiation_metadata_event ??
      {}) as Record<string, unknown>;
    if (typeof meta.conversation_id === "string")
      this.conversationId = meta.conversation_id;
    const formats = [
      meta.agent_output_audio_format,
      meta.user_input_audio_format,
    ];
    const wrong = formats.find(
      (f) => typeof f === "string" && f !== REQUIRED_FORMAT,
    );
    if (wrong) {
      // Refuse rather than log and carry on. The alternative is a call that
      // stays up for its whole duration while the caller hears noise.
      logger.error(
        `standin: the ElevenLabs agent audio is ${String(wrong)}, expected ${REQUIRED_FORMAT} ` +
          "both ways; ending the call",
      );
      return false;
    }
    return true;
  }

  get isOpen(): boolean {
    return this.#ws !== undefined && this.#ws.readyState === WebSocket.OPEN;
  }

  #send(message: Record<string, unknown>, droppable = false): void {
    const ws = this.#ws;
    if (ws === undefined || ws.readyState !== WebSocket.OPEN) return;
    const payload = JSON.stringify(message);
    if (droppable && this.#pendingBytes > MAX_SEND_BUFFER_BYTES) {
      // Realtime audio is the only droppable thing here: control messages are
      // tiny and load-bearing, so they always queue.
      this.#dropped += 1;
      const now = Date.now();
      if (now - this.#lastDropWarning >= 1000) {
        logger.warn(
          `standin: ElevenLabs send backpressure, dropped ${this.#dropped} chunk(s)`,
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

  /** Caller audio, forwarded verbatim. This message carries no `type`. */
  sendAudioChunk(pcmBase64: string): void {
    this.#send({ user_audio_chunk: pcmBase64 }, true);
  }

  sendConversationInit(init: Record<string, unknown>): void {
    this.#send(init);
  }

  sendPong(eventId: number): void {
    this.#send({ type: "pong", event_id: eventId });
  }

  /** Background context that must NOT interrupt the agent mid-sentence. */
  sendContextualUpdate(text: string): void {
    this.#send({ type: "contextual_update", text });
  }

  /** An interrupting user turn, which is how a goodbye gets spoken. */
  sendUserMessage(text: string): void {
    this.#send({ type: "user_message", text });
  }

  sendToolResult(toolCallId: string, result: string, isError = false): void {
    this.#send({
      type: "client_tool_result",
      tool_call_id: toolCallId,
      result,
      is_error: isError,
    });
  }

  /** Upload a frame and inject it as a multimodal user turn. */
  async attachImage(
    data: Buffer,
    mime: string,
    question: string,
  ): Promise<void> {
    if (!this.conversationId)
      throw new Error("the conversation has not started yet");
    const fileId = await uploadConversationFile(
      this.#config,
      this.conversationId,
      data,
      mime,
    );
    this.#send({
      type: "multimodal_message",
      text: { type: "user_message", text: question },
      file: { type: "file_input", file_id: fileId },
    });
  }

  /** Close the conversation. Safe to call twice. */
  async aclose(): Promise<void> {
    if (this.#closedByUs) return;
    this.#closedByUs = true;
    if (this.#idleTimer) clearTimeout(this.#idleTimer);
    const ws = this.#ws;
    if (ws !== undefined && ws.readyState === WebSocket.OPEN) {
      ws.close(1000, "session-end");
    }
  }
}
