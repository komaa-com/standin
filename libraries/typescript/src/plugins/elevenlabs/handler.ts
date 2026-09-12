// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The relay: one Microsoft Teams call on one side, one ElevenLabs agent on the
 * other.
 *
 * A plain `CallHandler`. Everything that is the same for every framework - the
 * socket StandIn dials, the handshake, capacity, the frame loop, the watchdogs -
 * belongs to `CallServer`, so what is left here is only what ElevenLabs needs.
 *
 * The client tools it answers are the SDK's call capabilities, so this agent can
 * do what every other provider's can. ElevenLabs declares client tools on the
 * agent rather than over the wire, so {@link clientTools} returns the exact
 * declarations to paste in rather than leaving you to retype them.
 *
 * Two of them mean something slightly different here, and the difference is the
 * reason they are not the SDK's implementations. `look` uploads the frame into
 * the conversation for the model to read directly, which PERSISTS the caller's
 * screen with ElevenLabs and is why it takes a recorded call. `show_image` also
 * accepts inline bytes, which no other provider offers.
 *
 * The Python twin is `standin.plugins.elevenlabs.handler`, method for
 * method.
 */

import type { CallHandler, CallSession } from "../../handler.js";
import { toolSchemas } from "../../callTools.js";
import { fetchPublicImage } from "../../fetch.js";
import { logger } from "../../log.js";
import { StartupBuffer } from "../../startup.js";
import {
  DISPLAY_IMAGE_MIME_TYPES,
  MAX_IMAGE_BYTES,
  type VideoFrame,
  type VideoSource,
} from "../../vision.js";
import { KeyframeStore } from "../../visionTools.js";
import {
  AgentSocket,
  buildConversationInit,
  type AgentMessage,
  type AgentPort,
} from "./agent.js";
import { elevenLabsConfigFromEnv, type ElevenLabsConfig } from "./config.js";

/**
 * Bounds on what a model may put in an emotion or a caption. These reach the
 * avatar tile, and an unbounded string from a model is an unbounded string from
 * whoever is steering it.
 */
const MAX_EMOTION_CHARS = 40;
const MAX_CAPTION_CHARS = 200;
const MAX_MODE_CHARS = 20;

const IMAGE_FETCH_TIMEOUT_MS = 10_000;

/**
 * Caller audio and context that arrive while the conversation is still opening.
 * Bounded: on a socket that never opens these would grow for the whole call.
 */
const MAX_PENDING_AUDIO = 200;
const MAX_PENDING_CONTEXT = 20;

/**
 * The client tools to declare on the ElevenLabs agent.
 *
 * ElevenLabs configures tools on the agent, not over the wire, so there is
 * nothing this plugin can send. What it can do is hand you the declarations
 * verbatim. The names and descriptions are the SDK's, so this agent is told
 * about the same capabilities as every other provider's. `show_image` is
 * widened here because ElevenLabs is the one provider that can take the bytes
 * inline.
 */
export function clientTools(): Array<Record<string, unknown>> {
  return toolSchemas("flat").map((tool) => {
    if (tool.name !== "show_image") return tool;
    const parameters = tool.parameters as {
      type: string;
      properties: Record<string, unknown>;
      required: string[];
    };
    return {
      ...tool,
      description:
        `${tool.description as string} You can give the image inline instead, ` +
        "as 'dataBase64' with its 'mime'.",
      parameters: {
        ...parameters,
        properties: {
          ...parameters.properties,
          dataBase64: {
            type: "string",
            description:
              "The image itself, base64 encoded. An alternative to 'url'.",
          },
          mime: {
            type: "string",
            description: `Required with dataBase64: one of ${DISPLAY_IMAGE_MIME_TYPES.join(", ")}.`,
          },
          durationMs: {
            type: "integer",
            description: "How long to leave it up, in milliseconds.",
          },
        },
        // Either form will do, so neither argument can be required.
        required: [],
      },
    };
  });
}

/** How the handler reaches ElevenLabs. Replaceable so tests need no network. */
export type Connect = (
  config: ElevenLabsConfig,
  onMessage: (message: AgentMessage) => void | Promise<void>,
  onClose: (code: number, reason: string) => void | Promise<void>,
) => Promise<AgentPort>;

/** One Microsoft Teams call answered by one ElevenLabs agent. */
export class ElevenLabsHandler implements CallHandler {
  #config: ElevenLabsConfig;
  #connect: Connect;
  #call: CallSession | undefined;
  // Frames kept so a question about a slide already gone can still be
  // answered. The store keeps nothing unless the call is recorded.
  readonly #keyframes = new KeyframeStore();
  #agent: AgentPort | undefined;
  #closed = false;
  // Holds BOTH the caller's first words and the first context, so neither
  // is lost while the provider is still connecting.
  readonly #pending = new StartupBuffer();
  #pendingAudio: string[] = [];
  // Audio already in flight when an interruption lands is audio the caller must
  // never hear: the model stopped, and playing the tail would talk over the
  // person who just interrupted.
  #lastAudioEvent = 0;
  #lastInterruptEvent = 0;

  constructor(
    config?: ElevenLabsConfig,
    connect: Connect = AgentSocket.connect.bind(AgentSocket),
  ) {
    this.#config = config ?? elevenLabsConfigFromEnv();
    this.#connect = connect;
  }

  /**
   * Whether the call is being recorded, straight off the session.
   *
   * The server keeps this current from `session.start` and every later
   * `recording.status`, so there is nothing to re-derive here.
   */
  #recording(): boolean {
    return this.#call?.recordingActive ?? false;
  }

  // ---- the SDK seam -------------------------------------------------------

  async onStart(session: CallSession): Promise<void> {
    this.#call = session;
    const caller = session.start.caller;

    let agent: AgentPort;
    try {
      agent = await this.#connect(
        this.#config,
        (message) => this.#onAgentMessage(message),
        (code, reason) => this.#onAgentClose(code, reason),
      );
    } catch (err) {
      logger.error(
        `standin: could not open the ElevenLabs conversation: ${String(err)}`,
      );
      await session.end("agent-unavailable");
      return;
    }

    // The call can end DURING the connect above. Keeping a socket opened after
    // teardown would leave a live, billed conversation with nothing on the
    // other end of it.
    if (this.#closed) {
      await agent.aclose();
      return;
    }
    this.#agent = agent;

    agent.sendConversationInit(
      buildConversationInit({
        dynamicVariables: {
          caller_name: caller.displayName ?? "caller",
          tenant_id: caller.tenantId ?? "unknown-tenant",
          call_direction: session.start.direction,
        },
        firstMessage: this.#config.firstMessage,
        environment: this.#config.environment,
        // Per-person memory, and only when the person is actually identified.
        userId: caller.aadId,
        branchId: this.#config.agentBranchId,
      }),
    );

    // Whatever arrived while the socket was opening. The "there are N people
    // here, stay quiet" signal usually lands exactly in this window.
    await this.#pending.release(
      (pcm: Buffer) => agent.sendAudioChunk(pcm.toString("base64")),
      (text: string) => agent.sendContextualUpdate(text),
    );
    if (this.#pending.dropped.audio || this.#pending.dropped.context) {
      logger.info(
        "standin: the caller outran the agent starting up; some early input was dropped",
      );
    }
  }

  /** The caller's voice, straight through. Both sides are PCM16 at 16 kHz. */
  async onCallerAudio(pcm: Buffer): Promise<void> {
    const chunk = pcm.toString("base64");
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      this.#pending.audio(pcm);
      return;
    }
    agent.sendAudioChunk(chunk);
  }

  /**
   * Keep a short history, so `look_back` has something to look at.
   *
   * The store itself refuses to keep anything unless the call is being
   * recorded, which is the same promise `look` makes.
   */
  async onVideoFrame(frame: VideoFrame): Promise<void> {
    this.#keyframes.offer(frame, this.#call?.recordingActive ?? false);
  }

  /**
   * Participant counts, key presses, recording changes. Sent as a contextual
   * update, which ElevenLabs delivers WITHOUT interrupting mid-sentence.
   */
  async onContext(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      this.#pending.context(text);
      return;
    }
    agent.sendContextualUpdate(text);
  }

  /**
   * StandIn is ending the call and wants this line spoken first. Delivered as a
   * user turn, which interrupts whatever the agent was saying.
   */
  async onGoodbye(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) return;
    this.#lastInterruptEvent = Math.max(
      this.#lastInterruptEvent,
      this.#lastAudioEvent,
    );
    agent.sendUserMessage(
      `[system: the call is ending. Say a brief goodbye now: "${text}"]`,
    );
  }

  /** Close the conversation. Always runs exactly once. */
  async aclose(): Promise<void> {
    this.#closed = true;
    const agent = this.#agent;
    this.#agent = undefined;
    if (agent !== undefined) {
      try {
        await agent.aclose();
      } catch {
        // Teardown must not throw: the slot is freed either way.
      }
    }
  }

  // ---- what ElevenLabs sends us -------------------------------------------

  async #onAgentMessage(message: AgentMessage): Promise<void> {
    switch (message.type) {
      case "audio":
        await this.#onAgentAudio(message);
        break;
      case "interruption":
        await this.#onInterruption(message);
        break;
      case "ping": {
        const event = message.ping_event as { event_id?: unknown } | undefined;
        if (typeof event?.event_id === "number")
          this.#agent?.sendPong(event.event_id);
        break;
      }
      case "client_tool_call": {
        const call = message.client_tool_call as
          Record<string, unknown> | undefined;
        if (
          typeof call?.tool_name === "string" &&
          typeof call.tool_call_id === "string"
        ) {
          void this.#onToolCall(call);
        }
        break;
      }
      case "user_transcript":
      case "agent_response":
        // Gated twice. A transcript in your logs is a recording of the caller,
        // so it takes both an explicit opt-in AND the call being recorded.
        if (this.#config.logTranscripts && this.#recording()) {
          logger.info(`standin: elevenlabs ${message.type}`);
        }
        break;
      default:
        break;
    }
  }

  async #onAgentAudio(message: AgentMessage): Promise<void> {
    const event = message.audio_event as
      { event_id?: unknown; audio_base_64?: unknown } | undefined;
    if (
      typeof event?.event_id !== "number" ||
      typeof event.audio_base_64 !== "string"
    )
      return;
    this.#lastAudioEvent = Math.max(this.#lastAudioEvent, event.event_id);
    if (event.event_id <= this.#lastInterruptEvent) {
      // Audio the model generated before it was interrupted, arriving after.
      // Playing it is exactly the thing barge-in exists to stop.
      return;
    }
    const pcm = Buffer.from(event.audio_base_64, "base64");
    if (pcm.length === 0) return;
    await this.#call?.sendAudio(pcm);
  }

  async #onInterruption(message: AgentMessage): Promise<void> {
    const event = message.interruption_event as
      { event_id?: unknown } | undefined;
    if (typeof event?.event_id !== "number") return;
    this.#lastInterruptEvent = Math.max(
      this.#lastInterruptEvent,
      event.event_id,
    );
    // The only lever that un-sends audio StandIn already has buffered.
    await this.#call?.cancelPlayback();
  }

  async #onAgentClose(code: number, reason: string): Promise<void> {
    logger.info(
      `standin: the ElevenLabs conversation closed (${code} ${reason})`,
    );
    if (!this.#closed) await this.#call?.end("agent-disconnected");
  }

  // ---- the agent's client tools -------------------------------------------

  async #onToolCall(call: Record<string, unknown>): Promise<void> {
    const name = call.tool_name as string;
    const toolCallId = call.tool_call_id as string;
    const params = (
      typeof call.parameters === "object" && call.parameters !== null
        ? call.parameters
        : {}
    ) as Record<string, unknown>;

    switch (name) {
      case "end_call":
        this.#reply(toolCallId, "the call is ending");
        await this.#call?.end("agent-ended-call");
        break;
      case "express":
        await this.#onExpress(toolCallId, params);
        break;
      case "show_image":
        await this.#onShowImage(toolCallId, params);
        break;
      case "look":
        await this.#onLook(toolCallId, params);
        break;
      case "look_back":
        await this.#onLookBack(toolCallId, params);
        break;
      default:
        this.#reply(
          toolCallId,
          `"${name}" is not a tool this plugin answers`,
          true,
        );
    }
  }

  async #onExpress(
    toolCallId: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    const emotion =
      typeof params.emotion === "string" ? params.emotion.trim() : "";
    if (!emotion) {
      this.#reply(toolCallId, "express needs an 'emotion'", true);
      return;
    }
    try {
      await this.#call?.express(emotion);
    } catch (err) {
      // The bound lives where the message is built, so every plugin gets it.
      // Read it back rather than throwing at the model.
      this.#reply(toolCallId, String(err), true);
      return;
    }
    this.#reply(toolCallId, `expressing ${emotion}`);
  }

  /** Put a picture on the bot's tile, from inline bytes or a URL. */
  async #onShowImage(
    toolCallId: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    const call = this.#call;
    if (call === undefined) return;
    try {
      let dataBase64 =
        typeof params.dataBase64 === "string" ? params.dataBase64 : undefined;
      let mime = typeof params.mime === "string" ? params.mime : undefined;
      const url = typeof params.url === "string" ? params.url : undefined;

      if (dataBase64 === undefined && url) {
        // The URL came from the model, which is steered by whoever is on the
        // call. The guard is what stops a crafted prompt reaching cloud
        // metadata or anything else on your network.
        const fetched = await fetchPublicImage(
          url,
          MAX_IMAGE_BYTES,
          IMAGE_FETCH_TIMEOUT_MS,
        );
        dataBase64 = fetched.bytes.toString("base64");
        mime = fetched.mime;
      }
      if (
        !dataBase64 ||
        !mime ||
        !(DISPLAY_IMAGE_MIME_TYPES as readonly string[]).includes(mime)
      ) {
        throw new Error(
          "show_image needs {dataBase64, mime} or {url}, and the image must be one of " +
            DISPLAY_IMAGE_MIME_TYPES.join(", "),
        );
      }
      const duration = params.durationMs;
      await call.displayImage(dataBase64, {
        mime,
        durationMs: typeof duration === "number" ? duration : undefined,
        mode:
          typeof params.mode === "string"
            ? (params.mode.slice(0, MAX_MODE_CHARS) as "fullscreen" | "overlay")
            : undefined,
        caption:
          typeof params.caption === "string"
            ? params.caption.slice(0, MAX_CAPTION_CHARS)
            : undefined,
      });
      this.#reply(toolCallId, "the caller can see the image");
    } catch (err) {
      // Tell the agent the truth. Claiming success would leave it talking about
      // a picture the caller never saw.
      this.#reply(toolCallId, `show_image failed: ${String(err)}`, true);
    }
  }

  /**
   * Look at what the caller is showing.
   *
   * The frame is uploaded to the conversation, which PERSISTS the caller's
   * screen or face with ElevenLabs. That is why it takes the call being
   * recorded: the caller has been told the call is being kept, and this is part
   * of what is kept.
   */
  async #onLook(
    toolCallId: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    const call = this.#call;
    const agent = this.#agent;
    if (call === undefined || agent === undefined) return;

    const source = params.source;
    const frame = call.latestVideoFrame(
      source === "camera" || source === "screenshare"
        ? (source as VideoSource)
        : undefined,
    );
    if (frame === undefined) {
      this.#reply(
        toolCallId,
        "there is nothing to look at: the caller is not sharing their camera or screen",
        true,
      );
      return;
    }
    if (!this.#recording()) {
      this.#reply(
        toolCallId,
        "cannot look: the Microsoft Teams call is not being recorded, and looking would " +
          "store the caller's screen with a third party",
        true,
      );
      return;
    }
    await this.#attach(toolCallId, frame, params, "look");
  }

  /**
   * Look at something the caller has already moved past.
   *
   * Only possible on a recorded call, because that is the only time frames are
   * kept at all.
   */
  async #onLookBack(
    toolCallId: string,
    params: Record<string, unknown>,
  ): Promise<void> {
    if (this.#call === undefined || this.#agent === undefined) return;
    const frames = this.#keyframes.recent();
    if (frames.length === 0) {
      this.#reply(
        toolCallId,
        this.#recording()
          ? "nothing has been shown on this call yet"
          : "I can only look back at earlier screens while the call is being recorded, " +
              "and nothing has been kept",
        true,
      );
      return;
    }
    await this.#attach(
      toolCallId,
      frames[frames.length - 1]!,
      params,
      "look_back",
    );
  }

  /** Upload one frame into the conversation and tell the agent to read it. */
  async #attach(
    toolCallId: string,
    frame: VideoFrame,
    params: Record<string, unknown>,
    tool: string,
  ): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined) return;
    const who =
      frame.participantName ??
      (frame.source === "screenshare" ? "a participant" : "the caller");
    const seeing =
      frame.source === "screenshare"
        ? `screen shared by ${who}`
        : `camera of ${who}`;
    const question =
      typeof params.question === "string" && params.question.trim()
        ? params.question.trim()
        : "Describe what is visible.";
    try {
      await agent.attachImage(
        frame.data,
        frame.mime,
        `[live call frame: ${seeing}] ${question}`,
      );
      this.#reply(
        toolCallId,
        "the frame is attached; answer from what you can see",
      );
    } catch (err) {
      this.#reply(toolCallId, `${tool} failed: ${String(err)}`, true);
    }
  }

  #reply(toolCallId: string, result: string, isError = false): void {
    this.#agent?.sendToolResult(toolCallId, result, isError);
  }
}
