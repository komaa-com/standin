// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Answer Microsoft Teams calls with a LiveKit agent.
 *
 * StandIn answers the Microsoft Teams call and dials this worker. This
 * plugin answers that dial, creates one LiveKit room per call, dispatches
 * your agent into it, and relays the audio both ways.
 *
 * Your agent needs no Microsoft Teams awareness: it sees a participant who
 * talks. Who is calling arrives as job metadata, and call context arrives on two
 * room data topics, `msteams.context` and `msteams.goodbye`, which are the same
 * strings the Python SDK uses.
 *
 * This is the one plugin with dependencies of its own, because joining a
 * room means running LiveKit's client:
 *
 * ```bash
 * npm install @komaa/standin-sdk @livekit/rtc-node livekit-server-sdk
 *
 * export STANDIN_SECRET=...       # your StandIn connection secret
 * export LIVEKIT_URL=wss://...
 * export LIVEKIT_API_KEY=...
 * export LIVEKIT_API_SECRET=...
 * export LIVEKIT_AGENT_NAME=standin-msteams
 * ```
 *
 * ```ts
 * import { CallServer } from "@komaa/standin-sdk";
 * import { LiveKitHandler } from "@komaa/standin-sdk/livekit";
 *
 * const server = new CallServer({ handlerFactory: () => new LiveKitHandler() });
 * await server.start();
 * ```
 *
 * This plugin relays AUDIO. It does not put the agent's own avatar video on
 * the bot's tile: StandIn renders the avatar, and a LiveKit-side video relay is
 * not implemented here. `CallSession.express` and `sendSpeechMarks` still drive
 * the avatar, and the caller hears the agent either way.
 *
 * Those two packages are optional peers: they are imported only when this
 * module is reached, so the core and every other plugin install without
 * them. Writing the agent in Python instead? `standin.plugins.livekit`
 * does the same thing and additionally arms itself from inside your worker.
 */

import { CallServer } from "../../index.js";
import { LiveKitHandler } from "./handler.js";
import { liveKitConfigFromEnv } from "./config.js";

export { liveKitConfigFromEnv, type LiveKitConfig } from "./config.js";
export {
  LiveKitHandler,
  type Connect,
  type LiveKitHandlerOptions,
} from "./handler.js";
export {
  TOPIC_CONTEXT,
  TOPIC_GOODBYE,
  contextPayload,
  connectRoom,
  type RoomHandlers,
  type RoomPort,
} from "./room.js";

/** Answer Microsoft Teams calls with LiveKit until the process is interrupted. */
export async function serve(): Promise<void> {
  const config = liveKitConfigFromEnv();
  const server = new CallServer({
    handlerFactory: () => new LiveKitHandler({ config }),
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
