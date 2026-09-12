// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The seam every StandIn plugin implements.
 *
 * This is the whole contract between the SDK and an agent framework, and it is
 * deliberately five methods wide. {@link CallServer} owns everything that is the
 * same for every framework - the socket StandIn dials, the HMAC handshake and
 * its replay guard, capacity and draining, the frame loop, sequence numbers and
 * the outbound audio timeline, and the two watchdogs that end a call nobody
 * closed. A plugin owns only the part that differs: what to do with a caller's
 * voice, and where the reply comes from.
 *
 * Identical in shape to the Python SDK's `standin.handler`, translated to TS
 * naming (snake_case becomes camelCase). A plugin ported between the two
 * changes only its method names.
 *
 * Writing one:
 *
 * ```ts
 * import { CallServer, type CallSession } from "@komaa/standin-sdk";
 *
 * class EchoHandler {
 *   #call!: CallSession;
 *   async onStart(session: CallSession) { this.#call = session; }
 *   async onCallerAudio(pcm: Buffer) { await this.#call.sendAudio(pcm); }
 * }
 *
 * const server = new CallServer({ handlerFactory: () => new EchoHandler() });
 * await server.start();
 * ```
 *
 * Every method is optional: the server treats a missing one as a no-op, so a
 * handler that only wants audio implements only `onCallerAudio`. Nothing extends
 * anything.
 */

import type { Emotion, SpeechMark } from "./avatar.js";
import type { SessionStart } from "./protocol.js";
import type { DisplayImageOptions, VideoFrame, VideoSource } from "./vision.js";

/**
 * One live Microsoft Teams call, as a handler sees it.
 *
 * Handed to {@link CallHandler.onStart} and valid until the call ends. The
 * server owns the socket; this is the handler's only way to reach it.
 */
export interface CallSession {
  /**
   * StandIn's id for this call. Authenticated - it is the value the handshake
   * HMAC signed, not something a caller supplied.
   */
  readonly callId: string;

  /**
   * The `session.start` that opened the call: caller identity, direction,
   * thread, recording status.
   */
  readonly start: SessionStart;

  /**
   * Send the agent's voice to the caller.
   *
   * `pcm` is raw PCM16, 16 kHz, mono, little-endian - the same format
   * {@link CallHandler.onCallerAudio} receives. The server owns the sequence
   * number and the outbound timeline, so a handler never tracks either, and a
   * re-published or swapped audio source cannot make timestamps jump backwards.
   */
  /**
   * Whether the Microsoft Teams call is being recorded, right now.
   *
   * One flag, kept current by the server: it starts from
   * `session.start.recordingStatus` and follows every later `recording.status`
   * change, so a plugin never has to re-derive it from the context sentence it
   * happens to have seen.
   *
   * A reported status WINS over the start snapshot, whichever arrives first.
   * `session.start` omits the field when the state was unknown at answer time,
   * and an omitted field is not "not recording": letting the snapshot overwrite
   * a `recording.status` that landed first would shut every recording-gated
   * capability for the whole call, silently.
   *
   * Gate on it before anything that STORES what the caller said or showed with
   * a third party. A recorded call is one the caller was told is being kept; an
   * unrecorded one is not.
   */
  readonly recordingActive: boolean;

  /**
   * Who is speaking now, when StandIn sends unmixed audio.
   *
   * `undefined` on the mixed path, which is most calls, so treat it as a hint
   * rather than as something to depend on. Use it to attribute a transcript, or
   * to tell a model who it is answering in a meeting.
   */
  readonly speaker: string | undefined;

  /**
   * How many people are on the call.
   *
   * Zero until StandIn first says, and it says again whenever somebody joins or
   * leaves. The number behind the "this is a 1:1 call" and "there are N human
   * participants" context sentences, for a plugin that wants to branch on it
   * rather than hand a sentence to a model.
   */
  readonly participantCount: number;

  /** Whether anything has actually taken this call yet. */
  readonly answered: boolean;

  /**
   * Say that an agent has taken the call. Stamped once, never re-stamped.
   *
   * A plugin that joins a room calls this when the agent's own audio track
   * appears, not when a participant connects: monitors, recorders and avatar
   * workers all connect, and none of them is an agent answering. A plugin that
   * never calls it is still covered, because sending audio counts.
   */
  markAnswered(): void;

  /**
   * How much outbound data the socket has not yet flushed.
   *
   * The number a continuous sender watches to decide whether to drop a frame.
   * Audio and the avatar tile both push on a timer, and a peer that stops
   * reading turns "send everything" into an unbounded buffer.
   *
   * Zero when the transport cannot report it, so treat a zero as "no evidence
   * of backpressure" rather than as proof of an idle socket.
   */
  readonly bufferedBytes: number;

  /**
   * The outbound audio timeline, in milliseconds.
   *
   * The same clock this call's `audio.frame` messages are stamped with, which is
   * what a video frame must be stamped with too. A wall clock keeps ticking
   * through listening silence while this one does not, so stamping video from a
   * wall clock makes the audio and video drift apart on paper even when they are
   * in step.
   */
  readonly mediaTimeMs: number;

  sendAudio(pcm: Buffer): Promise<void>;

  /**
   * Drop whatever agent audio StandIn still has buffered.
   *
   * The only lever that un-sends audio already handed to the service: it
   * flushes the platform player, so the caller stops hearing the turn they just
   * interrupted. Call it the moment your provider reports the caller started
   * speaking, before you cancel the response upstream - otherwise a barge-in
   * stops the model but the bot keeps talking for the length of the buffer.
   */
  cancelPlayback(): Promise<void>;

  /**
   * The most recent frame the caller showed, or `undefined` if they have shown
   * nothing.
   *
   * Synchronous, because it reads a value the frame loop already stored: there
   * is no waiting for a frame here, and a handler that wants to know the moment
   * one arrives implements {@link CallHandler.onVideoFrame} instead.
   *
   * With no `source` the screen share wins over the camera, because an agent
   * asked to look is nearly always being asked about what is being shown rather
   * than who is showing it.
   */
  latestVideoFrame(source?: VideoSource): VideoFrame | undefined;

  /**
   * Draw an image on the bot's video tile for a few seconds.
   *
   * The agent's half of the vision lane: a chart it just computed, a page it is
   * quoting, a photo it was asked for. Best-effort and additive, so a service
   * that does not implement it ignores the message rather than failing the call.
   */
  displayImage(
    image: Buffer | string,
    options?: DisplayImageOptions,
  ): Promise<void>;

  /**
   * Put one frame of continuous video on the bot's tile.
   *
   * The server owns the sequence number and stamps the frame with the outbound
   * AUDIO timeline, exactly as it does for {@link sendAudio}, so the two streams
   * cannot disagree about what time it is.
   *
   * Latest wins and there is no handshake: the first frames start the stream and
   * silence ends it. Pace it and drop under backpressure rather than queueing,
   * which is what `TileStream` is for.
   */
  sendTileFrame(jpeg: Buffer, width?: number, height?: number): Promise<void>;

  /**
   * Hint the emotion the avatar should wear on the bot's tile.
   *
   * Best-effort and video only: an unknown emotion renders as neutral, and
   * nothing here changes a sample of what the caller hears.
   */
  express(emotion: Emotion): Promise<void>;

  /**
   * Send the viseme timeline for one utterance, which is what drives lip-sync
   * on the avatar.
   *
   * Real timings from your provider are best. Where there are none, which is
   * every realtime speech-to-speech model, `lipsync.ts` estimates a timeline
   * from the text and spreads it over the audio that turn actually sent: a
   * mouth on a measured clock beats a still one. What is worse than none is a
   * timeline whose DURATION was guessed, from text length or a words-per-minute
   * rate, because that one drifts further out of step with the voice the longer
   * it runs.
   */
  sendSpeechMarks(marks: Iterable<SpeechMark>): Promise<void>;

  /**
   * Ask for the call to end. Idempotent, and the first reason wins - a cascade
   * of close causes must not overwrite the one that actually ended it.
   *
   * Safe to call from inside {@link CallHandler.onStart} to refuse a call: it
   * returns immediately there rather than deadlocking against teardown, and the
   * close runs once onStart unwinds.
   */
  end(reason: string): Promise<void>;
}

/**
 * What a plugin implements. One instance per call, built by a
 * {@link HandlerFactory}.
 *
 * Every method is awaited by the server and every one is optional. A rejection
 * from any of them is logged and ends that call alone - one bad call must never
 * take the worker with it.
 */
export interface CallHandler {
  /**
   * The call is live. Join a room, open a realtime socket, build an agent -
   * whatever this framework needs. Audio does not flow until this resolves, so a
   * slow start delays the caller rather than dropping frames.
   */
  onStart?(session: CallSession): void | Promise<void>;

  /**
   * One frame of the caller's voice: PCM16, 16 kHz, mono, little-endian.
   *
   * Called on the receive path of a live call, so it must not block. The frame
   * has already been validated - a truncated or malformed payload is dropped by
   * the server and never reaches here.
   */
  onCallerAudio?(pcm: Buffer): void | Promise<void>;

  /**
   * One sampled frame of the caller's camera or screen share.
   *
   * Optional, and most handlers never implement it: frames arrive sparsely and
   * best-effort, and the common shape is to look only when the model asks, which
   * {@link CallSession.latestVideoFrame} already serves without a callback.
   * Implement this for ambient vision - narrating a slide deck, watching a
   * whiteboard, noticing that the share stopped.
   *
   * Called on the receive path of a live call, so a slow model call belongs off
   * the frame loop, exactly as it does in {@link CallHandler.onCallerAudio}.
   */
  onVideoFrame?(frame: VideoFrame): void | Promise<void>;

  /**
   * A different person started speaking.
   *
   * Optional, and only ever called when StandIn sends unmixed audio: most calls
   * carry mixed audio and never call it at all. Called on CHANGE only, never
   * per frame. The name rides every inbound audio frame, and a model told forty
   * times a second who is speaking would hear nothing else.
   *
   * Called on the receive path of a live call, so anything slow belongs off the
   * frame loop, exactly as in {@link CallHandler.onCallerAudio}.
   */
  onSpeakerChange?(name: string): void | Promise<void>;

  /**
   * Non-interrupting context about the call, as a plain sentence ready to put in
   * front of a model: participant counts and group-call etiquette, DTMF key
   * presses, and recording status changes.
   *
   * Delivered as it arrives. A framework that cannot accept context before its
   * agent is ready should queue it here - the server does not, because what
   * "ready" means is a framework's own business.
   */
  onContext?(text: string): void | Promise<void>;

  /**
   * StandIn is ending the call and wants this line spoken first.
   *
   * The server has already told StandIn to drop whatever agent audio it had
   * buffered, so this line plays immediately. Interrupt the current turn and say
   * it: the teardown follows shortly, and a goodbye queued behind a long answer
   * is a goodbye the caller never hears.
   */
  onGoodbye?(text: string): void | Promise<void>;

  /**
   * Release everything this call holds. Always called exactly once, on every
   * path, before the slot is freed.
   */
  aclose?(reason: string): void | Promise<void>;
}

/**
 * Builds one {@link CallHandler} per call. Called with no arguments, so a plugin
 * closes over its own configuration rather than threading it through the server.
 */
export type HandlerFactory = () => CallHandler;
