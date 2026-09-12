// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The relay: one Microsoft Teams call on one side, one Deepgram Voice Agent on
 * the other.
 *
 * Two shapes differ from the other providers and are worth knowing:
 *
 * **Context rides the prompt.** The Voice Agent API has no non-interrupting
 * context message, so participant counts and key presses are appended to a
 * bounded rolling section of the prompt and pushed with `UpdatePrompt`. Bounded
 * matters: the prompt is resent in full each time.
 *
 * **Functions are declared, not configured.** The call capabilities come from
 * `standin/callTools`, are sent in the Settings message, and so there is
 * nothing to set up on the Deepgram side and nothing restated here: a
 * capability added to the SDK reaches this plugin without an edit. Add your own
 * with {@link CustomTool} and they are declared the same way.
 *
 * The Python twin is `standin.plugins.deepgram.handler`.
 */

import type { CallHandler, CallSession } from "../../handler.js";
import { CallTools, toolSchemas } from "../../callTools.js";
import { fetchPublicImage } from "../../fetch.js";
import { logger } from "../../log.js";
import { StartupBuffer } from "../../startup.js";
import { FrameDescriber, type VideoFrame } from "../../vision.js";
import { VisionTools } from "../../visionTools.js";
import {
  AgentSocket,
  buildPrompt,
  buildSettings,
  type AgentMessage,
  type AgentPort,
  type CallerContext,
} from "./agent.js";
import { deepgramConfigFromEnv, type DeepgramConfig } from "./config.js";

const MAX_EMOTION_CHARS = 40;
const MAX_CAPTION_CHARS = 200;
const IMAGE_FETCH_TIMEOUT_MS = 10_000;

/**
 * How many context notes ride in the prompt. The whole prompt is resent on every
 * update, so this is a cost per participant change, not a one-off.
 */
const MAX_CONTEXT_NOTES = 8;

const MAX_PENDING_AUDIO = 200;

/** What a custom tool is told about the call it is running inside. */
export interface ToolContext {
  /** The live call, so a tool can speak, show something, or hang up. */
  call: CallSession;
  /**
   * Whether the Microsoft Teams call is being recorded. Gate anything that
   * stores what the caller said or showed on this.
   */
  recording: boolean;
}

/** A function of your own, which the agent calls and your code answers. */
export interface CustomTool {
  /** Must not collide with the four built in above. */
  name: string;
  /**
   * What the model reads to decide whether to call it. This is the prompt for
   * the tool, so write it for a model, not for a developer.
   */
  description: string;
  /** JSON schema for the parameters. */
  parameters?: Record<string, unknown>;
  /**
   * Returns the string the agent is told, which it will read out or reason
   * from. Keep it fast: the caller is waiting in silence while it runs.
   */
  handler: (
    params: Record<string, unknown>,
    ctx: ToolContext,
  ) => string | Promise<string>;
}

/**
 * The call capabilities every Deepgram session gets, declared in Settings.
 * These have no endpoint, which is what tells Deepgram to ask the client to run
 * them. The list is the SDK's, in Deepgram's shape, so every provider offers
 * the same agent and a new capability needs no edit here.
 */
export const BUILT_IN_TOOLS: Array<Record<string, unknown>> =
  toolSchemas("flat");

const BUILT_IN_NAMES = new Set(
  BUILT_IN_TOOLS.map((tool) => tool.name as string),
);

/** How the handler reaches Deepgram. Replaceable so tests need no network. */
export type Connect = (
  config: DeepgramConfig,
  onMessage: (message: AgentMessage) => void | Promise<void>,
  onAudio: (pcm: Buffer) => void | Promise<void>,
  onClose: (code: number, reason: string) => void | Promise<void>,
) => Promise<AgentPort>;

/** Options for {@link DeepgramHandler}. */
export interface DeepgramHandlerOptions {
  config?: DeepgramConfig;
  tools?: CustomTool[];
  describer?: FrameDescriber;
  connect?: Connect;
}

/** One Microsoft Teams call answered by one Deepgram Voice Agent. */
export class DeepgramHandler implements CallHandler {
  #config: DeepgramConfig;
  #tools: Map<string, CustomTool>;
  #describer: FrameDescriber | undefined;
  #connect: Connect;
  #call: CallSession | undefined;
  #vision: VisionTools | undefined;
  #callTools: CallTools | undefined;
  #agent: AgentPort | undefined;
  #closed = false;
  // Holds BOTH the caller's first words and the first context, so neither
  // is lost while the provider is still connecting.
  readonly #pending = new StartupBuffer();
  #caller: CallerContext = {
    callerName: "the caller",
    tenantId: "unknown-tenant",
    direction: "inbound",
  };
  #notes: string[] = [];

  constructor(options: DeepgramHandlerOptions = {}) {
    this.#config = options.config ?? deepgramConfigFromEnv();
    this.#tools = new Map(
      (options.tools ?? []).map((tool) => [tool.name, tool]),
    );
    this.#describer = options.describer ?? FrameDescriber.fromEnv();
    this.#connect = options.connect ?? AgentSocket.connect.bind(AgentSocket);

    const collisions = [...this.#tools.keys()].filter((name) =>
      BUILT_IN_NAMES.has(name),
    );
    if (collisions.length > 0) {
      // Caught here rather than at the first call: a shadowed built-in is a
      // call capability that silently stops working.
      throw new Error(
        `custom tools may not shadow built-in ones: ${collisions.sort().join(", ")}`,
      );
    }
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
    this.#vision = new VisionTools(session, { describer: this.#describer });
    this.#callTools = new CallTools(session, { vision: this.#vision });
    const caller = session.start.caller;
    this.#caller = {
      callerName: caller.displayName ?? "the caller",
      tenantId: caller.tenantId ?? "unknown-tenant",
      direction: session.start.direction,
    };

    let agent: AgentPort;
    try {
      agent = await this.#connect(
        this.#config,
        (message) => this.#onAgentMessage(message),
        (pcm) => this.#onAgentAudio(pcm),
        (code, reason) => this.#onAgentClose(code, reason),
      );
    } catch (err) {
      logger.error(
        `standin: could not open the Deepgram agent: ${String(err)}`,
      );
      await session.end("agent-unavailable");
      return;
    }

    // The call can end during the connect above.
    if (this.#closed) {
      await agent.aclose();
      return;
    }
    this.#agent = agent;

    const functions = [
      ...BUILT_IN_TOOLS,
      ...[...this.#tools.values()].map((tool) => ({
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters ?? {
          type: "object",
          properties: {},
          required: [],
        },
      })),
    ];
    agent.sendSettings(
      buildSettings(
        this.#config,
        buildPrompt(this.#config, this.#caller, this.#notes),
        functions,
      ),
    );
    await this.#pending.release(
      (pcm: Buffer) => agent.sendAudio(pcm),
      (text: string) => this.#noteContext(agent, text),
    );
    if (this.#pending.dropped.audio || this.#pending.dropped.context) {
      logger.info(
        "standin: the caller outran the agent starting up; some early input was dropped",
      );
    }
  }

  /** The caller's voice, straight through as a binary frame. */
  async onCallerAudio(pcm: Buffer): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      this.#pending.audio(pcm);
      return;
    }
    agent.sendAudio(pcm);
  }

  /** Fold context into the prompt, because this API has nowhere else to put it. */
  /**
   * Keep a short history, so a question about a slide already gone can still be
   * answered. Only kept while the call is recorded.
   */
  async onVideoFrame(frame: VideoFrame): Promise<void> {
    this.#vision?.keyframes.offer(frame, this.#call?.recordingActive ?? false);
  }

  async onContext(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      // Held rather than dropped: the "there are N people here, stay quiet"
      // line and the recording change both land in this gap.
      this.#pending.context(text);
      return;
    }
    this.#noteContext(agent, text);
  }

  /** Fold one held context line into the prompt, once the agent exists. */
  #noteContext(agent: AgentPort, text: string): void {
    this.#notes.push(text);
    if (this.#notes.length > MAX_CONTEXT_NOTES) this.#notes.shift();
    agent.updatePrompt(buildPrompt(this.#config, this.#caller, this.#notes));
  }

  /** Say this line now, interrupting whatever the agent was saying. */
  async onGoodbye(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent !== undefined && agent.isOpen) agent.injectAgentMessage(text);
  }

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

  /**
   * The shared vision tools for this call.
   *
   * They carry the budget, the keyframes and the graceful refusals, so this
   * plugin no longer keeps its own copy of any of them.
   */
  #visionTools(): VisionTools {
    if (this.#vision === undefined)
      throw new Error("the call has not started yet");
    return this.#vision;
  }

  // ---- what Deepgram sends us ---------------------------------------------

  /** Agent audio: already PCM16 at 16 kHz, so it goes straight out. */
  async #onAgentAudio(pcm: Buffer): Promise<void> {
    if (pcm.length > 0) await this.#call?.sendAudio(pcm);
  }

  async #onAgentMessage(message: AgentMessage): Promise<void> {
    switch (message.type) {
      case "UserStartedSpeaking":
        // Deepgram has decided the caller is talking. Flushing what StandIn has
        // buffered is what actually stops the bot mid-word.
        await this.#call?.cancelPlayback();
        break;
      case "FunctionCallRequest": {
        const functions = message.functions;
        if (Array.isArray(functions)) {
          for (const fn of functions) {
            if (typeof fn === "object" && fn !== null) {
              void this.#runFunction(fn as Record<string, unknown>);
            }
          }
        }
        break;
      }
      case "ConversationText":
        if (this.#config.logTranscripts && this.#recording()) {
          logger.info(`standin: deepgram ${String(message.role ?? "turn")}`);
        }
        break;
      case "Error":
      case "Warning":
        logger.warn(
          `standin: Deepgram ${message.type.toLowerCase()}: ` +
            String(message.description ?? message.code ?? "no detail"),
        );
        break;
      default:
        break;
    }
  }

  async #onAgentClose(code: number, reason: string): Promise<void> {
    logger.info(`standin: the Deepgram agent closed (${code} ${reason})`);
    if (!this.#closed) await this.#call?.end("agent-disconnected");
  }

  // ---- the agent's functions ----------------------------------------------

  async #runFunction(fn: Record<string, unknown>): Promise<void> {
    const name = fn.name;
    const callId = fn.id;
    if (typeof name !== "string" || typeof callId !== "string") return;

    let params: Record<string, unknown> = {};
    if (typeof fn.arguments === "string") {
      try {
        const parsed: unknown = JSON.parse(fn.arguments || "{}");
        if (typeof parsed === "object" && parsed !== null)
          params = parsed as Record<string, unknown>;
      } catch {
        params = {};
      }
    } else if (typeof fn.arguments === "object" && fn.arguments !== null) {
      params = fn.arguments as Record<string, unknown>;
    }

    let result: string;
    try {
      result = await this.dispatch(name, params);
    } catch (err) {
      // Tell the agent the truth. A tool that silently "succeeded" leaves it
      // talking about something that never happened.
      result = `${name} failed: ${String(err)}`;
    }
    this.#agent?.sendFunctionResult(callId, name, result);
  }

  /** Run one function and return what the agent is told. Exposed for tests. */
  async dispatch(
    name: string,
    params: Record<string, unknown>,
  ): Promise<string> {
    const call = this.#call;
    if (call === undefined) return "the call is no longer active";

    const callTools = this.#callTools;
    if (callTools !== undefined && BUILT_IN_NAMES.has(name)) {
      // The SDK owns these, and its dispatch never throws: what comes back is
      // the sentence the agent should say.
      return callTools.dispatch(name, params);
    }

    const tool = this.#tools.get(name);
    if (tool === undefined)
      return `"${name}" is not a tool this plugin answers`;
    return String(
      await tool.handler(params, { call, recording: this.#recording() }),
    );
  }

  /**
   * Describe what the caller is showing.
   *
   * Deepgram's agent hears but does not see, so the frame goes to a vision model
   * of your choosing and only the description comes back.
   */
}
