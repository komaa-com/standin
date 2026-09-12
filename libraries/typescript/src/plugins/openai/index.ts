// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Answer Microsoft Teams calls with an OpenAI Realtime model.
 *
 * Speech to speech: the model hears the caller's voice rather than a transcript
 * of it, so there is no recognition step adding latency in the middle.
 *
 * Nothing to install beyond the SDK: the Realtime API is reached over an
 * ordinary WebSocket, so this plugin adds no dependency.
 *
 * ```bash
 * npm install @komaa/standin-sdk
 *
 * export STANDIN_SECRET=...      # your StandIn connection secret
 * export OPENAI_API_KEY=...
 * npx standin-openai
 * ```
 *
 * The Realtime API speaks PCM at 24 kHz and a Microsoft Teams call speaks 16
 * kHz. This plugin owns that conversion in both directions, using the same
 * resampler the Python SDK uses, driven by the same shared conformance vectors.
 *
 * The model gets four call capabilities without any configuration: `end_call`,
 * `express`, `show_image` and `look`. Looking needs a vision model, because the
 * Realtime model hears but does not see - set `STANDIN_VISION_API_URL` and
 * `STANDIN_VISION_MODEL` to any OpenAI-compatible endpoint that takes images.
 *
 * Add tools of your own, or point the session at a remote MCP server that
 * OpenAI dials itself:
 *
 * ```ts
 * import { CallServer } from "@komaa/standin-sdk";
 * import { OpenAIHandler, mcpTool } from "@komaa/standin-sdk/openai";
 *
 * const server = new CallServer({
 *   handlerFactory: () => new OpenAIHandler({
 *     tools: [{
 *       name: "open_ticket",
 *       description: "Open a support ticket for the caller.",
 *       parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] },
 *       handler: async (params) => `opened ticket for ${String(params.summary)}`,
 *     }],
 *     mcpTools: [mcpTool({ server_label: "docs", server_url: "https://mcp.example.com" })],
 *   }),
 * });
 * await server.start();
 * ```
 */

import { CallServer } from "../../index.js";
import { OpenAIHandler } from "./handler.js";
import { openAIConfigFromEnv } from "./config.js";

export {
  AgentSocket,
  BUILT_IN_TOOLS,
  buildInstructions,
  buildSessionUpdate,
  mcpTool,
  type AgentMessage,
  type AgentPort,
  type CallerContext,
} from "./agent.js";
export {
  DEFAULT_INSTRUCTIONS,
  openAIConfigFromEnv,
  type OpenAIConfig,
} from "./config.js";
export {
  OpenAIHandler,
  type Connect,
  type CustomTool,
  type OpenAIHandlerOptions,
  type ToolContext,
} from "./handler.js";

/** Answer Microsoft Teams calls with OpenAI until the process is interrupted. */
export async function serve(): Promise<void> {
  const config = openAIConfigFromEnv();
  const server = new CallServer({
    handlerFactory: () => new OpenAIHandler({ config }),
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
