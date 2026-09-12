// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Answer Microsoft Teams calls with a Deepgram Voice Agent.
 *
 * StandIn answers the Microsoft Teams call and dials this worker. This
 * plugin answers that dial, opens one Voice Agent session per call, and
 * relays the audio both ways.
 *
 * Nothing to install beyond the SDK: Deepgram is reached over an ordinary
 * WebSocket, so this plugin adds no dependency.
 *
 * ```bash
 * npm install @komaa/standin-sdk
 *
 * export STANDIN_SECRET=...         # your StandIn connection secret
 * export DEEPGRAM_API_KEY=...
 * npx standin-deepgram
 * ```
 *
 * Speech to text, reasoning and speech are all configured on the session, so
 * there is nothing to set up on the Deepgram side. Audio is pinned to linear16
 * at 16 kHz in both directions, which is exactly what StandIn speaks.
 *
 * The agent gets four call capabilities without any configuration: `end_call`,
 * `express`, `show_image` and `look`. Looking needs a vision model, because a
 * Voice Agent hears but does not see - set `STANDIN_VISION_API_URL` and
 * `STANDIN_VISION_MODEL` to any OpenAI-compatible endpoint that takes images.
 *
 * Add tools of your own, executed in your worker:
 *
 * ```ts
 * import { CallServer } from "@komaa/standin-sdk";
 * import { DeepgramHandler } from "@komaa/standin-sdk/deepgram";
 *
 * const tools = [{
 *   name: "open_ticket",
 *   description: "Open a support ticket for the caller.",
 *   parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] },
 *   handler: async (params) => `opened ticket for ${String(params.summary)}`,
 * }];
 *
 * const server = new CallServer({ handlerFactory: () => new DeepgramHandler({ tools }) });
 * await server.start();
 * ```
 *
 * The Python twin is `standin.plugins.deepgram`.
 */

import { CallServer } from "../../index.js";
import { DeepgramHandler } from "./handler.js";
import { deepgramConfigFromEnv } from "./config.js";

export {
  AgentSocket,
  WIRE_SAMPLE_RATE_HZ,
  buildPrompt,
  buildSettings,
  type AgentMessage,
  type AgentPort,
  type CallerContext,
} from "./agent.js";
export {
  DEFAULT_INSTRUCTIONS,
  deepgramConfigFromEnv,
  type DeepgramConfig,
} from "./config.js";
export {
  BUILT_IN_TOOLS,
  DeepgramHandler,
  type Connect,
  type CustomTool,
  type DeepgramHandlerOptions,
  type ToolContext,
} from "./handler.js";

/**
 * Answer Microsoft Teams calls with Deepgram until the process is interrupted.
 *
 * The configuration is read ONCE here: a missing API key should stop the worker
 * at startup, not surprise the first caller.
 */
export async function serve(): Promise<void> {
  const config = deepgramConfigFromEnv();
  const server = new CallServer({
    handlerFactory: () => new DeepgramHandler({ config }),
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
