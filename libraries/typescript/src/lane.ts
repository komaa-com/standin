// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Caller audio in, a spoken answer out, for agents that are not speech to speech.
 *
 * A speech-to-speech model hears the caller and talks back, and a plugin for one
 * is mostly a socket. Most agents are not that. They read text, they write text,
 * and getting them onto a phone call means four things in a row: work out where
 * the caller stopped talking, turn that into words, ask the agent, and say the
 * answer back at the rate a call consumes audio.
 *
 * `voice.ts` has each of those pieces. This is the thing that runs them in
 * order, holds the turn together, and gets the awkward parts right:
 *
 * **One turn at a time.** An agent asked two questions at once answers neither
 * well. A new utterance supersedes the one in flight rather than racing it.
 *
 * **Barge-in actually stops the answer.** Somebody who interrupts has stopped
 * listening, and a worker that keeps streaming a paragraph at them is talking to
 * nobody. The buffered audio is dropped at the same moment the new utterance
 * opens, not when the old one finishes.
 *
 * **A silence is not a turn.** A cough, a door, a second of traffic: the
 * segmenter opens on any loud frame, and waking the agent for every one of them
 * is a bill and a caller being answered at random.
 *
 * **Nothing here throws into the call.** A provider that fails says so, in a
 * sentence, out loud. Silence is the one thing a caller cannot interpret.
 *
 * Identical in shape to the Python SDK's `standin.lane`.
 */

import type { CallSession } from "./handler.js";
import { logger } from "./log.js";
import { PacedPlayback, UtteranceSegmenter } from "./voice.js";

/**
 * What the caller hears when a step of the lane fails.
 *
 * Spoken, not logged and swallowed. Somebody on a phone call cannot tell a
 * broken transcriber from an agent that is thinking, and will keep waiting.
 */
export const TROUBLE_HEARING = "Sorry, I did not catch that.";
export const TROUBLE_ANSWERING =
  "Sorry, I am having trouble answering just now.";
export const TROUBLE_SPEAKING = "Sorry, I am having trouble speaking just now.";

/** Caller audio (PCM16 mono, 16 kHz) to words. Empty means nothing was said. */
export type Transcribe = (pcm: Buffer) => Promise<string>;

/** Words to an answer. Either the whole thing, or sentences as they are written. */
export type Answer = (text: string) => Promise<string> | AsyncIterable<string>;

/** An answer to speech (PCM16 mono, 16 kHz). Either one buffer, or chunks. */
export type Synthesize = (
  text: string,
) => Promise<Buffer> | AsyncIterable<Buffer>;

/** One exchange, after it is over. */
export interface VoiceTurn {
  readonly heard: string;
  readonly said: string;
  /**
   * Whether the caller cut the answer short. Not a failure: it is the most
   * common way a real conversation goes.
   */
  readonly interrupted: boolean;
  readonly error?: string;
}

/** Options for {@link VoiceLane}. */
export interface VoiceLaneOptions {
  segmenter?: UtteranceSegmenter;
  onTurn?: (turn: VoiceTurn) => void;
}

/**
 * Runs one call's worth of listen, transcribe, answer, speak.
 *
 * Built by a plugin, which supplies the three steps. Everything about pacing,
 * interruption and turn-taking is here, because getting those wrong is what
 * makes a working provider sound broken.
 */
export class VoiceLane {
  readonly #session: CallSession;
  readonly #transcribe: Transcribe;
  readonly #answer: Answer;
  readonly #synthesize: Synthesize;
  readonly #segmenter: UtteranceSegmenter;
  readonly #onTurn: ((turn: VoiceTurn) => void) | undefined;
  readonly #playback: PacedPlayback;
  #turn: Promise<void> | undefined;
  #generation = 0;
  #closed = false;

  constructor(
    session: CallSession,
    transcribe: Transcribe,
    answer: Answer,
    synthesize: Synthesize,
    options: VoiceLaneOptions = {},
  ) {
    this.#session = session;
    this.#transcribe = transcribe;
    this.#answer = answer;
    this.#synthesize = synthesize;
    this.#segmenter = options.segmenter ?? new UtteranceSegmenter();
    this.#onTurn = options.onTurn;
    this.#playback = new PacedPlayback((pcm) => session.sendAudio(pcm));
  }

  /** Whether the agent is talking right now. */
  get speaking(): boolean {
    return this.#playback.playing;
  }

  /** Whether a turn is in flight, including the model's own thinking. */
  get busy(): boolean {
    return this.#turn !== undefined;
  }

  /** The turn in flight, if any. Await it to let one finish. */
  get turn(): Promise<void> | undefined {
    return this.#turn;
  }

  /** Take one frame of caller audio. Never throws, never blocks. */
  async feed(pcm: Buffer): Promise<void> {
    if (this.#closed) return;
    const wasSpeaking = this.#segmenter.speaking;
    const utterance = this.#segmenter.feed(pcm);
    if (!wasSpeaking && this.#segmenter.speaking && this.#playback.playing) {
      // The caller started over the top of the answer. Drop what is buffered
      // NOW rather than when this utterance finishes: the extra second of
      // talking at somebody who has stopped listening is the whole difference
      // between a call that feels alive and one that does not.
      await this.bargeIn();
    }
    if (utterance !== undefined) this.#begin(utterance);
  }

  /** Stop talking, immediately. The caller interrupted. */
  async bargeIn(): Promise<void> {
    this.#playback.cancel();
    try {
      await this.#session.cancelPlayback();
    } catch {
      // Best effort. A failed cancel is not worth ending a call over.
    }
  }

  /**
   * Speak a line the agent did not have to be asked for.
   *
   * A greeting, a handover, something that arrived from outside the call.
   */
  async say(text: string): Promise<VoiceTurn> {
    // Same guard as feed(). A line handed in after teardown would otherwise
    // synthesize and send on a call that has already gone.
    if (this.#closed)
      return {
        heard: "",
        said: "",
        interrupted: false,
        error: "the call has ended",
      };
    return await this.#speak(text, "");
  }

  /** Stop everything. Called once, on teardown. */
  async aclose(): Promise<void> {
    this.#closed = true;
    this.#generation += 1;
    this.#playback.cancel();
    this.#segmenter.reset();
    const turn = this.#turn;
    this.#turn = undefined;
    if (turn !== undefined) await turn.catch(() => undefined);
  }

  // ---- one turn ---------------------------------------------------------

  /**
   * Start a turn, superseding whatever was in flight.
   *
   * Detached on purpose: this is reached from the receive path of a live call,
   * and awaiting a model there stops frames arriving.
   */
  #begin(utterance: Buffer): void {
    // One turn at a time. An agent asked two questions at once answers neither
    // well, and both answers would be spoken over each other. JavaScript cannot
    // cancel a promise, so the older turn is retired by generation: it runs to
    // completion but nothing it produces is ever spoken.
    const generation = ++this.#generation;
    this.#playback.cancel();
    const running = this.#run(utterance, generation).finally(() => {
      if (this.#turn === running) this.#turn = undefined;
    });
    this.#turn = running;
  }

  async #run(utterance: Buffer, generation: number): Promise<void> {
    try {
      const heard = await this.#hear(utterance, generation);
      if (heard === undefined || generation !== this.#generation) return;
      await this.#respond(heard, generation);
    } catch (err) {
      logger.warn(`standin: the voice turn failed: ${String(err)}`);
    }
  }

  async #hear(
    utterance: Buffer,
    generation: number,
  ): Promise<string | undefined> {
    let heard: string;
    try {
      heard = ((await this.#transcribe(utterance)) ?? "").trim();
    } catch (err) {
      logger.warn(`standin: could not transcribe the caller: ${String(err)}`);
      if (generation === this.#generation) {
        await this.#speak(TROUBLE_HEARING, "", String(err));
      }
      return undefined;
    }
    if (heard === "") {
      // A cough, a door, a second of traffic. The segmenter opens on any loud
      // frame, and waking the agent for every one of them is a bill and a
      // caller being answered at random.
      logger.debug("standin: an utterance transcribed to nothing; no turn");
      return undefined;
    }
    return heard;
  }

  async #respond(heard: string, generation: number): Promise<void> {
    let reply: Promise<string> | AsyncIterable<string>;
    try {
      reply = this.#answer(heard);
    } catch (err) {
      logger.warn(`standin: the agent did not answer: ${String(err)}`);
      // Gated, like every other thing this turn might say. A turn superseded
      // while its answer was pending would otherwise apologise over the top of
      // the turn that replaced it.
      if (generation === this.#generation) {
        await this.#speak(TROUBLE_ANSWERING, heard, String(err));
      }
      return;
    }

    if (isAsyncIterable(reply)) {
      // Sentence by sentence, so the caller hears the beginning of a long
      // answer while the rest is still being written.
      const spoken: string[] = [];
      let interrupted = false;
      try {
        for await (const piece of reply) {
          if (generation !== this.#generation) return;
          if (piece.trim() === "") continue;
          const turn = await this.#speak(piece, heard);
          spoken.push(turn.said);
          if (turn.interrupted) {
            interrupted = true;
            break;
          }
        }
      } catch (err) {
        logger.warn(`standin: the agent did not answer: ${String(err)}`);
        if (generation === this.#generation) {
          await this.#speak(TROUBLE_ANSWERING, heard, String(err));
        }
        return;
      }
      if (spoken.length > 0)
        this.#finished({ heard, said: spoken.join(" "), interrupted });
      return;
    }

    let said: string;
    try {
      said = ((await reply) ?? "").trim();
    } catch (err) {
      logger.warn(`standin: the agent did not answer: ${String(err)}`);
      if (generation === this.#generation) {
        await this.#speak(TROUBLE_ANSWERING, heard, String(err));
      }
      return;
    }
    if (said === "") {
      logger.debug("standin: the agent answered with nothing; staying quiet");
      return;
    }
    if (generation !== this.#generation) return;
    this.#finished(await this.#speak(said, heard));
  }

  /** Say one piece of an answer, and report what the caller heard. */
  async #speak(
    text: string,
    heard: string,
    error?: string,
  ): Promise<VoiceTurn> {
    const line = (text ?? "").trim();
    if (line === "") return { heard, said: "", interrupted: false, error };
    try {
      const audio = this.#synthesize(line);
      if (isAsyncIterable(audio)) {
        let interrupted = false;
        for await (const chunk of audio) {
          const played = await this.#playback.say(chunk);
          if (played.interrupted) {
            interrupted = true;
            break;
          }
        }
        return { heard, said: line, interrupted, error };
      }
      const played = await this.#playback.say(await audio);
      return { heard, said: line, interrupted: played.interrupted, error };
    } catch (err) {
      logger.warn(`standin: could not speak: ${String(err)}`);
      if (line !== TROUBLE_SPEAKING) {
        // One retry, with the sentence that says what happened. Without it a
        // synthesis failure is indistinguishable from a dropped call, and the
        // caller waits for an answer that is not coming.
        return await this.#speak(TROUBLE_SPEAKING, heard, String(err));
      }
      return { heard, said: "", interrupted: false, error: String(err) };
    }
  }

  #finished(turn: VoiceTurn): void {
    if (this.#onTurn === undefined) return;
    try {
      this.#onTurn(turn);
    } catch {
      // A plugin's own bookkeeping must not end a call.
    }
  }
}

function isAsyncIterable<T>(value: unknown): value is AsyncIterable<T> {
  return (
    typeof value === "object" && value !== null && Symbol.asyncIterator in value
  );
}
