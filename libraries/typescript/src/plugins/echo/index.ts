// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The smallest StandIn plugin that answers a real Microsoft Teams call.
 *
 * Copy this directory to start a new plugin. It has no dependency beyond
 * the SDK core and it works: call the number, talk, and hear yourself back. That
 * makes it a useful thing to run before you suspect your own agent - if the echo
 * answers, your secret, your tunnel and your StandIn identity are all correct.
 *
 * ```bash
 * npm install @komaa/standin-sdk
 * STANDIN_SECRET=... npx standin-echo
 * ```
 *
 * To make it a real plugin, replace {@link EchoHandler.onCallerAudio} with
 * your framework's agent loop. Everything else here is the shape every
 * plugin keeps.
 *
 * The Python twin of this file is `standin.plugins.echo`; the two are the
 * same handler with the same method names in each language's casing.
 */

import { CallServer, type CallSession } from "../../index.js";

/**
 * One instance per call. Sends the caller's own voice back to them.
 *
 * A handler implements only what it needs: the SDK treats every callback as
 * optional, so this class defines three of the five and the other two are
 * no-ops. Nothing extends anything.
 */
export class EchoHandler {
  #call: CallSession | undefined;

  async onStart(session: CallSession): Promise<void> {
    this.#call = session;
    console.info(
      `call ${session.callId} from ${session.start.caller.displayName ?? "unknown"}`,
    );
  }

  async onCallerAudio(pcm: Buffer): Promise<void> {
    // Your agent goes here. PCM16, 16 kHz, mono, little-endian - the same format
    // sendAudio expects back.
    await this.#call?.sendAudio(pcm);
  }

  async onGoodbye(text: string): Promise<void> {
    // StandIn is ending the call and wants this line spoken first. A real plugin
    // would interrupt the agent and say it.
    console.info(`goodbye: ${text}`);
  }
}

/** Answer Microsoft Teams calls until the process is interrupted. */
export async function serve(): Promise<void> {
  const server = new CallServer({ handlerFactory: () => new EchoHandler() });
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
