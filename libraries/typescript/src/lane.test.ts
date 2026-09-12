// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Listen, transcribe, answer, speak, for agents that are not speech to speech.
 *
 * The twin is `libraries/python/tests/test_lane.py`.
 */

import { describe, expect, it } from "vitest";

import type { CallSession } from "./handler.js";
import {
  TROUBLE_ANSWERING,
  TROUBLE_HEARING,
  VoiceLane,
  type VoiceTurn,
} from "./lane.js";
import { UtteranceSegmenter } from "./voice.js";

const FRAME_MS = 20;
const FRAME = ((16_000 * FRAME_MS) / 1000) * 2;

const LOUD = Buffer.alloc(FRAME).fill(Buffer.from([0x00, 0x40]));
const QUIET = Buffer.alloc(FRAME);

class FakeCall {
  readonly callId = "call-1";
  sent: Buffer[] = [];
  cancelled = 0;
  async sendAudio(pcm: Buffer): Promise<void> {
    this.sent.push(pcm);
  }
  async cancelPlayback(): Promise<void> {
    this.cancelled += 1;
  }
}

interface Steps {
  transcribe?: (pcm: Buffer) => Promise<string>;
  answer?: (text: string) => Promise<string> | AsyncIterable<string>;
  synthesize?: (text: string) => Promise<Buffer> | AsyncIterable<Buffer>;
  segmenter?: UtteranceSegmenter;
  onTurn?: (turn: VoiceTurn) => void;
}

function lane(call: FakeCall, steps: Steps = {}): VoiceLane {
  return new VoiceLane(
    call as unknown as CallSession,
    steps.transcribe ?? (async () => "what is the weather"),
    steps.answer ?? (async () => "It is raining."),
    steps.synthesize ?? (async () => Buffer.concat([LOUD, LOUD, LOUD])),
    { segmenter: steps.segmenter, onTurn: steps.onTurn },
  );
}

async function utterance(
  voice: VoiceLane,
  loudFrames = 6,
  quietFrames = 45,
): Promise<void> {
  for (let i = 0; i < loudFrames; i += 1) await voice.feed(LOUD);
  for (let i = 0; i < quietFrames; i += 1) await voice.feed(QUIET);
}

const rest = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

describe("the voice lane", () => {
  it("turns one utterance into one spoken answer", async () => {
    const call = new FakeCall();
    const turns: VoiceTurn[] = [];
    const voice = lane(call, { onTurn: (t) => turns.push(t) });
    await utterance(voice);
    await voice.turn;

    expect(turns).toEqual([
      {
        heard: "what is the weather",
        said: "It is raining.",
        interrupted: false,
      },
    ]);
    expect(call.sent.length).toBeGreaterThan(0);
    await voice.aclose();
  });

  it("does not wake the agent for a cough", async () => {
    // The segmenter opens on any loud frame, and waking the agent for every one
    // of them is a bill and a caller being answered at random.
    const call = new FakeCall();
    const asked: string[] = [];
    const voice = lane(call, {
      transcribe: async () => "   ",
      answer: async (text) => {
        asked.push(text);
        return "should never be said";
      },
    });
    await utterance(voice);
    await voice.turn;

    expect(asked).toEqual([]);
    expect(call.sent).toEqual([]);
    await voice.aclose();
  });

  it("tells the caller what went wrong rather than going silent", async () => {
    // Somebody on a phone call cannot tell a broken transcriber from an agent
    // that is thinking, and will keep waiting.
    const call = new FakeCall();
    const said: string[] = [];
    const synthesize = async (text: string): Promise<Buffer> => {
      said.push(text);
      return LOUD;
    };

    const deaf = lane(call, {
      transcribe: async () => {
        throw new Error("the transcriber is down");
      },
      synthesize,
    });
    await utterance(deaf);
    await deaf.turn;
    expect(said).toEqual([TROUBLE_HEARING]);
    await deaf.aclose();

    said.length = 0;
    const mute = lane(call, {
      answer: async () => {
        throw new Error("the model is down");
      },
      synthesize,
    });
    await utterance(mute);
    await mute.turn;
    expect(said).toEqual([TROUBLE_ANSWERING]);
    await mute.aclose();
  });

  it("stops the answer when the interruption starts, not when it ends", async () => {
    // The extra second of talking at somebody who has stopped listening is the
    // whole difference between a call that feels alive and one that does not.
    const call = new FakeCall();
    const long = Buffer.concat(Array.from({ length: 200 }, () => LOUD)); // four seconds
    const voice = lane(call, { synthesize: async () => long });
    await utterance(voice);
    await rest(50);
    expect(voice.speaking).toBe(true);

    for (let i = 0; i < 4; i += 1) await voice.feed(LOUD);
    expect(call.cancelled).toBe(1);

    const sent = call.sent.length;
    await rest(100);
    expect(voice.speaking).toBe(false);
    expect(call.sent.length - sent).toBeLessThanOrEqual(1);
    await voice.aclose();
  });

  it("supersedes the first question rather than racing it", async () => {
    // An agent asked two questions at once answers neither well, and both
    // answers would be spoken over each other.
    const call = new FakeCall();
    const answered: string[] = [];
    const spoken: string[] = [];
    const heard = ["first question", "second question"];

    const voice = lane(call, {
      transcribe: async () => heard.shift() ?? "second question",
      answer: async (text) => {
        answered.push(text);
        await rest(200);
        return `answer to ${text}`;
      },
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
    });
    await utterance(voice);
    await rest(20);
    await utterance(voice);
    await rest(300);

    expect(answered).toEqual(["first question", "second question"]);
    // Both were asked; only the newer one was ever spoken.
    expect(spoken).toEqual(["answer to second question"]);
    await voice.aclose();
  });

  it("speaks a streamed answer as it is written", async () => {
    const call = new FakeCall();
    const spoken: string[] = [];
    const turns: VoiceTurn[] = [];
    const voice = lane(call, {
      answer: async function* () {
        yield "It is raining.";
        yield "Take a coat.";
      },
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
      onTurn: (t) => turns.push(t),
    });
    await utterance(voice);
    await voice.turn;

    // The caller hears the beginning of a long answer while the rest is still
    // being written.
    expect(spoken).toEqual(["It is raining.", "Take a coat."]);
    expect(turns[0]!.said).toBe("It is raining. Take a coat.");
    await voice.aclose();
  });

  it("puts a line nobody asked for through the same lane", async () => {
    const call = new FakeCall();
    const spoken: string[] = [];
    const voice = lane(call, {
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
    });
    const turn = await voice.say("Thanks for taking the call.");
    expect(turn.said).toBe("Thanks for taking the call.");
    expect(spoken).toEqual(["Thanks for taking the call."]);
    await voice.aclose();
  });

  it("drops a turn in flight at teardown", async () => {
    const call = new FakeCall();
    const spoken: string[] = [];
    const voice = lane(call, {
      answer: async () => {
        await rest(80);
        return "too late";
      },
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
    });
    await utterance(voice);
    await rest(20);
    expect(voice.busy).toBe(true);
    await voice.aclose();
    await rest(120);
    // Nothing the retired turn produced ever reached the caller.
    expect(spoken).toEqual([]);
    // And feeding a closed lane is a no-op rather than an error.
    await voice.feed(LOUD);
  });

  it("does not let a superseded turn apologise over the one that replaced it", async () => {
    // Its own failure belongs to a question nobody is waiting on any more.
    const call = new FakeCall();
    const spoken: string[] = [];
    const heard = ["first question", "second question"];
    const voice = lane(call, {
      transcribe: async () => heard.shift() ?? "second question",
      answer: async (text) => {
        if (text === "first question") {
          await rest(150);
          throw new Error("the model gave up on the old question");
        }
        return "the newer answer";
      },
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
    });
    await utterance(voice);
    await rest(20);
    await utterance(voice);
    await rest(250);
    expect(spoken).toEqual(["the newer answer"]);
    await voice.aclose();
  });

  it("refuses a line handed in after teardown", async () => {
    // It would otherwise synthesize and send on a call that has already gone.
    const call = new FakeCall();
    const spoken: string[] = [];
    const voice = lane(call, {
      synthesize: async (text) => {
        spoken.push(text);
        return LOUD;
      },
    });
    await voice.aclose();
    const turn = await voice.say("Are you still there?");
    expect(turn.said).toBe("");
    expect(turn.error).toBe("the call has ended");
    expect(spoken).toEqual([]);
  });

  it("lets the plugin tune the segmenter", async () => {
    const call = new FakeCall();
    const voice = lane(call, {
      segmenter: new UtteranceSegmenter({ silenceMs: 200 }),
    });
    await utterance(voice, 6, 12);
    await voice.turn;
    expect(call.sent.length).toBeGreaterThan(0);
    await voice.aclose();
  });
});
