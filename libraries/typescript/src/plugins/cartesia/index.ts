// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Answer Microsoft Teams calls with a Cartesia Line agent.
 *
 * StandIn answers the Microsoft Teams call and dials this worker. This
 * plugin answers that dial, opens one Line agent stream per call, and
 * relays the audio both ways.
 *
 * Nothing to install beyond the SDK: Cartesia is reached over an ordinary
 * WebSocket, so this plugin adds no dependency.
 *
 * ```bash
 * npm install @komaa/standin-sdk
 *
 * export STANDIN_SECRET=...      # your StandIn connection secret
 * export CARTESIA_API_KEY=...
 * export CARTESIA_AGENT_ID=...
 * npx standin-cartesia
 * ```
 *
 * The agent itself is your code on Cartesia's platform, so this plugin is
 * transport and nothing else: there are no call capabilities to declare and no
 * tools to answer here. Caller details reach your agent as stream metadata, and
 * call context arrives as `custom` events for your agent code to act on.
 *
 * Audio is pinned to `pcm_16000` in both directions, which is exactly what
 * StandIn speaks, so nothing resamples anything.
 *
 * The Python twin is `standin.plugins.cartesia`.
 */

import { CallServer } from "../../index.js";
import { CartesiaHandler } from "./handler.js";
import { cartesiaConfigFromEnv } from "./config.js";

export {
  AgentSocket,
  WIRE_SAMPLE_RATE_HZ,
  buildStart,
  mintAccessToken,
  type AgentMessage,
  type AgentPort,
  type CallerContext,
} from "./agent.js";
export { cartesiaConfigFromEnv, type CartesiaConfig } from "./config.js";
export {
  CartesiaHandler,
  type CartesiaHandlerOptions,
  type Connect,
} from "./handler.js";

/** Answer Microsoft Teams calls with Cartesia until the process is interrupted. */
export async function serve(): Promise<void> {
  const config = cartesiaConfigFromEnv();
  const server = new CallServer({
    handlerFactory: () => new CartesiaHandler({ config }),
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
