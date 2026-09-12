// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Turn-taking for an agent that is not a realtime model.
 *
 * A realtime speech-to-speech provider is handed the caller's audio and hands
 * audio back, and the turn-taking is theirs. Everything else is not like that. A
 * transcription service wants one utterance at a time, a language model wants
 * text, and a text-to-speech engine hands back a whole buffer that somebody has
 * to feed out at the rate a call consumes it.
 *
 * That shape is the same whichever three services you pick, and four separate
 * bridges each wrote it before this module existed. Three pieces:
 *
 * {@link UtteranceSegmenter} turns fifty frames a second into one utterance per
 * spoken phrase. {@link decodeWav} and {@link encodeWav} deal with the fact that
 * a speech engine hands you a WAV and it is rarely the WAV you wanted.
 * {@link PacedPlayback} feeds a finished buffer out at the rate the call
 * consumes it, interruptibly.
 *
 * None of this is needed by a realtime plugin, and none of it costs one
 * anything: it is a module you do not import.
 *
 * Identical in shape to the Python SDK's `standin.voice`.
 */

import { FRAME_BYTES, FRAME_MS, pcm16Rms, resamplePcm16 } from "./audio.js";
import { logger } from "./log.js";
import { SAMPLE_RATE_HZ } from "./protocol.js";

/**
 * Loudness at which a frame counts as speech rather than room noise. The same
 * scale {@link pcm16Rms} returns, 0.0 to 1.0.
 */
export const DEFAULT_SPEECH_RMS = 0.02;

/**
 * How much quiet ends an utterance. Short enough that the agent does not feel
 * slow, long enough to survive the pause in the middle of a sentence.
 */
export const DEFAULT_SILENCE_MS = 800;

/**
 * Audio kept from BEFORE the gate opened. The syllable that trips the gate is
 * part of the word, and without this every utterance starts clipped.
 */
export const DEFAULT_PREROLL_MS = 240;

/**
 * Below this an utterance is a cough, a door, a chair. Transcribing it costs a
 * request and returns nothing worth answering.
 */
export const DEFAULT_MIN_UTTERANCE_MS = 120;

/**
 * A hard ceiling, so a stuck-open microphone or a television in the room does
 * not grow one utterance for the length of the call.
 */
export const DEFAULT_MAX_UTTERANCE_MS = 30_000;

/** Options for {@link UtteranceSegmenter}. */
export interface SegmenterOptions {
  speechRms?: number;
  silenceMs?: number;
  prerollMs?: number;
  minUtteranceMs?: number;
  maxUtteranceMs?: number;
}

/**
 * One utterance per spoken phrase, out of a continuous stream.
 *
 * A Microsoft Teams call delivers audio continuously: silence is still frames,
 * fifty a second. A transcription service wants a phrase. This is the gate
 * between them.
 *
 * ```ts
 * const utterance = segmenter.feed(frame);
 * if (utterance !== undefined) await transcribe(utterance);
 * ```
 *
 * Four bounds, and each exists because of a specific failure.
 *
 * **Pre-roll**, because the syllable that trips the gate is part of the word.
 * Without it every utterance begins mid-consonant and the transcript loses the
 * first word of most sentences.
 *
 * **Trailing silence**, because a pause inside a sentence is not the end of one.
 * Too short and the agent interrupts a thinking caller; too long and it feels
 * slow.
 *
 * **A floor**, because a cough is not a turn. Sending one costs a request and
 * returns nothing worth answering.
 *
 * **A ceiling**, because a stuck-open microphone or a television in the room
 * never goes quiet, and without a cap one utterance grows for the whole call.
 */
export class UtteranceSegmenter {
  readonly speechRms: number;
  readonly silenceMs: number;
  readonly minUtteranceMs: number;
  readonly maxUtteranceMs: number;
  readonly #prerollFrames: number;
  // A ring, not a growing list: it holds pre-speech audio on every frame of a
  // silent call, which is most of them.
  #preroll: Buffer[] = [];
  #speech: Buffer[] = [];
  #quietMs = 0;
  #speaking = false;
  /**
   * Frames since the gate opened, and the last of them that was loud. The gap
   * between the two is trailing silence, which is not speech.
   */
  #sinceOpen = 0;
  #lastLoud = 0;

  constructor(options: SegmenterOptions = {}) {
    this.speechRms = options.speechRms ?? DEFAULT_SPEECH_RMS;
    this.silenceMs = options.silenceMs ?? DEFAULT_SILENCE_MS;
    this.minUtteranceMs = options.minUtteranceMs ?? DEFAULT_MIN_UTTERANCE_MS;
    this.maxUtteranceMs = options.maxUtteranceMs ?? DEFAULT_MAX_UTTERANCE_MS;
    this.#prerollFrames = Math.max(
      1,
      Math.floor((options.prerollMs ?? DEFAULT_PREROLL_MS) / FRAME_MS),
    );
  }

  /** Whether the caller is mid-utterance right now. */
  get speaking(): boolean {
    return this.#speaking;
  }

  /**
   * Take one frame. Returns a finished utterance, or undefined.
   *
   * Never throws and never blocks: it runs on the receive path of a live call,
   * once per frame.
   */
  feed(pcm: Buffer): Buffer | undefined {
    if (pcm.length === 0) return undefined;
    const loud = pcm16Rms(pcm) >= this.speechRms;

    if (!this.#speaking) {
      if (!loud) {
        this.#preroll.push(pcm);
        if (this.#preroll.length > this.#prerollFrames) this.#preroll.shift();
        return undefined;
      }
      // Opening: the ring is what the caller actually started saying.
      this.#speech = [...this.#preroll, pcm];
      this.#sinceOpen = 1;
      this.#lastLoud = 1;
      this.#preroll = [];
      this.#speaking = true;
      this.#quietMs = 0;
      return undefined;
    }

    this.#speech.push(pcm);
    this.#sinceOpen += 1;
    if (loud) this.#lastLoud = this.#sinceOpen;
    this.#quietMs = loud ? 0 : this.#quietMs + FRAME_MS;

    if (this.#quietMs >= this.silenceMs) return this.#finish(false);
    if (this.#durationMs() >= this.maxUtteranceMs) {
      logger.info(
        `standin: cutting an utterance at its ${this.maxUtteranceMs} ms ceiling`,
      );
      return this.#finish(true);
    }
    return undefined;
  }

  /** Take whatever is held, mid-utterance. For teardown, or a barge-in. */
  flush(): Buffer | undefined {
    return this.#speaking ? this.#finish(false) : undefined;
  }

  /** Forget everything. The caller interrupted, or the turn is abandoned. */
  reset(): void {
    this.#speech = [];
    this.#preroll = [];
    this.#quietMs = 0;
    this.#speaking = false;
    this.#sinceOpen = 0;
    this.#lastLoud = 0;
  }

  /**
   * Everything held, pre-roll and trailing silence included.
   *
   * The right measure for the ceiling: a stuck-open microphone is filling
   * memory whether or not anybody is talking into it.
   */
  #durationMs(): number {
    return this.#speech.length * FRAME_MS;
  }

  /** The loud part alone, which is what a floor has to judge. */
  #voicedMs(): number {
    return this.#lastLoud * FRAME_MS;
  }

  #finish(force: boolean): Buffer | undefined {
    const audio = Buffer.concat(this.#speech);
    const voiced = this.#voicedMs();
    this.reset();
    if (!force && voiced < this.minUtteranceMs) return undefined;
    return audio.length > 0 ? audio : undefined;
  }
}

// -------------------------------------------------------------------- the WAV

/** Plain integer PCM. */
const FORMAT_PCM = 1;
/** IEEE float, which several engines emit at 32 bits. */
const FORMAT_FLOAT = 3;
/**
 * The wrapper ffmpeg and several speech engines emit even for plain PCM. The
 * real format is a GUID in the extension, whose first two bytes are the tag
 * above, so reading those two bytes is enough.
 */
const FORMAT_EXTENSIBLE = 0xfffe;

/**
 * Read a WAV and return PCM16 mono at the call's rate.
 *
 * A speech engine hands you a WAV and it is rarely the one you wanted: 32-bit
 * float, 44.1 kHz, stereo, or wrapped in `WAVE_FORMAT_EXTENSIBLE`, which ffmpeg
 * emits even for plain PCM. Every one of those plays as noise if you put it on a
 * call unconverted, and the failure is not obvious from the header.
 *
 * Chunks are walked rather than assumed at a fixed offset, because a real
 * encoder puts `LIST` and `fact` chunks before the data and a fixed offset reads
 * them as samples.
 *
 * Throws on anything that is not a WAV this can read.
 */
export function decodeWav(data: Buffer): Buffer {
  if (
    data.length < 12 ||
    data.toString("ascii", 0, 4) !== "RIFF" ||
    data.toString("ascii", 8, 12) !== "WAVE"
  ) {
    throw new Error("not a RIFF/WAVE file");
  }

  let audioFormat = 0;
  let channels = 0;
  let rate = 0;
  let bits = 0;
  let payload: Buffer | undefined;
  let at = 12;
  while (at + 8 <= data.length) {
    const chunkId = data.toString("ascii", at, at + 4);
    const size = data.readUInt32LE(at + 4);
    const body = data.subarray(at + 8, at + 8 + size);
    if (chunkId === "fmt " && body.length >= 16) {
      audioFormat = body.readUInt16LE(0);
      channels = body.readUInt16LE(2);
      rate = body.readUInt32LE(4);
      bits = body.readUInt16LE(14);
      if (audioFormat === FORMAT_EXTENSIBLE && body.length >= 26) {
        // The real format is the first two bytes of the sub-format GUID.
        audioFormat = body.readUInt16LE(24);
      }
    } else if (chunkId === "data") {
      payload = body;
    }
    // Chunks are word-aligned, and an odd size carries a pad byte.
    at += 8 + size + (size & 1);
  }

  if (channels === 0 || rate === 0)
    throw new Error("the WAV has no readable fmt chunk");
  if (payload === undefined) throw new Error("the WAV has no data chunk");

  let samples: Buffer;
  if (audioFormat === FORMAT_FLOAT && bits === 32) {
    samples = float32ToPcm16(payload);
  } else if (audioFormat === FORMAT_PCM && bits === 16) {
    samples = payload;
  } else if (audioFormat === FORMAT_PCM && bits === 8) {
    // 8-bit WAV is UNSIGNED, centred on 128. Read as signed it is a square wave
    // of noise.
    samples = Buffer.alloc(payload.length * 2);
    for (let i = 0; i < payload.length; i += 1)
      samples.writeInt16LE((payload[i]! - 128) * 256, i * 2);
  } else {
    throw new Error(`unsupported WAV format ${audioFormat} at ${bits} bits`);
  }

  if (channels > 1) samples = downmix(samples, channels);
  if (rate !== SAMPLE_RATE_HZ)
    samples = resamplePcm16(samples, rate, SAMPLE_RATE_HZ);
  return samples;
}

/** Wrap PCM16 mono in a WAV, for a service that will not take raw PCM. */
export function encodeWav(
  pcm: Buffer,
  sampleRateHz: number = SAMPLE_RATE_HZ,
): Buffer {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0, "ascii");
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write("WAVE", 8, "ascii");
  header.write("fmt ", 12, "ascii");
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(FORMAT_PCM, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(sampleRateHz, 24);
  header.writeUInt32LE(sampleRateHz * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36, "ascii");
  header.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([header, pcm]);
}

function float32ToPcm16(payload: Buffer): Buffer {
  const count = Math.floor(payload.length / 4);
  const out = Buffer.alloc(count * 2);
  for (let i = 0; i < count; i += 1) {
    const value = payload.readFloatLE(i * 4);
    // Clamped, because a float WAV is allowed outside -1..1 and wrapping there
    // is the loudest possible click.
    const clamped = value < -1 ? -1 : value > 1 ? 1 : value;
    out.writeInt16LE(Math.trunc(clamped * 32767), i * 2);
  }
  return out;
}

/** Average the channels. Taking only the left loses whoever is on the right. */
function downmix(pcm: Buffer, channels: number): Buffer {
  const frame = channels * 2;
  const count = Math.floor(pcm.length / frame);
  const out = Buffer.alloc(count * 2);
  for (let i = 0; i < count; i += 1) {
    let total = 0;
    for (let c = 0; c < channels; c += 1)
      total += pcm.readInt16LE(i * frame + c * 2);
    out.writeInt16LE(Math.trunc(total / channels), i * 2);
  }
  return out;
}

// --------------------------------------------------------------- the playback

/** What happened to one buffer handed to {@link PacedPlayback}. */
export interface Playback {
  /** How much actually reached the caller. */
  readonly sentMs: number;
  /** How much there was. */
  readonly totalMs: number;
  /** Whether it was cut short. */
  readonly interrupted: boolean;
}

/** Hand one wire frame to the call. Normally `session.sendAudio`. */
export type FrameSink = (pcm: Buffer) => Promise<void>;

/**
 * Feed a finished buffer out at the rate a call consumes it.
 *
 * A text-to-speech engine returns a whole utterance at once. A call takes 20
 * milliseconds every 20 milliseconds. Sending the buffer in one go hands the
 * service seconds of audio it must queue, and the queue is what makes a barge-in
 * arrive too late to matter: the caller interrupts, the model stops, and the bot
 * keeps talking for the length of what was already sent.
 *
 * So it goes out paced, and the pacing is on an ABSOLUTE clock. Sleeping 20 ms
 * per frame accumulates every scheduling delay, and a minute of speech ends
 * seconds behind where it should be; this one waits until the next frame is DUE,
 * so a late frame is followed by a short wait rather than a full one.
 *
 * ```ts
 * const playback = new PacedPlayback((pcm) => session.sendAudio(pcm));
 * const result = await playback.say(pcm);
 * if (result.interrupted) { ... }
 * ```
 *
 * {@link cancel} stops whatever is playing. The result says how much was heard,
 * which is the difference between "I told them" and "I started to".
 */
export class PacedPlayback {
  readonly #send: FrameSink;
  readonly #frameMs: number;
  readonly #frameBytes: number;
  #cancelled = false;
  #playing = false;
  // Serialised, so two turns cannot interleave into one stream of audio the
  // caller hears as both at once.
  #queue: Promise<unknown> = Promise.resolve();

  constructor(send: FrameSink, frameMs: number = FRAME_MS) {
    this.#send = send;
    this.#frameMs = Math.max(1, frameMs);
    this.#frameBytes = (FRAME_BYTES * this.#frameMs) / FRAME_MS;
  }

  get playing(): boolean {
    return this.#playing;
  }

  /** Stop what is playing. Safe from anywhere, including a receive loop. */
  cancel(): void {
    this.#cancelled = true;
  }

  /** Play one buffer and return what the caller actually heard. */
  async say(pcm: Buffer): Promise<Playback> {
    const totalMs = Math.floor(
      (Math.floor(pcm.length / 2) * 1000) / SAMPLE_RATE_HZ,
    );
    if (pcm.length === 0) return { sentMs: 0, totalMs: 0, interrupted: false };

    const run = this.#queue.then(() => this.#play(pcm, totalMs));
    // Swallowed on the QUEUE only: the caller still sees the rejection through
    // their own await, but one failed turn must not poison every later one.
    this.#queue = run.catch(() => undefined);
    return run;
  }

  async #play(pcm: Buffer, totalMs: number): Promise<Playback> {
    this.#cancelled = false;
    this.#playing = true;
    let sent = 0;
    // The clock is absolute. Waiting a fixed step per frame accumulates every
    // delay, and a long utterance finishes seconds late.
    let due = Date.now();
    try {
      for (let at = 0; at < pcm.length; at += this.#frameBytes) {
        if (this.#cancelled) break;
        const frame = pcm.subarray(at, at + this.#frameBytes);
        await this.#send(frame);
        sent += frame.length;
        due += this.#frameMs;
        const delay = due - Date.now();
        if (delay > 0) {
          await new Promise((resolve) => {
            const timer = setTimeout(resolve, delay);
            timer.unref?.();
          });
        }
      }
    } finally {
      this.#playing = false;
    }
    const interrupted = this.#cancelled;
    this.#cancelled = false;
    return {
      sentMs: Math.floor((Math.floor(sent / 2) * 1000) / SAMPLE_RATE_HZ),
      totalMs,
      interrupted,
    };
  }
}
