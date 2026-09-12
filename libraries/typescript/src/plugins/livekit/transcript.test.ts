// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Telling the caller's words from the agent's own.
 *
 * LiveKit publishes transcripts of BOTH sides on one topic. Getting this wrong
 * is not a cosmetic bug: an agent greeting that says its own name would open the
 * follow-up window and make the assistant answer the next turn of a meeting
 * nobody addressed it in, which is exactly what the group gate exists to stop.
 */

import { describe, expect, it } from "vitest";

import {
  ATTRIBUTE_TRANSCRIBED_TRACK_ID,
  LOCAL_IDENTITY,
  isCallerTranscript,
  readTranscript,
  type TextStreamReader,
} from "./room.js";

function reader(
  chunks: string[],
  attributes?: Record<string, string>,
): TextStreamReader {
  return {
    info: attributes === undefined ? undefined : { attributes },
    async *[Symbol.asyncIterator]() {
      for (const chunk of chunks) yield chunk;
    },
  };
}

describe("whose words these are", () => {
  it("matches the caller's track exactly when the id is there", () => {
    expect(
      isCallerTranscript(
        { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_caller" },
        "anyone",
        {
          callerTrackSid: "TR_caller",
          localIdentity: LOCAL_IDENTITY,
        },
      ),
    ).toBe(true);
    expect(
      isCallerTranscript(
        { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_agent" },
        "anyone",
        {
          callerTrackSid: "TR_caller",
          localIdentity: LOCAL_IDENTITY,
        },
      ),
    ).toBe(false);
  });

  it("falls back to who sent it when there is no track id", () => {
    expect(
      isCallerTranscript(undefined, LOCAL_IDENTITY, {
        callerTrackSid: "TR_caller",
        localIdentity: LOCAL_IDENTITY,
      }),
    ).toBe(true);
  });

  it("treats anything that is not the agent as the caller, once the agent is bound", () => {
    expect(
      isCallerTranscript(undefined, "somebody-else", {
        localIdentity: LOCAL_IDENTITY,
        agentIdentity: "agent-1",
      }),
    ).toBe(true);
    expect(
      isCallerTranscript(undefined, "agent-1", {
        localIdentity: LOCAL_IDENTITY,
        agentIdentity: "agent-1",
      }),
    ).toBe(false);
  });

  it("ignores a stream it cannot place", () => {
    expect(
      isCallerTranscript(undefined, undefined, {
        localIdentity: LOCAL_IDENTITY,
      }),
    ).toBe(false);
    // The agent is not bound yet and the sender is not us: not classifiable.
    expect(
      isCallerTranscript(undefined, "someone", {
        localIdentity: LOCAL_IDENTITY,
      }),
    ).toBe(false);
  });

  it("never matches a cached sid that has gone stale", () => {
    // The SDK re-issues the sid in place after a reconnect. A cached string
    // stops matching, and every caller transcript is then read as the agent's.
    expect(
      isCallerTranscript(
        { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_new" },
        "anyone",
        {
          callerTrackSid: undefined,
          localIdentity: LOCAL_IDENTITY,
        },
      ),
    ).toBe(false);
  });
});

describe("reading a transcript stream", () => {
  const route = {
    callerTrackSid: () => "TR_caller",
    localIdentity: LOCAL_IDENTITY,
    agentIdentity: () => "agent-1",
    isClosed: () => false,
  };

  it("reports every partial and then one final", async () => {
    // A partial exists so a wake phrase is noticed as it is said.
    const seen: Array<[string, boolean]> = [];
    await readTranscript(
      reader(["assistant", "assistant, what", "assistant, what is the plan"], {
        [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_caller",
      }),
      "caller",
      { ...route, deliver: (text, final) => void seen.push([text, final]) },
    );
    expect(seen).toEqual([
      ["assistant", false],
      ["assistant, what", false],
      ["assistant, what is the plan", false],
      ["assistant, what is the plan", true],
    ]);
  });

  it("reports nothing for the agent's own speech", async () => {
    const seen: string[] = [];
    await readTranscript(
      reader(["Hi, I'm Assistant"], {
        [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_agent",
      }),
      "agent-1",
      { ...route, deliver: (text) => void seen.push(text) },
    );
    expect(seen).toEqual([]);
  });

  it("drains a stream it ignores", async () => {
    // An abandoned reader keeps its subscription for the life of the process,
    // and the agent publishes one stream per turn.
    let drained = 0;
    const counting: TextStreamReader = {
      info: { attributes: { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_agent" } },
      async *[Symbol.asyncIterator]() {
        for (const chunk of ["one", "two"]) {
          drained += 1;
          yield chunk;
        }
      },
    };
    await readTranscript(counting, "agent-1", {
      ...route,
      deliver: () => undefined,
    });
    expect(drained).toBe(2);
  });

  it("stops when the call is over", async () => {
    const seen: string[] = [];
    await readTranscript(
      reader(["hello"], { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_caller" }),
      "caller",
      {
        ...route,
        isClosed: () => true,
        deliver: (text) => void seen.push(text),
      },
    );
    expect(seen).toEqual([]);
  });

  it("survives a stream that ends early", async () => {
    const broken: TextStreamReader = {
      info: { attributes: { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_caller" } },
      async *[Symbol.asyncIterator]() {
        yield "hello";
        throw new Error("the stream was cut");
      },
    };
    const seen: Array<[string, boolean]> = [];
    await readTranscript(broken, "caller", {
      ...route,
      deliver: (text, final) => void seen.push([text, final]),
    });
    // The partial was reported; no final, because the turn never finished.
    expect(seen).toEqual([["hello", false]]);
  });

  it("reports nothing for an empty turn", async () => {
    const seen: string[] = [];
    await readTranscript(
      reader([""], { [ATTRIBUTE_TRANSCRIBED_TRACK_ID]: "TR_caller" }),
      "caller",
      {
        ...route,
        deliver: (text) => void seen.push(text),
      },
    );
    expect(seen).toEqual([]);
  });
});
