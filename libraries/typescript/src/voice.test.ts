// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Turn-taking for an agent that is not a realtime model.
 *
 * Three pieces, and every bound in them exists because of a specific failure: an
 * utterance clipped at the front, a turn that ends on a thinking pause, a cough
 * sent for transcription, a television that never goes quiet, a WAV that plays
 * as noise, a barge-in that arrives after the bot has already said the rest.
 *
 * The Python twin is `tests/test_voice.py`.
 */

import { describe, expect, it } from "vitest";

import { FRAME_MS } from "./audio.js";
import {
  PacedPlayback,
  UtteranceSegmenter,
  decodeWav,
  encodeWav,
  type SegmenterOptions,
} from "./voice.js";

/** One 20 ms frame of each kind. */
const LOUD = (() => {
  const b = Buffer.alloc(640);
  for (let i = 0; i < 320; i += 1) b.writeInt16LE(8000, i * 2);
  return b;
})();
const QUIET = Buffer.alloc(640);

function segmenter(options: SegmenterOptions = {}): UtteranceSegmenter {
  return new UtteranceSegmenter({
    silenceMs: 100,
    minUtteranceMs: 40,
    prerollMs: 40,
    ...options,
  });
}

function drive(seg: UtteranceSegmenter, frames: Buffer[]): Buffer[] {
  const out: Buffer[] = [];
  for (const frame of frames) {
    const got = seg.feed(frame);
    if (got !== undefined) out.push(got);
  }
  return out;
}

function repeat(frame: Buffer, times: number): Buffer[] {
  return Array.from({ length: times }, () => frame);
}

/** Build a WAV by hand, in the shapes a real speech engine emits. */
function wav(
  format: number,
  channels: number,
  rate: number,
  bits: number,
  payload: Buffer,
  extensible = false,
): Buffer {
  const fmtSize = extensible ? 40 : 16;
  const body = Buffer.alloc(fmtSize);
  body.writeUInt16LE(extensible ? 0xfffe : format, 0);
  body.writeUInt16LE(channels, 2);
  body.writeUInt32LE(rate, 4);
  body.writeUInt32LE((rate * channels * bits) / 8, 8);
  body.writeUInt16LE((channels * bits) / 8, 12);
  body.writeUInt16LE(bits, 14);
  if (extensible) {
    // cbSize, valid bits, channel mask, then the sub-format GUID whose first two
    // bytes carry the real format tag.
    body.writeUInt16LE(22, 16);
    body.writeUInt16LE(bits, 18);
    body.writeUInt32LE(3, 20);
    body.writeUInt16LE(format, 24);
  }
  const head = Buffer.alloc(12);
  head.write("RIFF", 0, "ascii");
  head.writeUInt32LE(4 + 8 + fmtSize + 8 + payload.length, 4);
  head.write("WAVE", 8, "ascii");
  const fmtHeader = Buffer.alloc(8);
  fmtHeader.write("fmt ", 0, "ascii");
  fmtHeader.writeUInt32LE(fmtSize, 4);
  const dataHeader = Buffer.alloc(8);
  dataHeader.write("data", 0, "ascii");
  dataHeader.writeUInt32LE(payload.length, 4);
  return Buffer.concat([head, fmtHeader, body, dataHeader, payload]);
}

describe("segmenting a continuous stream", () => {
  it("yields one utterance per spoken phrase", () => {
    expect(
      drive(segmenter(), [
        ...repeat(QUIET, 3),
        ...repeat(LOUD, 10),
        ...repeat(QUIET, 6),
      ]),
    ).toHaveLength(1);
  });

  it("does not clip the syllable that opened the gate", () => {
    // Without pre-roll every utterance begins mid-consonant, and the transcript
    // loses the first word of most sentences.
    const got = drive(segmenter({ prerollMs: 60 }), [
      ...repeat(QUIET, 5),
      ...repeat(LOUD, 10),
      ...repeat(QUIET, 6),
    ]);
    expect(got[0]!.length).toBeGreaterThan(10 * LOUD.length);
  });

  it("does not end a turn on a pause inside a sentence", () => {
    // A caller thinking mid-sentence is not a caller who has finished.
    const frames = [
      ...repeat(LOUD, 5),
      ...repeat(QUIET, 5),
      ...repeat(LOUD, 5),
      ...repeat(QUIET, 12),
    ];
    expect(drive(segmenter({ silenceMs: 200 }), frames)).toHaveLength(1);
  });

  it("does not treat a cough as a turn", () => {
    // Sending one costs a transcription request and returns nothing worth
    // answering.
    expect(
      drive(segmenter({ minUtteranceMs: 200 }), [
        ...repeat(LOUD, 2),
        ...repeat(QUIET, 8),
      ]),
    ).toEqual([]);
  });

  it("cuts a room that never goes quiet at the ceiling", () => {
    // A stuck-open microphone or a television never trips a silence check, and
    // without a cap one utterance grows for the whole call.
    const got = drive(segmenter({ maxUtteranceMs: 200 }), repeat(LOUD, 40));
    expect(got.length).toBeGreaterThan(0);
    expect(got[0]!.length).toBeLessThanOrEqual(
      (200 / FRAME_MS + 2) * LOUD.length,
    );
  });

  it("flushes what is held mid-utterance", () => {
    const seg = segmenter();
    drive(seg, repeat(LOUD, 5));
    expect(seg.speaking).toBe(true);
    expect(seg.flush()).toBeDefined();
    expect(seg.speaking).toBe(false);
    expect(seg.flush()).toBeUndefined();
  });

  it("abandons the turn on reset", () => {
    const seg = segmenter();
    drive(seg, repeat(LOUD, 5));
    seg.reset();
    expect(seg.speaking).toBe(false);
    expect(seg.flush()).toBeUndefined();
  });

  it("ignores an empty frame", () => {
    expect(segmenter().feed(Buffer.alloc(0))).toBeUndefined();
  });
});

describe("reading a WAV a speech engine handed back", () => {
  it("round trips", () => {
    const pcm = Buffer.alloc(3200, 7);
    expect(decodeWav(encodeWav(pcm))).toEqual(pcm);
  });

  it("reads 32-bit float as PCM16", () => {
    // Several engines emit 32-bit float, which plays as noise unconverted.
    const payload = Buffer.alloc(16);
    [0, 0.5, -0.5, 1].forEach((v, i) => payload.writeFloatLE(v, i * 4));
    const pcm = decodeWav(wav(3, 1, 16000, 32, payload));
    expect(pcm.length).toBe(8);
    expect([0, 1, 2, 3].map((i) => pcm.readInt16LE(i * 2))).toEqual([
      0, 16383, -16383, 32767,
    ]);
  });

  it("clamps a float outside the range rather than wrapping it", () => {
    // Wrapping is the loudest possible click, and a float WAV is allowed
    // outside minus one to one.
    const payload = Buffer.alloc(8);
    payload.writeFloatLE(4, 0);
    payload.writeFloatLE(-4, 4);
    const pcm = decodeWav(wav(3, 1, 16000, 32, payload));
    expect([pcm.readInt16LE(0), pcm.readInt16LE(2)]).toEqual([32767, -32767]);
  });

  it("unwraps WAVE_FORMAT_EXTENSIBLE", () => {
    // ffmpeg and several speech engines emit it even for plain PCM, and reading
    // the tag literally rejects a perfectly good file.
    const pcm = Buffer.alloc(200, 3);
    expect(decodeWav(wav(1, 1, 16000, 16, pcm, true))).toEqual(pcm);
  });

  it("averages stereo rather than halving it", () => {
    // Taking only the left channel loses whoever is on the right.
    const payload = Buffer.alloc(8);
    [1000, 3000, -1000, -3000].forEach((v, i) =>
      payload.writeInt16LE(v, i * 2),
    );
    const pcm = decodeWav(wav(1, 2, 16000, 16, payload));
    expect([pcm.readInt16LE(0), pcm.readInt16LE(2)]).toEqual([2000, -2000]);
  });

  it("reads an 8-bit WAV as unsigned", () => {
    // 8-bit WAV is centred on 128. Read as signed it is a square wave of noise.
    const pcm = decodeWav(wav(1, 1, 16000, 8, Buffer.from([128, 255])));
    expect([pcm.readInt16LE(0), pcm.readInt16LE(2)]).toEqual([0, 32512]);
  });

  it("resamples another rate to the call's", () => {
    const pcm = decodeWav(wav(1, 1, 44100, 16, Buffer.alloc(8820, 1)));
    // 4410 samples at 44.1 kHz is 100 ms, which is 1600 samples at 16 kHz.
    expect(Math.abs(pcm.length / 2 - 1600)).toBeLessThanOrEqual(2);
  });

  it("walks the chunks rather than assuming an offset", () => {
    // A real encoder puts LIST and fact chunks first, and a fixed offset reads
    // them as samples.
    const pcm = Buffer.alloc(200, 5);
    const base = encodeWav(pcm);
    const extra = Buffer.alloc(12);
    extra.write("LIST", 0, "ascii");
    extra.writeUInt32LE(4, 4);
    extra.write("INFO", 8, "ascii");
    const out = Buffer.concat([base.subarray(0, 12), extra, base.subarray(12)]);
    out.writeUInt32LE(out.length - 8, 4);
    expect(decodeWav(out)).toEqual(pcm);
  });

  it.each([
    Buffer.alloc(0),
    Buffer.from("not a wav at all"),
    Buffer.concat([Buffer.from("RIFF"), Buffer.alloc(8)]),
  ])("refuses something that is not a WAV", (data) => {
    expect(() => decodeWav(data)).toThrow();
  });

  it("says so for a format it cannot read", () => {
    expect(() => decodeWav(wav(1, 1, 16000, 24, Buffer.alloc(30)))).toThrow(
      /unsupported WAV format/,
    );
  });
});

describe("pacing a finished buffer", () => {
  it("sends one wire frame at a time", async () => {
    const sent: Buffer[] = [];
    // The default frame is the wire frame: 20 ms, 640 bytes. frameMs scales the
    // frame SIZE with it, so a smaller one is a smaller frame, not a faster
    // clock.
    const playback = new PacedPlayback(async (pcm) => void sent.push(pcm));
    const result = await playback.say(Buffer.concat(repeat(QUIET, 5)));
    expect(sent).toHaveLength(5);
    expect(sent.every((f) => f.length === 640)).toBe(true);
    expect(result.interrupted).toBe(false);
    expect(result.sentMs).toBe(result.totalMs);
    expect(result.totalMs).toBe(100);
  });

  it("stops on a barge-in and says how much was heard", async () => {
    // The difference between "I told them" and "I started to".
    const sent: Buffer[] = [];
    let playback!: PacedPlayback;
    playback = new PacedPlayback(async (pcm) => {
      sent.push(pcm);
      if (sent.length === 2) playback.cancel();
    });
    const result = await playback.say(Buffer.concat(repeat(QUIET, 10)));
    expect(result.interrupted).toBe(true);
    expect(result.sentMs).toBeGreaterThan(0);
    expect(result.sentMs).toBeLessThan(result.totalMs);
  });

  it("does not interleave two turns", async () => {
    // Left unserialised the caller hears both at once.
    const order: string[] = [];
    const playback = new PacedPlayback(async (pcm) => {
      order.push(pcm.subarray(0, 1).toString("hex"));
    });
    const first = playback.say(Buffer.alloc(1920, 0xaa));
    const second = playback.say(Buffer.alloc(1920, 0xbb));
    await Promise.all([first, second]);
    expect(order).toEqual(["aa", "aa", "aa", "bb", "bb", "bb"]);
  });

  it("does not let a failed turn poison the next one", async () => {
    let calls = 0;
    const playback = new PacedPlayback(async () => {
      calls += 1;
      if (calls === 1) throw new Error("the socket went away");
    });
    await expect(playback.say(QUIET)).rejects.toThrow("the socket went away");
    expect((await playback.say(QUIET)).interrupted).toBe(false);
  });

  it("treats nothing to say as not an error", async () => {
    const result = await new PacedPlayback(async () => {
      throw new Error("nothing should be sent");
    }).say(Buffer.alloc(0));
    expect(result.totalMs).toBe(0);
    expect(result.interrupted).toBe(false);
  });

  it("scales the frame size with the frame length", async () => {
    // frameMs is the length of a frame, so it scales the frame's SIZE. A plugin
    // whose provider hands back 10 ms chunks gets 320-byte frames.
    const sent: Buffer[] = [];
    await new PacedPlayback(async (pcm) => void sent.push(pcm), 10).say(QUIET);
    expect(sent.map((f) => f.length)).toEqual([320, 320]);
  });
});

describe("what counts as an utterance", () => {
  // The floor judges the LOUD part. Measuring the whole buffer counts the
  // pre-roll and the trailing silence, over a second at the defaults, so the
  // floor could never fire and every click reached the transcriber.
  const FRAME = ((16_000 * 20) / 1000) * 2;
  const loud = Buffer.alloc(FRAME).fill(Buffer.from([0x00, 0x40]));
  const quiet = Buffer.alloc(FRAME);

  function utter(loudFrames: number): Buffer | undefined {
    const segmenter = new UtteranceSegmenter();
    for (let i = 0; i < loudFrames; i += 1) segmenter.feed(loud);
    for (let i = 0; i < 45; i += 1) {
      const got = segmenter.feed(quiet);
      if (got !== undefined) return got;
    }
    return undefined;
  }

  it("drops a click and keeps a short word", () => {
    // One frame of noise: a door, a chair, a tap on the microphone.
    expect(utter(1)).toBeUndefined();
    // Two hundred milliseconds of voice: "yes". It must survive.
    expect(utter(10)).toBeDefined();
  });
});
