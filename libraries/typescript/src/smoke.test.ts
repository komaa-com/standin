// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Proving an install works without placing a real call.
 *
 * The twin is `libraries/python/tests/test_smoke.py`.
 */

import { describe, expect, it } from "vitest";

import type { CallHandler, CallSession } from "./handler.js";
import { report, runSmoke } from "./smoke.js";

class Echo implements CallHandler {
  #session: CallSession | undefined;
  async onStart(session: CallSession): Promise<void> {
    this.#session = session;
  }
  async onCallerAudio(pcm: Buffer): Promise<void> {
    await this.#session?.sendAudio(pcm);
  }
}

class Silent implements CallHandler {
  async onStart(): Promise<void> {}
}

describe("the smoke check", () => {
  it("rings the worker's own handler and proves audio came back", async () => {
    const result = await runSmoke(() => new Echo(), 4);
    expect(result.ok).toBe(true);
    expect(result.echoFrames).toBeGreaterThan(0);
    expect(result.checks.map((c) => c.name)).toEqual([
      "secret",
      "listener",
      "call",
      "audio",
    ]);
    // The listener is on loopback, never on every interface: a verification run
    // has no business being reachable from the network.
    expect(result.checks[1]!.detail).toContain("127.0.0.1:");
  });

  it("is not ok when nothing came back", async () => {
    // A run that proves nothing must not read as a pass.
    const result = await runSmoke(() => new Silent(), 3);
    expect(result.ok).toBe(false);
    expect(result.echoFrames).toBe(0);
    expect(report(result)).toContain("the caller would hear nothing");
  });

  it("takes a plugin's own checks, advisory unless they say otherwise", async () => {
    const advisory = await runSmoke(
      () => new Echo(),
      2,
      async () => [
        {
          name: "browser",
          ok: false,
          cost: "show_page would apologise",
          required: false,
        },
      ],
    );
    expect(advisory.ok).toBe(true);

    const mandatory = await runSmoke(
      () => new Echo(),
      2,
      async () => [
        {
          name: "model",
          ok: false,
          cost: "the agent could not answer",
          required: true,
        },
      ],
    );
    expect(mandatory.ok).toBe(false);
  });

  it("records a plugin check that threw rather than failing the run", async () => {
    const result = await runSmoke(
      () => new Echo(),
      2,
      async () => {
        throw new Error("the plugin check exploded");
      },
    );
    expect(result.ok).toBe(true);
    expect(result.checks.at(-1)!.name).toBe("plugin checks");
  });
});
