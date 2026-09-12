// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What arrived before your agent was ready to hear it.
 *
 * A call starts the moment StandIn dials. Your agent starts a little later: a
 * socket has to open, a room has to be joined, a session has to be configured.
 * Everything the caller does in that gap still arrives, and if nothing holds it,
 * it is gone.
 *
 * Two things arrive in that gap and both matter:
 *
 * **The caller's first words.** People start talking the instant the call
 * connects, and often the first thing they say is the reason they called. Drop
 * it and the agent opens by asking a question that was already answered.
 *
 * **The first context.** The "there are three people here, stay quiet unless
 * addressed" sentence and the recording-status change both land within the first
 * moment of a meeting. Drop those and a group-call gate never engages and a
 * recording gate never opens.
 *
 * This exists because every plugin was solving half of it. Of the nine plugins
 * in this SDK, two held both, and five held one and silently lost the other.
 *
 * Bounded on purpose. A socket that never opens must not grow a buffer for the
 * length of the call, so the oldest entries are dropped rather than the newest.
 *
 * Identical in shape to the Python SDK's `standin.startup`.
 */

/**
 * About four seconds of speech at the wire's frame size. Enough to hold an
 * opening sentence, far short of enough to hide a socket that never opened.
 */
export const MAX_PENDING_AUDIO = 200;

/** Context sentences are rare and each one is small. This is generous. */
export const MAX_PENDING_CONTEXT = 20;

/**
 * Holds caller audio and call context until the agent can take them.
 *
 * Order is preserved within each lane, and audio is released before context,
 * because the provider needs the caller's words in the order they were said and
 * the context is a note about the call rather than part of the conversation.
 */
export class StartupBuffer {
  readonly #maxAudio: number;
  readonly #maxContext: number;
  #audio: Buffer[] = [];
  #context: string[] = [];
  #holding = true;
  #droppedAudio = 0;
  #droppedContext = 0;

  constructor(maxAudio = MAX_PENDING_AUDIO, maxContext = MAX_PENDING_CONTEXT) {
    this.#maxAudio = Math.max(1, maxAudio);
    this.#maxContext = Math.max(1, maxContext);
  }

  /** Whether the agent is still being set up. */
  get holding(): boolean {
    return this.#holding;
  }

  /**
   * How much was lost to the bounds.
   *
   * Worth logging when it is not zero: it means the agent took long enough to
   * start that the caller outran it.
   */
  get dropped(): { audio: number; context: number } {
    return { audio: this.#droppedAudio, context: this.#droppedContext };
  }

  /** Hold one frame of the caller's voice. */
  audio(pcm: Buffer): void {
    if (pcm.length === 0) return;
    this.#audio.push(pcm);
    if (this.#audio.length > this.#maxAudio) {
      this.#audio.shift();
      this.#droppedAudio += 1;
    }
  }

  /** Hold one line of call context. */
  context(text: string): void {
    if (!text) return;
    this.#context.push(text);
    if (this.#context.length > this.#maxContext) {
      this.#context.shift();
      this.#droppedContext += 1;
    }
  }

  /**
   * Hand everything over, in order, and stop holding.
   *
   * Both callbacks may be sync or async, because a provider's send is often
   * fire-and-forget. Safe to call twice: the second call releases nothing.
   */
  async release(
    sendAudio?: (pcm: Buffer) => void | Promise<void>,
    sendContext?: (text: string) => void | Promise<void>,
  ): Promise<{ audio: number; context: number }> {
    this.#holding = false;
    const audio = this.#audio.splice(0);
    const context = this.#context.splice(0);
    for (const frame of audio) await sendAudio?.(frame);
    for (const line of context) await sendContext?.(line);
    return { audio: audio.length, context: context.length };
  }

  /** Throw it away. For a call that ended before the agent was ready. */
  discard(): void {
    this.#holding = false;
    this.#audio = [];
    this.#context = [];
  }
}
