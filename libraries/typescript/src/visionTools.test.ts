// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The vision and display tools, and the two guards that travel with them.
 *
 * Every one of these returns a sentence rather than throwing, because the
 * caller is a tool result being read back to something that will say it out
 * loud. The Python twin is `tests/test_vision_tools.py`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { BUILT_IN_TOOLS, SHOW_PAGE_TOOL } from "./callTools.js";
import type { CallSession } from "./handler.js";
import type { SessionStart } from "./protocol.js";
import {
  KeyframeStore,
  SLIDESHOW_OVERLAP_MS,
  VisionBudget,
  VisionTools,
  displayImageName,
  normalizeDisplayMode,
} from "./visionTools.js";
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
  images: Array<{
    image: Buffer | string;
    mime?: string;
    caption?: string;
    durationMs?: number;
    mode?: string;
  }> = [];
  failWith: Error | undefined;
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

  async displayImage(
    image: Buffer | string,
    options: {
      mime?: string;
      caption?: string;
      durationMs?: number;
      mode?: string;
    } = {},
  ) {
    if (this.failWith) throw this.failWith;
    this.images.push({
      image,
      mime: options.mime,
      caption: options.caption,
      durationMs: options.durationMs,
      mode: options.mode,
    });
  }

  readonly callId = "call-1";
  readonly start = {} as SessionStart;
  readonly bufferedBytes = 0;
  readonly mediaTimeMs = 0;
  async sendAudio() {}
  async cancelPlayback() {}
  async end() {}
  async express() {}
  async sendSpeechMarks() {}
  async sendTileFrame() {}
}

class FakeDescriber {
  asked: Array<[string, string]> = [];
  fail = false;
  constructor(readonly answer = "a slide about revenue") {}
  async describe(f: VideoFrame, question: string): Promise<string> {
    if (this.fail) throw new Error("the vision endpoint is down");
    this.asked.push([f.source, question]);
    return this.answer;
  }
}

function tools(
  call: FakeCall,
  describer?: FakeDescriber,
  budget?: VisionBudget,
): VisionTools {
  return new VisionTools(call as unknown as CallSession, {
    describer: describer as never,
    budget,
  });
}

describe("looking", () => {
  it("answers about the newest frame", async () => {
    const describer = new FakeDescriber();
    const t = tools(new FakeCall(false, { screenshare: frame() }), describer);
    expect(await t.look("What is on the slide?")).toBe("a slide about revenue");
    expect(describer.asked).toEqual([["screenshare", "What is on the slide?"]]);
  });

  it("prefers the screen share but honours a request", async () => {
    const describer = new FakeDescriber();
    const t = tools(
      new FakeCall(false, { screenshare: frame(), camera: frame("camera") }),
      describer,
    );
    await t.look();
    await t.look("", "camera");
    expect(describer.asked.map(([s]) => s)).toEqual(["screenshare", "camera"]);
  });

  it("says so when there is nothing to see", async () => {
    expect(await tools(new FakeCall(), new FakeDescriber()).look()).toContain(
      "not sharing",
    );
  });

  it("says so when no vision model is configured", async () => {
    // Saying it plainly beats a silent tool.
    const t = new VisionTools(
      new FakeCall(false, { screenshare: frame() }) as unknown as CallSession,
    );
    expect(await t.look()).toContain("no vision model is configured");
  });

  it("refunds a failed look rather than charging it", async () => {
    // A flaky endpoint would otherwise burn a budget the caller paid nothing for.
    const describer = new FakeDescriber();
    describer.fail = true;
    const budget = new VisionBudget(2);
    const t = tools(
      new FakeCall(false, { screenshare: frame() }),
      describer,
      budget,
    );
    expect(await t.look()).toContain("could not look");
    expect(budget.spent).toBe(0);
  });

  it("stops a model looking in a loop", async () => {
    const t = tools(
      new FakeCall(false, { screenshare: frame() }),
      new FakeDescriber(),
      new VisionBudget(2),
    );
    expect(await t.look()).toBe("a slide about revenue");
    expect(await t.look()).toBe("a slide about revenue");
    expect(await t.look()).toContain("reached its limit");
  });
});

describe("the vision budget", () => {
  it("refunds the charge it was given, not the newest", () => {
    // Two tool calls overlap. Refunding "the most recent" would refund the wrong
    // one and let the budget drift upward under exactly the load it bounds.
    const budget = new VisionBudget(2);
    const first = budget.tryConsume()!;
    budget.tryConsume();
    expect(budget.tryConsume()).toBeUndefined();

    budget.refund(first);
    expect(budget.tryConsume()).toBeDefined();
    expect(budget.spent).toBe(2);
  });

  it("treats a zero budget as no budget", () => {
    const budget = new VisionBudget(0);
    for (let i = 0; i < 50; i += 1) expect(budget.tryConsume()).toBeDefined();
  });
});

function distinct(
  index: number,
  source: VideoSource = "screenshare",
): VideoFrame {
  const body = Buffer.from(`frame-${index}`).toString("base64");
  return {
    source,
    ts: index,
    width: 1280,
    height: 720,
    mime: "image/jpeg",
    dataBase64: body,
    participantName: "Dana",
    data: Buffer.from(body, "base64"),
    dataUrl: `data:image/jpeg;base64,${body}`,
  };
}

describe("keyframes", () => {
  it("keeps frames only while the call is recorded", () => {
    // Keeping a history of somebody's screen is a different promise from
    // glancing at it once, and the recording is what told them.
    const store = new KeyframeStore();
    expect(store.offer(frame(), false)).toBe(false);
    expect(store.size).toBe(0);
    expect(store.offer(frame(), true)).toBe(true);
    expect(store.size).toBe(1);
  });

  it("stays bounded", () => {
    const store = new KeyframeStore(3);
    // Frames that differ, so the dedup below does not collapse them.
    for (let i = 0; i < 10; i += 1) store.offer(distinct(i), true);
    expect(store.size).toBe(3);
  });

  it("keeps an unchanged screen once", () => {
    // A screen nobody touched would otherwise fill the whole store with one
    // picture, and looking back would find nothing else.
    const store = new KeyframeStore(5);
    for (let i = 0; i < 10; i += 1) store.offer(frame(), true);
    expect(store.size).toBe(1);
  });

  it("gives each source its own history", () => {
    // An alternating camera and screen share are two things being shown, not
    // one changing.
    const store = new KeyframeStore(5);
    for (let i = 0; i < 3; i += 1) {
      store.offer(frame("screenshare"), true);
      store.offer(frame("camera"), true);
    }
    expect(store.size).toBe(2);
  });

  it("needs a recorded call to look back", async () => {
    expect(
      await tools(new FakeCall(false), new FakeDescriber()).lookBack(),
    ).toContain("while the call is being recorded");
  });

  it("answers about a frame already gone", async () => {
    const t = tools(new FakeCall(true), new FakeDescriber("the earlier slide"));
    t.keyframes.offer(frame(), true);
    expect(await t.lookBack("what did it say?")).toBe("the earlier slide");
  });
});

describe("showing", () => {
  it("puts an image on the tile", async () => {
    const call = new FakeCall();
    expect(
      await tools(call).show(Buffer.from([0xff, 0xd8]), "image/jpeg", "Q3"),
    ).toBe("the caller can see it");
    expect(call.images[0]?.caption).toBe("Q3");
  });

  it("refuses a type the service will not draw", async () => {
    expect(
      await tools(new FakeCall()).show(Buffer.from([0]), "image/gif"),
    ).toContain("must be one of");
  });

  it("turns an oversized image into a sentence, not an exception", async () => {
    // The wire has a hard ceiling. A model must be told it in words, because it
    // cannot see an exception.
    const call = new FakeCall();
    call.failWith = new Error(
      "display.image is 9000000 bytes, over the 1400000 limit",
    );
    const result = await tools(call).show(
      Buffer.from([0xff, 0xd8]),
      "image/jpeg",
    );
    expect(result).toContain("could not show that");
    expect(result).toContain("over the");
  });

  it("trims a long caption before it reaches the screen", async () => {
    const call = new FakeCall();
    await tools(call).show(
      Buffer.from([0xff, 0xd8]),
      "image/jpeg",
      "x".repeat(500),
    );
    expect(call.images[0]?.caption?.length).toBe(200);
  });

  it("refuses a private address from a model-chosen URL", async () => {
    // The URL comes from a model steered by whoever is on the call.
    const result = await tools(new FakeCall()).showUrl(
      "http://169.254.169.254/latest/meta-data/",
    );
    expect(result).toContain("could not fetch");
  });

  it("needs a url", async () => {
    expect(await tools(new FakeCall()).showUrl("  ")).toContain(
      "needs a public",
    );
  });
});

describe("walkthrough", () => {
  it("speaks each step before showing it", async () => {
    // The pacing is in the SDK; the speaking is the plugin's, because only the
    // provider knows when a line has finished being said.
    const call = new FakeCall();
    const said: string[] = [];
    const result = await tools(call).walkthrough(
      [
        { say: "First, the summary.", image: Buffer.from("one") },
        { say: "Then the detail.", image: Buffer.from("two") },
      ],
      async (text) => {
        said.push(text);
      },
    );
    expect(said).toEqual(["First, the summary.", "Then the detail."]);
    expect(call.images.length).toBe(2);
    expect(result).toContain("all 2 steps");
  });

  it("stops when the caller cuts in", async () => {
    const said: string[] = [];
    let cutIn = false;
    const result = await tools(new FakeCall()).walkthrough(
      [{ say: "one" }, { say: "two" }, { say: "three" }],
      async (text) => {
        said.push(text);
        cutIn = true;
      },
      () => cutIn,
    );
    expect(said).toEqual(["one"]);
    expect(result).toContain("the caller interrupted");
  });

  it("says so when there is nothing to walk through", async () => {
    expect(
      await tools(new FakeCall()).walkthrough([], async () => undefined),
    ).toContain("nothing to walk through");
  });
});

// ---- the display surface -------------------------------------------------

/** The SDK clamps a hold up to this floor, so the tests drive the clock. */
const HOLD = 1_000;

function item(tag: string) {
  return { image: Buffer.from(`\xff\xd8\xff${tag}`), mime: "image/jpeg" };
}

function held(tools: VisionTools): Promise<void> {
  return tools.slideshow ?? Promise.resolve();
}

describe("the display surface", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps only the two real modes", () => {
    expect(normalizeDisplayMode("FULLSCREEN ")).toBe("fullscreen");
    expect(normalizeDisplayMode("overlay")).toBe("overlay");
    // Everything a model says that is not a mode: "pip", "full", a number,
    // nothing at all. All of them take the default rather than being passed on.
    for (const said of ["pip", "inset", "full", "", undefined, 7]) {
      expect(normalizeDisplayMode(said)).toBeUndefined();
      expect(normalizeDisplayMode(said, "overlay")).toBe("overlay");
    }
  });

  it("takes a name only when it looks like one", () => {
    expect(
      displayImageName("https://x.example/y/chart.png?v=1", "image/png"),
    ).toBe("chart.png");
    expect(displayImageName("/srv/decks/q3.jpg", "image/jpeg")).toBe("q3.jpg");
    // A traversal, a bare directory, and nothing at all are refused in favour
    // of a name made from the type: this string is about to be shown to the
    // person on the call.
    expect(displayImageName("../../etc/passwd", "image/jpeg")).toBe(
      "image.jpg",
    );
    expect(displayImageName("", "image/png")).toBe("image.png");
  });

  it("omits the mode unless somebody chose one", async () => {
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    await tools.show(Buffer.from("x"), "image/jpeg");
    // Not "fullscreen": a default chosen here would override the service's own.
    expect(call.images[0]!.mode).toBeUndefined();

    await tools.show(
      Buffer.from("x"),
      "image/jpeg",
      undefined,
      undefined,
      "overlay",
    );
    expect(call.images[1]!.mode).toBe("overlay");

    // Nonsense from a model falls back to the configured default, not to the
    // nonsense.
    const other = new FakeCall();
    const defaulted = new VisionTools(other as unknown as CallSession, {
      defaultDisplayMode: "fullscreen",
    });
    await defaulted.show(
      Buffer.from("x"),
      "image/jpeg",
      undefined,
      undefined,
      "picture-in-picture",
    );
    expect(other.images[0]!.mode).toBe("fullscreen");
  });

  it("remembers what was shown only when it arrived", async () => {
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    expect(tools.lastShown).toBeUndefined();

    await tools.show(
      Buffer.from("first"),
      "image/jpeg",
      undefined,
      undefined,
      undefined,
      "chart.png",
    );
    expect(tools.lastShown?.name).toBe("chart.png");
    expect(tools.lastShown?.asBase64()).toBe(
      Buffer.from("first").toString("base64"),
    );

    // A send that failed must not leave a picture the caller never saw behind
    // for "send me that" to attach.
    call.failWith = new Error("that image is too large");
    const said = await tools.show(
      Buffer.from("newer"),
      "image/jpeg",
      undefined,
      undefined,
      undefined,
      "other.png",
    );
    expect(said.startsWith("could not show that")).toBe(true);
    expect(tools.lastShown?.name).toBe("chart.png");
  });

  it("puts the first picture up before answering the model", async () => {
    vi.useFakeTimers();
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    const said = await tools.showMany(
      [item("a"), item("b"), item("c")],
      undefined,
      undefined,
      HOLD,
    );
    // The model can say "here it is" and be right: one is already on the tile
    // when the tool returns. Waiting out all three would leave the caller in
    // silence.
    expect(call.images).toHaveLength(1);
    expect(said).toContain("the first of 3");

    await vi.advanceTimersByTimeAsync(HOLD * 3);
    await held(tools);
    expect(call.images).toHaveLength(3);
    // Every frame but the last is held a little past the pacing gap, so the
    // tile never blanks between pictures. The last carries no duration, so what
    // stays on screen is the service's own default.
    expect(call.images[0]!.durationMs).toBe(HOLD + SLIDESHOW_OVERLAP_MS);
    expect(call.images[1]!.durationMs).toBe(HOLD + SLIDESHOW_OVERLAP_MS);
    expect(call.images[2]!.durationMs).toBeUndefined();
  });

  it("says how many it dropped", async () => {
    vi.useFakeTimers();
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    const many = Array.from({ length: 14 }, (_, n) => item(String(n)));
    const said = await tools.showMany(many, undefined, undefined, HOLD);
    await vi.advanceTimersByTimeAsync(HOLD * 12);
    await held(tools);
    expect(said).toContain("showing the first 10 of 14");
    expect(call.images).toHaveLength(10);
  });

  it("does not wait out the old gap when a slideshow is replaced", async () => {
    vi.useFakeTimers();
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    // Thirty seconds is a legal hold. Waiting one out before the new first
    // picture went up would look, to the caller, like the agent had frozen: no
    // clock is advanced between these two calls.
    await tools.showMany(
      Array.from({ length: 4 }, (_, n) => item(`old${n}`)),
      undefined,
      undefined,
      30_000,
    );
    await tools.showMany(
      [item("new0"), item("new1")],
      undefined,
      undefined,
      HOLD,
    );
    expect(String(call.images.at(-1)!.image)).toContain("new0");

    await vi.advanceTimersByTimeAsync(HOLD * 2);
    await held(tools);
    // One tile. The old slideshow must not go on writing to it underneath the
    // new one.
    expect(call.images).toHaveLength(3);
    expect(String(call.images.at(-1)!.image)).toContain("new1");
  });

  it("stops the slideshow at teardown", async () => {
    vi.useFakeTimers();
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    await tools.showMany(
      Array.from({ length: 8 }, (_, n) => item(String(n))),
      undefined,
      undefined,
      HOLD,
    );
    const sent = call.images.length;
    await tools.reset();
    await vi.advanceTimersByTimeAsync(HOLD * 3);
    expect(call.images).toHaveLength(sent);
    expect(tools.lastShown).toBeUndefined();
  });

  it("says so when there is nothing to show", async () => {
    const tools = new VisionTools(new FakeCall() as unknown as CallSession);
    expect(await tools.showMany([])).toBe("there was nothing to show");
  });

  it("lets a walkthrough take the tile from a slideshow", async () => {
    vi.useFakeTimers();
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    await tools.showMany(
      Array.from({ length: 8 }, (_, n) => item(`slide${n}`)),
      undefined,
      undefined,
      HOLD,
    );
    await tools.walkthrough(
      [
        {
          say: "here is step one",
          image: Buffer.from("walk"),
          caption: "step one",
        },
      ],
      async () => {},
      undefined,
      "fullscreen",
    );
    await vi.advanceTimersByTimeAsync(HOLD * 3);
    expect(String(call.images.at(-1)!.image)).toContain("walk");
    expect(call.images.at(-1)!.mode).toBe("fullscreen");
  });
});

// ---- showing a web page --------------------------------------------------

async function png(): Promise<{ bytes: Buffer; mime: string }> {
  return {
    bytes: Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    mime: "image/png",
  };
}

describe("showing a web page", () => {
  it("needs a renderer and says so", async () => {
    // No browser lives in this SDK, and none ever will. Without one supplied,
    // the answer is a sentence rather than a promise the deployment cannot keep.
    const tools = new VisionTools(new FakeCall() as unknown as CallSession);
    expect(await tools.showPage("https://example.com")).toBe(
      "showing web pages is not available on this deployment",
    );
    expect(await tools.showPage("", undefined, png)).toBe(
      "that needs a public https URL of a page",
    );
  });

  it("refuses a private address before the renderer runs", async () => {
    // The guard is here, not in the plugin. A host browser's own
    // private-network protection assumes whoever wrote the URL already has a
    // shell on the machine. Here it was written by a model a stranger is
    // steering, which is the case that relaxation lets through.
    const reached: string[] = [];
    const tools = new VisionTools(new FakeCall() as unknown as CallSession);
    const said = await tools.showPage(
      "http://169.254.169.254/latest/meta-data/",
      undefined,
      async (url) => {
        reached.push(url);
        return await png();
      },
    );
    expect(said.startsWith("could not open that page")).toBe(true);
    expect(reached).toEqual([]);
  });

  it("puts a rendered page on the tile with a caption", async () => {
    const call = new FakeCall();
    const tools = new VisionTools(call as unknown as CallSession);
    const longUrl = `https://example.com/${"a".repeat(200)}`;
    expect(await tools.showPage(longUrl, undefined, png)).toBe(
      "the caller can see it",
    );
    expect(call.images[0]!.mime).toBe("image/png");
    // A URL makes a poor caption at any length and a worse one at 220
    // characters, and this one is read out on the caller's screen.
    expect(call.images[0]!.caption).toHaveLength(80);
    expect(call.images[0]!.durationMs).toBe(15_000);
  });

  it("says in seconds when a page never loads", async () => {
    const tools = new VisionTools(new FakeCall() as unknown as CallSession);
    const said = await tools.showPage(
      "https://example.com",
      undefined,
      () => new Promise(() => {}),
      10,
    );
    expect(said).toBe("that page did not finish loading within 1 second");
  });

  it("is not offered unless a plugin has a renderer", () => {
    // An absent tool is honest; one that apologises on every call is not.
    expect(BUILT_IN_TOOLS.map((spec) => spec.name)).not.toContain(
      SHOW_PAGE_TOOL.name,
    );
  });
});
