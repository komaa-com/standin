// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Showing the model what the caller is showing, without being asked.
 *
 * Three things keep this from being expensive or creepy: the recording gate, so
 * nobody's screen is streamed to a model without them being told; change
 * detection, so a screen nobody touched costs nothing; and a reserve, so ambient
 * spending cannot starve the caller's own request to look.
 *
 * The Python twin is `tests/test_ambient.py`.
 */

import { describe, expect, it } from "vitest";

import {
  AmbientVision,
  ambientImageDataUrl,
  type AmbientImage,
} from "./ambient.js";
import type { VideoFrame, VideoSource } from "./vision.js";
import { VisionBudget } from "./visionTools.js";

function frame(
  body = "one",
  source: VideoSource = "screenshare",
  name = "Dana",
): VideoFrame {
  const data = Buffer.from(body).toString("base64");
  return {
    source,
    ts: 1,
    width: 1280,
    height: 720,
    mime: "image/jpeg",
    dataBase64: data,
    participantName: name,
    data: Buffer.from(data, "base64"),
    dataUrl: `data:image/jpeg;base64,${data}`,
  };
}

class Sink {
  images: AmbientImage[] = [];
  failTimes = 0;

  readonly deliver = async (image: AmbientImage): Promise<void> => {
    if (this.failTimes > 0) {
      this.failTimes -= 1;
      throw new Error("the provider refused it");
    }
    this.images.push(image);
  };
}

/** Let the spawned pass run. */
async function settle(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("the gate", () => {
  it("is off unless a plugin turns it on", async () => {
    // It spends money on every scene change, and not every deployment wants it.
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver);
    ambient.offer(frame());
    await settle();
    expect(sink.images).toEqual([]);
    expect(ambient.queued).toBe(0);
  });

  it("stores nothing while the call is not recorded", async () => {
    // Streaming somebody's screen to a model is a different promise from
    // glancing at it once, and the recording is what told them.
    const sink = new Sink();
    const ambient = new AmbientVision(
      { recordingActive: false },
      sink.deliver,
      { enabled: true },
    );
    ambient.offer(frame());
    await settle();
    expect(sink.images).toEqual([]);
  });

  it("does not surface an earlier frame when the gate opens", async () => {
    // Otherwise turning the recording on reaches back to before the caller was
    // told anything was being kept.
    const session = { recordingActive: false };
    const sink = new Sink();
    const ambient = new AmbientVision(session, sink.deliver, { enabled: true });
    ambient.offer(frame("before the recording"));
    await settle();

    (session as { recordingActive: boolean }).recordingActive = true;
    ambient.flush();
    await settle();
    expect(sink.images).toEqual([]);
  });

  it("can be turned off for a deployment that does not need it", async () => {
    const sink = new Sink();
    const ambient = new AmbientVision(
      { recordingActive: false },
      sink.deliver,
      {
        enabled: true,
        requireRecording: false,
      },
    );
    ambient.offer(frame());
    await settle();
    expect(sink.images).toHaveLength(1);
  });
});

describe("the changes", () => {
  it("sends a changed screen to the model", async () => {
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
    });
    ambient.offer(frame("the first slide"));
    await settle();
    expect(sink.images).toHaveLength(1);
    expect(sink.images[0]!.owner).toBe("Dana");
    expect(sink.images[0]!.caption).toContain("Dana");
    expect(
      ambientImageDataUrl(sink.images[0]!).startsWith(
        "data:image/jpeg;base64,",
      ),
    ).toBe(true);
  });

  it("charges nothing for a screen nobody touched", async () => {
    const sink = new Sink();
    const budget = new VisionBudget(10);
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
      budget,
    });
    for (let i = 0; i < 5; i += 1) {
      ambient.offer(frame("the same slide"));
      await settle();
    }
    expect(sink.images).toHaveLength(1);
    expect(budget.spent).toBe(1);
  });

  it("tries again after a delivery that failed", async () => {
    // The latch is the last frame DELIVERED, not the last one seen. Latching a
    // frame that never arrived means the model never sees that screen.
    const sink = new Sink();
    sink.failTimes = 1;
    const budget = new VisionBudget(10);
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
      budget,
    });

    ambient.offer(frame("the slide"));
    await settle();
    expect(sink.images).toEqual([]);
    // The charge was given back, because nothing was delivered.
    expect(budget.spent).toBe(0);

    ambient.flush();
    await settle();
    expect(sink.images).toHaveLength(1);
  });

  it("latches each source separately", async () => {
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
    });
    ambient.offer(frame("shared screen", "screenshare"));
    ambient.offer(frame("their face", "camera"));
    await settle();
    expect(new Set(sink.images.map((i) => i.source))).toEqual(
      new Set(["screenshare", "camera"]),
    );
  });

  it("takes the screen share first", async () => {
    // Somebody presenting is nearly always talking about the screen rather than
    // about their face.
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
    });
    ambient.offer(frame("their face", "camera"));
    ambient.offer(frame("shared screen", "screenshare"));
    await settle();
    expect(sink.images.map((i) => i.source)).toEqual(["screenshare", "camera"]);
  });
});

describe("the budget", () => {
  it("reserves what ambient cannot spend", () => {
    expect(new VisionBudget(12).reserve).toBe(3);
    expect(new VisionBudget(4).reserve).toBe(2); // never less than two
    expect(new VisionBudget(0).reserve).toBe(0); // uncapped means uncapped
  });

  it("stops ambient at the reserve and leaves an explicit look room", () => {
    // Ambient spends on every scene change, which is exactly the load that
    // would leave the caller's own request with nothing left.
    const budget = new VisionBudget(8);
    let taken = 0;
    while (budget.tryConsumeAmbient() !== undefined) taken += 1;
    expect(taken).toBe(6); // eight minus a reserve of two
    expect(budget.tryConsume()).toBeDefined();
  });

  it("stops the pass on an exhausted budget rather than moving on", async () => {
    const sink = new Sink();
    const budget = new VisionBudget(3);
    for (let i = 0; i < budget.maxPerMinute - budget.reserve; i += 1)
      budget.tryConsume();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
      budget,
    });
    ambient.offer(frame("a", "screenshare"));
    ambient.offer(frame("b", "camera"));
    await settle();
    expect(sink.images).toEqual([]);
  });
});

describe("the queue", () => {
  it("holds frames until the provider is ready", async () => {
    let ready = false;
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
      sinkReady: () => ready,
    });
    ambient.offer(frame("the first slide"));
    await settle();
    expect(sink.images).toEqual([]);
    expect(ambient.queued).toBe(1);

    ready = true;
    ambient.flush();
    await settle();
    expect(sink.images).toHaveLength(1);
    expect(ambient.queued).toBe(0);
  });

  it("bounds what it holds", async () => {
    // A sink that never comes up would otherwise hold the whole call's video.
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
      sinkReady: () => false,
      queueMax: 2,
    });
    for (let i = 0; i < 6; i += 1) {
      ambient.offer(frame(`slide ${i}`));
      await settle();
    }
    expect(ambient.queued).toBe(2);
  });
});

describe("the bookkeeping", () => {
  it("can record what was shown", async () => {
    // The hook a meeting recap uses, so the minutes say what was on screen.
    const seen: string[] = [];
    const ambient = new AmbientVision(
      { recordingActive: true },
      new Sink().deliver,
      {
        enabled: true,
        onDelivered: (image) => seen.push(image.owner),
      },
    );
    ambient.offer(frame("the slide"));
    await settle();
    expect(seen).toEqual(["Dana"]);
  });

  it("degrades attribution rather than losing it", async () => {
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
    });
    ambient.offer(frame("a slide", "screenshare", ""));
    await settle();
    expect(sink.images[0]!.owner).toBe("a participant");
  });

  it("is permanently inert once closed", async () => {
    const sink = new Sink();
    const ambient = new AmbientVision({ recordingActive: true }, sink.deliver, {
      enabled: true,
    });
    await ambient.close();
    ambient.offer(frame("after the call"));
    ambient.flush();
    await settle();
    expect(sink.images).toEqual([]);
  });
});
