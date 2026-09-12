// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The relay: one Microsoft Teams call on one side, one Cartesia Line agent on
 * the other.
 *
 * The simplest of the provider plugins, because Cartesia's design puts the
 * agent's logic on their platform rather than in your worker. There are no
 * client tools to answer here: what the agent can do, it does in its own code,
 * and this side is transport.
 *
 * Context has one channel on this wire, `custom` metadata, which the Line
 * agent's code receives.
 *
 * The Python twin is `standin.plugins.cartesia.handler`.
 */

import type { CallHandler, CallSession } from "../../handler.js";
import { logger } from "../../log.js";
import { StartupBuffer } from "../../startup.js";
import {
  AgentSocket,
  buildStart,
  type AgentMessage,
  type AgentPort,
} from "./agent.js";
import { cartesiaConfigFromEnv, type CartesiaConfig } from "./config.js";

const MAX_PENDING_AUDIO = 200;

/**
 * The SDK renders a key press as a finished sentence, which is what a model
 * wants. Line has a real dtmf event, so the digit is read back out of the
 * sentence the SDK generated. Safe to do because that sentence is the SDK's own,
 * fixed and covered by a conformance vector, rather than anything a caller can
 * influence.
 */
const DTMF_SENTENCE = /pressed the "([^"]+)" key/;

/** How the handler reaches Cartesia. Replaceable so tests need no network. */
export type Connect = (
  config: CartesiaConfig,
  onMessage: (message: AgentMessage) => void | Promise<void>,
  onAudio: (payloadBase64: string) => void | Promise<void>,
  onClose: (code: number, reason: string) => void | Promise<void>,
) => Promise<AgentPort>;

/** Options for {@link CartesiaHandler}. */
export interface CartesiaHandlerOptions {
  config?: CartesiaConfig;
  connect?: Connect;
}

/** One Microsoft Teams call answered by one Cartesia Line agent. */
export class CartesiaHandler implements CallHandler {
  #config: CartesiaConfig;
  #connect: Connect;
  #call: CallSession | undefined;
  #agent: AgentPort | undefined;
  #closed = false;
  // Holds BOTH the caller's first words and the first context, so neither
  // is lost while the provider is still connecting.
  readonly #pending = new StartupBuffer();
  #pendingAudio: string[] = [];

  constructor(options: CartesiaHandlerOptions = {}) {
    this.#config = options.config ?? cartesiaConfigFromEnv();
    this.#connect = options.connect ?? AgentSocket.connect.bind(AgentSocket);
  }

  async onStart(session: CallSession): Promise<void> {
    this.#call = session;
    const caller = session.start.caller;

    let agent: AgentPort;
    try {
      agent = await this.#connect(
        this.#config,
        (message) => this.#onAgentMessage(message),
        (payload) => this.#onAgentAudio(payload),
        (code, reason) => this.#onAgentClose(code, reason),
      );
    } catch (err) {
      logger.error(
        `standin: could not open the Cartesia stream: ${String(err)}`,
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

    agent.sendStart(
      buildStart(
        agent.streamId,
        this.#config,
        {
          callerName: caller.displayName ?? "the caller",
          tenantId: caller.tenantId ?? "unknown-tenant",
          direction: session.start.direction,
        },
        session.callId,
      ),
    );
    for (const chunk of this.#pendingAudio.splice(0))
      agent.sendAudioChunk(chunk);
  }

  async onCallerAudio(pcm: Buffer): Promise<void> {
    const chunk = pcm.toString("base64");
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      this.#pending.audio(pcm);
      return;
    }
    agent.sendAudioChunk(chunk);
  }

  /** Everything the call knows, handed to the agent's own code. */
  async onContext(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent === undefined || !agent.isOpen) {
      // Held rather than dropped: the "there are N people here, stay quiet"
      // line and the recording change both land in this gap.
      this.#pending.context(text);
      return;
    }
    const digit = DTMF_SENTENCE.exec(text);
    if (digit?.[1]) {
      agent.sendDtmf(digit[1]);
      return;
    }
    agent.sendCustom({ from: "msteams", context: text });
  }

  /**
   * There is no inject-speech channel on this wire, so the line is handed to the
   * agent's code as context and it decides how to say it.
   */
  async onGoodbye(text: string): Promise<void> {
    const agent = this.#agent;
    if (agent !== undefined && agent.isOpen) {
      agent.sendCustom({ from: "msteams", goodbye: text });
    }
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

  // ---- what Cartesia sends us ---------------------------------------------

  async #onAgentAudio(payloadBase64: string): Promise<void> {
    const pcm = Buffer.from(payloadBase64, "base64");
    // Buffer.from never throws on bad input, it silently discards what it
    // cannot read, so re-encoding is the only way to know this really is the
    // payload that was sent.
    if (pcm.length === 0 || pcm.toString("base64") !== payloadBase64) {
      logger.warn("standin: Cartesia sent unusable audio; dropping the frame");
      return;
    }
    await this.#call?.sendAudio(pcm);
  }

  async #onAgentMessage(message: AgentMessage): Promise<void> {
    switch (message.event) {
      case "clear":
        // The agent is telling us the caller barged in. Flushing what StandIn
        // has buffered is what actually stops the bot mid-word.
        await this.#call?.cancelPlayback();
        break;
      case "end":
        if (!this.#closed) await this.#call?.end("agent-ended-call");
        break;
      case "transfer_call":
        // A phone-network transfer has no meaning on a Microsoft Teams call.
        logger.info(
          "standin: Cartesia asked for a call transfer, which this lane cannot do",
        );
        break;
      default:
        break;
    }
  }

  async #onAgentClose(code: number, reason: string): Promise<void> {
    logger.info(`standin: the Cartesia stream closed (${code} ${reason})`);
    if (!this.#closed) await this.#call?.end("agent-disconnected");
  }
}
