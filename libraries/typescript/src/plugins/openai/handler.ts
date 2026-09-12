// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The relay: one Microsoft Teams call on one side, one OpenAI Realtime session
 * on the other.
 *
 * Speech to speech, so there is no transcription step in the middle and the
 * model hears the caller's voice rather than a transcript of it. The one thing
 * this plugin must get right that the others do not is the rate: the
 * Realtime API speaks 24 kHz and the call speaks 16 kHz, and `agent.ts` owns
 * that conversion so nothing here has to think about it.
 *
 * The call capabilities come from `standin/callTools`, so a capability added to
 * the SDK reaches this plugin without an edit. Add your own with
 * {@link CustomTool} and they are declared the same way.
 */

import type { CallHandler, CallSession } from "../../handler.js";
import { ExpressionCue } from "../../avatar.js";
import { CallTools } from "../../callTools.js";
import { fetchPublicImage } from "../../fetch.js";
import { TurnLipSync } from "../../lipsync.js";
import { logger } from "../../log.js";
import { StartupBuffer } from "../../startup.js";
import { FrameDescriber, type VideoFrame } from "../../vision.js";
import { VisionTools } from "../../visionTools.js";
import {
  AgentSocket,
  BUILT_IN_TOOL_NAMES,
  buildInstructions,
  buildSessionUpdate,
  type AgentMessage,
  type AgentPort,
  type CallerContext,
} from "./agent.js";
import { openAIConfigFromEnv, type OpenAIConfig } from "./config.js";

const MAX_EMOTION_CHARS = 40;
const MAX_CAPTION_CHARS = 200;
const IMAGE_FETCH_TIMEOUT_MS = 10_000;
const MAX_PENDING_AUDIO = 200;
const MAX_PENDING_CONTEXT = 20;

/** What a custom tool is told about the call it is running inside. */
export interface ToolContext {
  call: CallSession;
  /** Whether the call is being recorded. Gate anything that stores what the caller showed. */
  recording: boolean;
}

/** A function of your own, which the model calls and your code answers. */
export interface CustomTool {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
  handler: (
    params: Record<string, unknown>,
    ctx: ToolContext,
  ) => string | Promise<string>;
}

/** How the handler reaches OpenAI. Replaceable so tests need no network. */
export type Connect = (
  config: OpenAIConfig,
  onMessage: (message: AgentMessage) => void | Promise<void>,
  onAudio: (pcm: Buffer) => void | Promise<void>,
  onClose: (code: number, reason: string) => void | Promise<void>,
) => Promise<AgentPort>;

/** Options for {@link OpenAIHandler}. */
export interface OpenAIHandlerOptions {
  config?: OpenAIConfig;
  tools?: CustomTool[];
  /** Remote MCP entries, normalised with `mcpTool`. Run server-side by OpenAI. */
  mcpTools?: Array<Record<string, unknown>>;
  describer?: FrameDescriber;
  connect?: Connect;
}

/** One Microsoft Teams call answered by one OpenAI Realtime session. */
export class OpenAIHandler implements CallHandler {
  #config: OpenAIConfig;
  #tools: Map<string, CustomTool>;
  #mcpTools: Array<Record<string, unknown>>;
  #describer: FrameDescriber | undefined;
  #connect: Connect;
  #call: CallSession | undefined;
  #vision: VisionTools | undefined;
  #callTools: CallTools | undefined;
  #agent: AgentPort | undefined;
  #closed = false;
  /** The mouth and the face. Both cosmetic, both cheap, both per call. */
  readonly #lip = new TurnLipSync();
  readonly #cue = new ExpressionCue();
  #replyText = "";
  // Holds BOTH the caller's first words and the first context, so neither
  // is lost while the provider is still connecting.
  readonly #pending = new StartupBuffer();

  constructor(options: OpenAIHandlerOptions = {}) {
    this.#config = options.config ?? openAIConfigFromEnv();
    this.#tools = new Map(
      (options.tools ?? []).map((tool) => [tool.name, tool]),
    );
    this.#mcpTools = options.mcpTools ?? [];
    this.#describer = options.describer ?? FrameDescriber.fromEnv();
    this.#connect = options.connect ?? AgentSocket.connect.bind(AgentSocket);

    const collisions = [...this.#tools.keys()].filter((name) =>
      BUILT_IN_TOOL_NAMES.has(name),
    );
    if (collisions.length > 0) {
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
    const context: CallerContext = {
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
        `standin: could not open the OpenAI Realtime session: ${String(err)}`,
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

    const extraTools = [
      ...[...this.#tools.values()].map((tool) => ({
        type: "function",
        name: tool.name,
        description: tool.description,
        parameters: tool.parameters ?? {
          type: "object",
          properties: {},
          required: [],
        },
      })),
      ...this.#mcpTools,
    ];
    agent.sendSessionUpdate(
      buildSessionUpdate(
        this.#config,
        buildInstructions(this.#config.instructions, context),
        extraTools,
      ),
    );
    await this.#pending.release(
      (pcm: Buffer) => agent.sendAudio(pcm),
      (text: string) => agent.sendContext(text),
    );
    if (this.#pending.dropped.audio || this.#pending.dropped.context) {
      logger.info(
        "standin: the caller outran the agent starting up; some early input was dropped",
      );
    }
  }

  async onCallerAudio(pcm: Buffer): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      this.#pending.audio(pcm);
      return;
    }
    agent.sendAudio(pcm);
  }

  /**
   * Context arrives as a user item with no response requested, so the model
   * reads it on its next turn instead of interrupting to acknowledge it.
   */
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
      this.#pending.context(text);
      return;
    }
    agent.sendContext(text);
  }

  /** Cancel whatever is being said, then ask for the goodbye. */
  async onGoodbye(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) return;
    agent.cancelResponse();
    agent.sendUserTurn(
      `[system: the call is ending. Say a brief goodbye now: "${text}"]`,
    );
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

  // ---- what OpenAI sends us -----------------------------------------------

  /** Agent audio, already resampled to the wire rate by the socket. */
  async #onAgentAudio(pcm: Buffer): Promise<void> {
    if (pcm.length === 0) return;
    await this.#call?.sendAudio(pcm);
    // The only duration this worker genuinely knows. A realtime model hands
    // back no timings, and a guess from text length drifts further out of step
    // with the voice the longer the turn runs.
    this.#lip.audioSent(pcm);
  }

  /**
   * One viseme timeline per turn, spread over the audio that turn actually sent.
   *
   * Cosmetic, so everything here is swallowed: the worst acceptable outcome is
   * a still mouth over correct audio, and the unacceptable one is a dropped
   * turn because a lip shape threw.
   */
  async #sendVisemes(): Promise<void> {
    const text = this.#replyText;
    this.#replyText = "";
    try {
      const marks = this.#lip.finish(text);
      if (marks.length > 0) await this.#call?.sendSpeechMarks(marks);
    } catch (err) {
      logger.debug(`standin: the viseme timeline was skipped: ${String(err)}`);
    }
  }

  /**
   * A piece of what the model is saying, as it is being said.
   *
   * Held for the viseme timeline, and read for the face. Re-read on every piece
   * rather than at the end, because waiting for the final transcript leaves the
   * face wrong for the whole time the reply is being spoken.
   */
  async #onReplyText(text: string): Promise<void> {
    if (text === "") return;
    this.#replyText += text;
    try {
      const emotion = this.#cue.cue(this.#replyText);
      if (emotion !== null) await this.#call?.express(emotion);
    } catch (err) {
      logger.debug(`standin: the expression cue was skipped: ${String(err)}`);
    }
  }

  async #onAgentMessage(message: AgentMessage): Promise<void> {
    switch (message.type) {
      case "input_audio_buffer.speech_started":
        // The model has already cancelled its own response server-side. This is
        // the other half: flushing what StandIn has buffered, which is what
        // actually stops the bot mid-word.
        await this.#call?.cancelPlayback();
        // The service drops audio the caller never heard. Keeping the count
        // would spread the NEXT turn's words over its own audio plus the audio
        // that was thrown away, and the mouth would run long for the rest of it.
        this.#lip.cancel();
        this.#replyText = "";
        break;
      case "response.output_audio_transcript.delta":
        await this.#onReplyText(
          typeof message.delta === "string" ? message.delta : "",
        );
        break;
      case "response.done":
        await this.#sendVisemes();
        break;
      case "response.function_call_arguments.done":
        void this.#runTool(message);
        break;
      case "conversation.item.input_audio_transcription.completed":
      case "response.output_audio_transcript.done":
        if (this.#config.logTranscripts && this.#recording()) {
          logger.info(`standin: openai ${message.type}`);
        }
        break;
      case "error":
        logger.warn(
          `standin: OpenAI error: ${JSON.stringify(message.error ?? message)}`,
        );
        break;
      default:
        break;
    }
  }

  async #onAgentClose(code: number, reason: string): Promise<void> {
    logger.info(
      `standin: the OpenAI Realtime session closed (${code} ${reason})`,
    );
    if (!this.#closed) await this.#call?.end("agent-disconnected");
  }

  // ---- the model's tools --------------------------------------------------

  async #runTool(message: AgentMessage): Promise<void> {
    const name = message.name;
    const callId = message.call_id;
    if (typeof name !== "string" || typeof callId !== "string") return;

    let params: Record<string, unknown> = {};
    if (typeof message.arguments === "string") {
      try {
        const parsed: unknown = JSON.parse(message.arguments || "{}");
        if (typeof parsed === "object" && parsed !== null) {
          params = parsed as Record<string, unknown>;
        }
      } catch {
        params = {};
      }
    }

    let output: string;
    try {
      output = await this.dispatch(name, params);
    } catch (err) {
      // Tell the model the truth: a tool that silently "succeeded" leaves it
      // talking about something that never happened.
      output = `${name} failed: ${String(err)}`;
    }
    this.#agent?.sendToolResult(callId, output);
  }

  /** Run one tool and return what the model is told. Exposed for tests. */
  async dispatch(
    name: string,
    params: Record<string, unknown>,
  ): Promise<string> {
    const call = this.#call;
    if (call === undefined) return "the call is no longer active";

    const callTools = this.#callTools;
    if (callTools !== undefined && BUILT_IN_TOOL_NAMES.has(name)) {
      // The SDK owns these, and its dispatch never throws: what comes back is
      // the sentence the model should say.
      return callTools.dispatch(name, params);
    }

    const tool = this.#tools.get(name);
    if (tool === undefined)
      return `"${name}" is not a tool this plugin answers`;
    return String(
      await tool.handler(params, { call, recording: this.#recording() }),
    );
  }
}
