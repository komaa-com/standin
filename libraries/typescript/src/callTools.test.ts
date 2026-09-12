// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The provider-neutral tool surface.
 *
 * Two things are being protected here. One, the same five capabilities reach
 * every provider with the same wording, so a caller gets the same agent
 * whichever one answers. Two, dispatch NEVER throws: its result is read out
 * loud, and an exception there is a silent tool and a caller left waiting.
 *
 * The Python twin is `tests/test_calltools.py`.
 */

import { describe, expect, it } from "vitest";

import { expression } from "./avatar.js";
import { BUILT_IN_TOOLS, CallTools, toolSchemas } from "./callTools.js";
import type { CallSession } from "./handler.js";
import type { SessionStart } from "./protocol.js";
import { VisionTools } from "./visionTools.js";
import type { VideoFrame, VideoSource } from "./vision.js";

const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0]).toString("base64");

function frame(source: VideoSource = "screenshare"): VideoFrame {
  return {
    source,
    ts: 1,
    width: 1280,
    height: 720,
    mime: "image/jpeg",
    dataBase64: JPEG,
    participantName: "Dana",
    data: Buffer.from(JPEG, "base64"),
    dataUrl: `data:image/jpeg;base64,${JPEG}`,
  };
}

class FakeCall {
  ended: string[] = [];
  emotions: string[] = [];
  readonly recordingActive: boolean;
  #frames: Partial<Record<VideoSource, VideoFrame>>;

  constructor(
    recording = false,
    frames: Partial<Record<VideoSource, VideoFrame>> = {},
  ) {
    this.recordingActive = recording;
    this.#frames = frames;
  }

  latestVideoFrame(source?: VideoSource): VideoFrame | undefined {
    if (source !== undefined) return this.#frames[source];
    return this.#frames.screenshare ?? this.#frames.camera;
  }

  async end(reason: string) {
    this.ended.push(reason);
  }

  async express(emotion: string) {
    // Build the real wire message, so this fake refuses exactly what the wire
    // refuses rather than something more forgiving.
    expression(emotion);
    this.emotions.push(emotion);
  }

  readonly callId = "call-1";
  readonly start = {} as SessionStart;
  readonly bufferedBytes = 0;
  readonly mediaTimeMs = 0;
  async sendAudio() {}
  async cancelPlayback() {}
  async displayImage() {}
  async sendSpeechMarks() {}
  async sendTileFrame() {}
}

class FakeDescriber {
  constructor(readonly answer = "a slide about revenue") {}
  async describe(_frame: VideoFrame, _question: string): Promise<string> {
    return this.answer;
  }
}

function tools(call: FakeCall, describer?: FakeDescriber): CallTools {
  const session = call as unknown as CallSession;
  return new CallTools(session, {
    vision: new VisionTools(session, { describer: describer as never }),
  });
}

describe("the declarations", () => {
  it("says the same five things whichever provider is asking", () => {
    const flat = toolSchemas("flat").map((s) => s.name);
    const openai = toolSchemas("openai").map((s) => s.name);
    const anthropic = toolSchemas("anthropic").map((s) => s.name);
    expect(flat).toEqual([
      "end_call",
      "express",
      "show_image",
      "look",
      "look_back",
    ]);
    expect(openai).toEqual(flat);
    expect(anthropic).toEqual(flat);
  });

  it("renders each provider's own shape", () => {
    expect(toolSchemas("flat")[0]).toMatchObject({
      name: "end_call",
      parameters: {},
    });
    expect(toolSchemas("openai")[0]).toMatchObject({
      type: "function",
      name: "end_call",
    });
    expect(toolSchemas("anthropic")[0]).toHaveProperty("input_schema");
    expect(toolSchemas("anthropic")[0]).not.toHaveProperty("parameters");
  });

  it("falls back to the flat shape rather than failing at connect time", () => {
    // A tool a model never sees is a worse outcome than a shape one provider
    // happens to also accept.
    expect(toolSchemas("something-new")).toEqual(toolSchemas("flat"));
  });

  it("keeps a required argument required in every dialect", () => {
    for (const dialect of ["flat", "openai", "anthropic"] as const) {
      const express = toolSchemas(dialect).find((s) => s.name === "express");
      const schema = (express?.parameters ?? express?.input_schema) as {
        required: string[];
      };
      expect(schema.required).toEqual(["emotion"]);
    }
  });

  it("describes each tool for a model, not for a maintainer", () => {
    // Every description has to say WHEN to reach for it; that sentence is the
    // only thing the model reads before deciding.
    for (const spec of BUILT_IN_TOOLS) {
      expect(spec.description.toLowerCase()).toContain("use");
      expect(spec.description.length).toBeGreaterThan(40);
    }
  });
});

describe("your own tools", () => {
  it("appear in the same list the model is given", async () => {
    const call = new FakeCall();
    const surface = tools(call);
    surface.register(
      {
        name: "open_ticket",
        description: "Use this when the caller reports a fault.",
      },
      () => "ticket 42 is open",
    );
    expect(surface.schemas("flat").map((s) => s.name)).toContain("open_ticket");
    expect(await surface.dispatch("open_ticket", {})).toBe("ticket 42 is open");
  });

  it("cannot take over a built-in, and are refused at registration", () => {
    // A shadowed end_call is an agent that has quietly lost the ability to hang
    // up, which is not something to discover mid-conversation.
    expect(() =>
      tools(new FakeCall()).register(
        { name: "end_call", description: "x" },
        () => "",
      ),
    ).toThrow(/built-in/);
  });

  it("do not take the call down when they fail", async () => {
    const surface = tools(new FakeCall());
    surface.register({ name: "flaky", description: "x" }, () => {
      throw new Error("the ticket system is down");
    });
    expect(await surface.dispatch("flaky", {})).toBe(
      "flaky failed: the ticket system is down",
    );
  });
});

describe("dispatch", () => {
  it("hangs up", async () => {
    const call = new FakeCall();
    expect(await tools(call).dispatch("end_call")).toBe("the call is ending");
    expect(call.ended).toEqual(["agent-ended-call"]);
  });

  it("reads an over-long emotion back to the model instead of raising", async () => {
    // The bound lives where the wire message is built, so a plugin cannot
    // forget it. What reaches the model is the reason, in words.
    const call = new FakeCall();
    const result = await tools(call).dispatch("express", {
      emotion: "x".repeat(41),
    });
    expect(result).toContain("at most 40 characters");
    expect(call.emotions).toEqual([]);
  });

  it("asks for the argument it is missing", async () => {
    expect(await tools(new FakeCall()).dispatch("express", {})).toContain(
      "needs an 'emotion'",
    );
    expect(
      await tools(new FakeCall()).dispatch("express", { emotion: "   " }),
    ).toContain("needs an 'emotion'");
  });

  it("expresses an emotion the avatar knows", async () => {
    const call = new FakeCall();
    expect(await tools(call).dispatch("express", { emotion: "happy" })).toBe(
      "expressing happy",
    );
    expect(call.emotions).toEqual(["happy"]);
  });

  it("looks at what the caller is showing", async () => {
    const call = new FakeCall(false, { screenshare: frame() });
    const result = await tools(call, new FakeDescriber()).dispatch("look", {
      question: "What is on the slide?",
    });
    expect(result).toBe("a slide about revenue");
  });

  it("says so when there is nothing to look at", async () => {
    const result = await tools(new FakeCall(), new FakeDescriber()).dispatch(
      "look",
      {},
    );
    expect(result).toContain("not sharing");
  });

  it("refuses a private address a model was talked into", async () => {
    const result = await tools(new FakeCall()).dispatch("show_image", {
      url: "http://169.254.169.254/latest/meta-data/",
    });
    expect(result).toContain("could not fetch");
  });

  it("survives arguments of the wrong type", async () => {
    // Tool arguments arrive as whatever the model emitted, which is not always
    // the type the schema asked for.
    const surface = tools(new FakeCall(), new FakeDescriber());
    expect(
      await surface.dispatch("express", { emotion: 7 as unknown as string }),
    ).toContain("needs an 'emotion'");
    expect(
      await surface.dispatch("show_image", { url: null as unknown as string }),
    ).toContain("needs a public");
    expect(
      await surface.dispatch("look", { source: 12 as unknown as string }),
    ).toContain("not sharing");
  });

  it("names a tool it does not have rather than going quiet", async () => {
    expect(await tools(new FakeCall()).dispatch("teleport", {})).toBe(
      '"teleport" is not a tool this agent has',
    );
  });

  it("tolerates a provider that sends no arguments at all", async () => {
    const call = new FakeCall();
    expect(await tools(call).dispatch("end_call", undefined as never)).toBe(
      "the call is ending",
    );
  });
});

describe("the ok bit", () => {
  it("is false only when the SDK knows the tool did not run", async () => {
    // Providers whose tool-result frame carries an error flag read this. It has
    // to mean something precise or it is worse than not having it.
    const surface = tools(new FakeCall(), new FakeDescriber());
    expect((await surface.run("end_call")).ok).toBe(true);
    expect((await surface.run("express", { emotion: "happy" })).ok).toBe(true);
    expect((await surface.run("express", {})).ok).toBe(false);
    expect((await surface.run("express", { emotion: "x".repeat(41) })).ok).toBe(
      false,
    );
    expect((await surface.run("teleport", {})).ok).toBe(false);
  });

  it("treats a vision refusal as an answer, not an error", async () => {
    // The vision tools answer in sentences by design, and the reason is in the
    // text where the model will read it.
    const result = await tools(new FakeCall(), new FakeDescriber()).run(
      "look",
      {},
    );
    expect(result.ok).toBe(true);
    expect(result.text).toContain("not sharing");
  });

  it("makes dispatch the text of run", async () => {
    const surface = tools(new FakeCall(), new FakeDescriber());
    expect(await surface.dispatch("look", {})).toBe(
      (await surface.run("look", {})).text,
    );
  });
});
