// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Getting a line of text to a person, whether or not they are already on a call.
 *
 * Something outside the call wants to reach somebody: a scheduled job, a chat
 * message, a host handing off a task. There are two ways that can land, and
 * which one is right depends entirely on whether a call to that person is up
 * right now.
 *
 * If one is, the line should be spoken into it. Ringing somebody who is mid
 * sentence with you is the rudest possible way to tell them something.
 *
 * If one is not, the call has to be placed, and the line parked so it is said
 * the moment they answer. `outbound.ts` already has every piece of that: who
 * may be rung, how often, and the durable parking. What it has never had is a
 * way to reach a call that is ALREADY up, so every plugin that wanted this
 * wrote its own registry, and the ones that exist disagree with each other
 * about identity and about what to do when the live path fails.
 *
 * Nothing in this module imports a provider or a host.
 *
 * Identical in shape to the Python SDK's `standin.delivery`.
 */

import { logger } from "./log.js";
import {
  OutboundError,
  OutboundPolicy,
  PendingMessages,
  type OutboundCaller,
  type PendingMessage,
} from "./outbound.js";

/** Which organisation a placed call belongs to. Operator configuration only. */
export const TENANT_ENV = "STANDIN_TENANT_ID";

/**
 * Something that can say a line on a call that is already up.
 *
 * Implemented by the plugin, because only it knows how to make its provider
 * speak without stepping on whatever the agent was already saying.
 */
export interface LiveSpeaker {
  say(text: string): Promise<void>;
}

/**
 * Which calls are up, and how to speak into them.
 *
 * A call is filed under its own id and, when it has one, under the Microsoft
 * Teams conversation it belongs to. Both, because a delivery addressed by
 * conversation would otherwise miss a call that is only filed by call id, and
 * the agent would place a second call to somebody already on the line with it.
 */
export class LiveCalls {
  readonly #speakers = new Map<string, LiveSpeaker>();

  register(speaker: LiveSpeaker, callId: string, threadId = ""): void {
    if (callId.trim()) this.#speakers.set(callId.trim(), speaker);
    if (threadId.trim()) this.#speakers.set(threadId.trim(), speaker);
  }

  /**
   * Remove this speaker's keys, and only this speaker's.
   *
   * The identity check is the point. A second call on the same thread can start
   * before the first one's teardown runs, and a blind delete would then wipe
   * the live call's entry: every later delivery for that thread would ring a
   * fresh call, and the person would hear a second ring instead of an answer.
   */
  unregister(speaker: LiveSpeaker, callId: string, threadId = ""): void {
    for (const key of [callId.trim(), threadId.trim()]) {
      if (key && this.#speakers.get(key) === speaker)
        this.#speakers.delete(key);
    }
  }

  /** The first live call any of these keys names. Conversation first. */
  find(...keys: string[]): LiveSpeaker | undefined {
    for (const key of keys) {
      const found = key ? this.#speakers.get(key.trim()) : undefined;
      if (found !== undefined) return found;
    }
    return undefined;
  }

  get size(): number {
    return this.#speakers.size;
  }
}

/** What happened to one line of text. */
export interface Delivery {
  readonly ok: boolean;
  /**
   * `"live-call"` when it was spoken into a call that was already up, or
   * `"call-back"` when a call was placed and the line parked for the answer.
   * Both strings are part of this API.
   */
  readonly mode?: "live-call" | "call-back";
  /** The call that was placed, on the call-back path. */
  readonly callId?: string;
  /** One sentence, safe to read out loud. Absent when ok. */
  readonly error?: string;
}

/** Options for {@link VoiceDelivery}. */
export interface VoiceDeliveryOptions {
  caller?: OutboundCaller;
  policy?: OutboundPolicy;
  pending?: PendingMessages;
  /**
   * The organisation to place calls into.
   *
   * Operator configuration only: this option, falling back to
   * `STANDIN_TENANT_ID`. Never the message, the metadata, the model or the
   * caller, because a model steered by whoever is talking must not be able to
   * choose which organisation gets dialled.
   */
  tenantId?: string;
  /** Directory id recorded against a parked message, for the audit trail. */
  requestedBy?: string;
}

/**
 * Speak into a live call if there is one, otherwise ring back.
 *
 * {@link deliver} never throws. Every refusal comes back as a sentence, because
 * the thing reading the result is either a host that will mark the whole
 * platform failed on an exception, or a model that will say it out loud.
 */
export class VoiceDelivery {
  readonly #live: LiveCalls;
  readonly #caller: OutboundCaller | undefined;
  readonly #policy: OutboundPolicy;
  readonly #pending: PendingMessages | undefined;
  readonly #tenantId: string;
  readonly #requestedBy: string;

  constructor(live: LiveCalls, options: VoiceDeliveryOptions = {}) {
    this.#live = live;
    this.#caller = options.caller;
    this.#policy = options.policy ?? new OutboundPolicy({});
    this.#pending = options.pending;
    this.#tenantId = (options.tenantId ?? process.env[TENANT_ENV] ?? "").trim();
    this.#requestedBy = options.requestedBy ?? "";
  }

  /** Say this line to that person, by whichever lane can reach them. */
  async deliver(text: string, target = "", threadId = ""): Promise<Delivery> {
    const line = (text ?? "").trim();
    if (line === "") {
      // Checked before the registry is even consulted: an empty message must
      // never place a real phone call to a real person.
      return { ok: false, error: "there was nothing to say" };
    }

    const speaker = this.#live.find(threadId, target);
    if (speaker !== undefined) {
      try {
        await speaker.say(line);
        return { ok: true, mode: "live-call" };
      } catch (err) {
        // Falls through to the call-back rather than stopping here. A wedged
        // provider socket would otherwise swallow the message with nobody
        // told. The trade is deliberate: a half-spoken line can be repeated by
        // the call-back, and a repeat beats silence.
        logger.warn(
          `standin: could not speak into the live call, ringing back instead: ${String(err)}`,
        );
      }
    }

    return await this.#callBack(line, target, threadId);
  }

  async #callBack(
    line: string,
    target: string,
    threadId: string,
  ): Promise<Delivery> {
    if (this.#caller === undefined) {
      return {
        ok: false,
        error: "no outbound caller is configured on this deployment",
      };
    }
    const who = (target ?? "").trim();
    try {
      this.#policy.check(who);
    } catch (err) {
      if (err instanceof OutboundError)
        return { ok: false, error: err.message };
      throw err;
    }
    if (this.#tenantId === "") {
      return {
        ok: false,
        error: `no tenant is configured: set ${TENANT_ENV} to place calls`,
      };
    }

    let callId = "";
    try {
      const placed = await this.#caller.placeCall({
        userObjectId: who,
        tenantId: this.#tenantId,
      });
      callId = (placed.callId ?? "").trim();
    } catch (err) {
      return { ok: false, error: `could not place the call: ${String(err)}` };
    }
    if (callId === "") return { ok: false, error: "the call was not placed" };

    // Recorded only now. Counting the attempt instead would let a broken worker
    // burn the hourly budget on calls that never rang anybody, and the next
    // real delivery would be refused for an hour because of it.
    this.#policy.record();

    // Parked before this returns. The answering leg is a different call and can
    // be answered before a later park would have run, and then the person picks
    // up to silence.
    const parked: PendingMessage = {
      callId,
      text: line,
      threadId: threadId.trim(),
      requestedBy: this.#requestedBy,
      tenantId: this.#tenantId,
      target: who,
    };
    try {
      // Guarded because parking writes to disk, and a full or read-only one
      // would otherwise throw out of a method whose whole contract is that it
      // does not. The call HAS been placed by now, so the honest answer names
      // what was lost rather than pretending nothing rang.
      this.#pending?.park(parked);
    } catch (err) {
      logger.warn(
        `standin: the call was placed but the line was not parked: ${String(err)}`,
      );
      return {
        ok: false,
        mode: "call-back",
        callId,
        error: "the call was placed, but what to say could not be saved",
      };
    }
    return { ok: true, mode: "call-back", callId };
  }
}
