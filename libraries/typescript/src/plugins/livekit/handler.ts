// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The relay: one Microsoft Teams call on one side, one LiveKit room on the other.
 *
 * Unlike the provider plugins, there is no model here and no tools to
 * answer. LiveKit is a room your own agent joins, so this side publishes the
 * caller's voice, subscribes to the agent's, and relays call context as room
 * data on two topics the agent can subscribe to.
 *
 * Your agent therefore needs no Microsoft Teams awareness at all. It sees a
 * participant who talks, and optionally two topics that tell it who is calling
 * and when to say goodbye.
 *
 * The Python twin is `standin.plugins.livekit`, which additionally arms
 * itself from inside a LiveKit worker. This one is explicit: you build the
 * handler yourself.
 */

import { GroupGate, isAddressed, type GateDecision } from "../../gate.js";
import type { CallHandler, CallSession } from "../../handler.js";
import { logger } from "../../log.js";
import { StartupBuffer } from "../../startup.js";
import { TileStream } from "../../tile.js";
import { liveKitConfigFromEnv, type LiveKitConfig } from "./config.js";
import {
  TOPIC_CONTEXT,
  TOPIC_GOODBYE,
  contextPayload,
  connectRoom,
  type RoomHandlers,
  type RoomPort,
} from "./room.js";

/** Caller audio that arrives while the room is still being joined. */
const MAX_PENDING_AUDIO = 200;

/** How the handler reaches LiveKit. Replaceable so tests need no framework. */
export type Connect = (
  config: LiveKitConfig,
  callId: string,
  metadata: Record<string, string>,
  handlers: RoomHandlers,
) => Promise<RoomPort>;

/** Options for {@link LiveKitHandler}. */
export interface LiveKitHandlerOptions {
  /**
   * What addresses the assistant in a meeting.
   *
   * Empty by default, which leaves the group gate inert: an agent that answers
   * everything keeps answering everything, and nothing changes for it.
   */
  wakePhrases?: readonly string[];
  config?: LiveKitConfig;
  connect?: Connect;
}

/** One Microsoft Teams call answered by one LiveKit agent. */
export class LiveKitHandler implements CallHandler {
  #config: LiveKitConfig;
  #connect: Connect;
  #call: CallSession | undefined;
  #room: RoomPort | undefined;
  #closed = false;
  // Holds BOTH the caller's first words and the first context, so neither
  // is lost while the provider is still connecting.
  readonly #pending = new StartupBuffer();
  #tile: TileStream | undefined;
  #stopRelay: (() => void) | undefined;
  #gate: GroupGate | undefined;
  #lastDecision: GateDecision | undefined;

  constructor(options: LiveKitHandlerOptions = {}) {
    this.#config = options.config ?? liveKitConfigFromEnv();
    this.#connect = options.connect ?? connectRoom;
    this.#wakePhrases = options.wakePhrases ?? [];
  }

  readonly #wakePhrases: readonly string[];

  /**
   * What the gate decided about the caller's last finished turn.
   *
   * `undefined` until a turn has been decided, and on a call where transcripts
   * never arrive. An agent that wants to stay out of a meeting it was not
   * addressed in reads this; one that does not can ignore it entirely.
   */
  get lastDecision(): GateDecision | undefined {
    return this.#lastDecision;
  }

  async onStart(session: CallSession): Promise<void> {
    this.#call = session;
    // Armed from the call's own thread. On a one-to-one call it is inert, so a
    // plugin that never configures a wake phrase behaves exactly as before.
    this.#gate = new GroupGate({
      wakePhrases: this.#wakePhrases,
      threadId: session.start.threadId ?? "",
    });
    const caller = session.start.caller;
    // Job metadata is how the agent learns who is calling. Absent fields stay
    // absent rather than becoming a shared default, so two anonymous callers
    // never look like the same person.
    //
    // The KEY NAMES are a cross-language contract, not a local choice. The
    // Python SDK's CallInfo.from_job reads exactly these, and returns a blank
    // record for metadata in any other shape, so an agent dispatched from here
    // and written in Python would otherwise see no caller at all.
    const metadata: Record<string, string> = {
      source: "msteams",
      call_id: session.callId,
      call_direction: session.start.direction,
    };
    if (caller.displayName) metadata.caller_name = caller.displayName;
    if (caller.aadId) metadata.user_id = caller.aadId;
    if (caller.tenantId) metadata.tenant_id = caller.tenantId;
    if (session.start.threadId) metadata.thread_id = session.start.threadId;

    let room: RoomPort;
    try {
      room = await this.#connect(this.#config, session.callId, metadata, {
        onAgentAudio: (pcm) => this.#onAgentAudio(pcm),
        onClosed: (reason) => this.#onClosed(reason),
        // The agent's own audio is what "answered" means. Without this the
        // core reaper would end a call an agent HAS taken but that is still
        // listening.
        onAnswered: () => session.markAnswered(),
        onCallerTranscript: (text, final) =>
          this.#onCallerTranscript(text, final),
      });
    } catch (err) {
      logger.error(`standin: could not join the LiveKit room: ${String(err)}`);
      await session.end("agent-unavailable");
      return;
    }

    // The call can end during the join above.
    if (this.#closed) {
      await room.aclose();
      return;
    }
    this.#room = room;
    await this.#pending.release(
      (pcm: Buffer) => room.sendCallerAudio(pcm),
      (text: string) => void room.publish(TOPIC_CONTEXT, contextPayload(text)),
    );
    if (this.#pending.dropped.audio || this.#pending.dropped.context) {
      logger.info(
        "standin: the caller outran the agent starting up; some early input was dropped",
      );
    }

    // The agent's own face on the tile, when it publishes one. On by default:
    // an agent that publishes video almost always means it for the caller.
    if (this.#config.tileVideo !== "off" && room.startAvatarRelay) {
      const tile = new TileStream(session, { fps: this.#config.tileVideoFps });
      await tile.start();
      this.#tile = tile;
      try {
        this.#stopRelay = await room.startAvatarRelay({
          offerRgb: (rgb, width, height) => tile.offerRgb(rgb, width, height),
        });
      } catch (err) {
        // A tile that will not start is not a call that should fail.
        logger.warn(
          `standin: could not start the avatar relay: ${String(err)}`,
        );
      }
    }
  }

  /** The caller's voice, published into the room as a track. */
  async onCallerAudio(pcm: Buffer): Promise<void> {
    const room = this.#room;
    if (room === undefined || !room.isOpen) {
      this.#pending.audio(pcm);
      return;
    }
    room.sendCallerAudio(pcm);
  }

  /**
   * What the caller said, fed to the group gate.
   *
   * A partial may only NOTICE the wake phrase, never decide a turn. Deciding on
   * a partial returns "do not respond" for a turn that is about to address the
   * assistant, and the answer to a turn that DID address it gets cut.
   *
   * The text is never logged or kept: it is read for this decision and dropped.
   */
  #onCallerTranscript(text: string, final: boolean): void {
    const gate = this.#gate;
    if (gate === undefined) return;
    if (!final) {
      // Stamps the window when the phrase is heard mid-turn, and cannot close
      // it: the window is a timestamp, so a missed phrase self-heals by clock
      // rather than stranding the agent silent for the meeting.
      if (isAddressed(text, gate.wakePhrases)) gate.decide(text, Date.now());
      return;
    }
    this.#lastDecision = gate.decide(text, Date.now());
  }

  /** Call context, published on a topic the agent can subscribe to. */
  async onContext(text: string): Promise<void> {
    const room = this.#room;
    if (room === undefined) {
      // Held rather than dropped: a data packet reaches only participants
      // connected at that instant, and the first context lands before the
      // dispatched agent has joined.
      this.#pending.context(text);
      return;
    }
    await room.publish(TOPIC_CONTEXT, contextPayload(text));
  }

  /**
   * The closing line, published on its own topic so an agent can tell it apart
   * from ordinary context and interrupt itself to say it.
   */
  async onGoodbye(text: string): Promise<void> {
    await this.#room?.publish(TOPIC_GOODBYE, contextPayload(text));
  }

  async aclose(): Promise<void> {
    this.#closed = true;
    this.#stopRelay?.();
    this.#stopRelay = undefined;
    const tile = this.#tile;
    this.#tile = undefined;
    if (tile !== undefined) await tile.aclose();
    const room = this.#room;
    this.#room = undefined;
    if (room !== undefined) {
      try {
        await room.aclose();
      } catch {
        // Teardown must not throw: the slot is freed either way.
      }
    }
  }

  async #onAgentAudio(pcm: Buffer): Promise<void> {
    if (pcm.length > 0) await this.#call?.sendAudio(pcm);
  }

  async #onClosed(reason: string): Promise<void> {
    logger.info(`standin: the LiveKit room ended (${reason})`);
    if (!this.#closed) await this.#call?.end("agent-disconnected");
  }
}
