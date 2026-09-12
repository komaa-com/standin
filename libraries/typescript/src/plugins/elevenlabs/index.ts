// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Answer Microsoft Teams calls with an ElevenLabs agent.
 *
 * StandIn answers the Microsoft Teams call and dials this worker. This
 * plugin answers that dial, opens one ElevenLabs agent conversation per
 * call, and relays the audio both ways.
 *
 * Nothing to install beyond the SDK: ElevenLabs is reached over an ordinary
 * WebSocket, so this plugin adds no dependency.
 *
 * ```bash
 * npm install @komaa/standin-sdk
 *
 * export STANDIN_SECRET=...          # your StandIn connection secret
 * export ELEVENLABS_API_KEY=...
 * export ELEVENLABS_AGENT_ID=...
 * npx standin-elevenlabs
 * ```
 *
 * Configure the agent for `pcm_16000` audio in BOTH directions. That is exactly
 * what StandIn speaks, so nothing resamples anything and the latency you measure
 * is the model's, not the transport's. An agent set to anything else is refused
 * at the first frame rather than producing a whole call of garbled audio.
 *
 * Give the agent the SDK's client tools and it can use the rest of the call, not
 * just the voice channel: hanging up, the avatar's expression, putting a
 * picture on the bot's tile, and looking at what the caller is sharing.
 * ElevenLabs declares tools on the agent rather than over the wire, so
 * {@link clientTools} returns the exact declarations to paste in.
 *
 * Drive it yourself inside your own worker instead:
 *
 * ```ts
 * import { CallServer } from "@komaa/standin-sdk";
 * import { ElevenLabsHandler } from "@komaa/standin-sdk/elevenlabs";
 *
 * const server = new CallServer({ handlerFactory: () => new ElevenLabsHandler() });
 * await server.start();
 * ```
 *
 * The Python twin is `standin.plugins.elevenlabs`.
 */

import { CallServer } from "../../index.js";
import { ElevenLabsHandler } from "./handler.js";
import { elevenLabsConfigFromEnv } from "./config.js";

export {
  AgentSocket,
  buildConversationInit,
  type AgentMessage,
  type AgentPort,
} from "./agent.js";
export { elevenLabsConfigFromEnv, type ElevenLabsConfig } from "./config.js";
export { ElevenLabsHandler, clientTools, type Connect } from "./handler.js";

/**
 * Answer Microsoft Teams calls with ElevenLabs until the process is interrupted.
 *
 * The configuration is read ONCE here rather than per call: a missing API key
 * should stop the worker at startup, not surprise the first caller.
 */
export async function serve(): Promise<void> {
  const config = elevenLabsConfigFromEnv();
  const server = new CallServer({
    handlerFactory: () => new ElevenLabsHandler(config),
  });
  await server.start();

  await new Promise<void>((resolve) => {
    const stop = (): void => {
      resolve();
    };
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
  });

  await server.aclose();
}
