// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * One LiveKit room per call, joined from your worker.
 *
 * The shape is the opposite of the other plugins. The providers are a
 * socket you send audio to; LiveKit is a room you JOIN, publishing the caller's
 * voice as a track and subscribing to the agent's. Your agent does not know it
 * is on a Microsoft Teams call at all: it sees a participant who talks.
 *
 * The framework is imported lazily, inside the function that needs it, which is
 * the rule that lets one package ship every plugin. `import { CallServer }
 * from "@komaa/standin-sdk"` must keep working on a machine with no LiveKit
 * installed, so nothing here is imported at module load.
 *
 * The Python twin is `standin.plugins.livekit.call`, and the two data
 * topics below are deliberately the same strings in both languages: an agent
 * written for one works with the other.
 */

import { StandInError } from "../../errors.js";
import { logger } from "../../log.js";
import { NUM_CHANNELS, SAMPLE_RATE_HZ } from "../../protocol.js";
import type { LiveKitConfig } from "./config.js";

/** Non-interrupting call context, published into the room as data. */
export const TOPIC_CONTEXT = "msteams.context";

/** The closing line StandIn wants spoken before the call ends. */
export const TOPIC_GOODBYE = "msteams.goodbye";

/** LiveKit's own transcription topic. Both sides of the call publish on it. */
export const TOPIC_TRANSCRIPTION = "lk.transcription";

/** Which track a transcript stream is OF. Absent on some publishers. */
export const ATTRIBUTE_TRANSCRIBED_TRACK_ID = "lk.transcribed_track_id";

/** Marks the closing header of a finished turn. */
export const ATTRIBUTE_TRANSCRIPTION_FINAL = "lk.transcription_final";

/** The identity this plugin joins under. */
export const LOCAL_IDENTITY = "standin-msteams";

/** What a LiveKit text stream gives us, narrowed to what is read here. */
export interface TextStreamReader {
  readonly info?: { readonly attributes?: Record<string, string> };
  [Symbol.asyncIterator](): AsyncIterator<string>;
}

/** How a transcript stream is classified and delivered. */
export interface TranscriptRoute {
  callerTrackSid: () => string | undefined;
  localIdentity: string;
  agentIdentity: () => string | undefined;
  isClosed: () => boolean;
  deliver: (text: string, final: boolean) => void | Promise<void>;
}

/**
 * Whether this transcript is of the CALLER rather than of the agent.
 *
 * The track id is exact when present. Without it, the sender identity is the
 * fallback: livekit publishes a participant's transcript under that
 * participant's identity, and ours is the caller's audio.
 */
export function isCallerTranscript(
  attributes: Record<string, string> | undefined,
  senderIdentity: string | undefined,
  route: {
    callerTrackSid?: string;
    localIdentity: string;
    agentIdentity?: string;
  },
): boolean {
  const trackId = attributes?.[ATTRIBUTE_TRANSCRIBED_TRACK_ID];
  if (trackId !== undefined && trackId !== "") {
    return (
      route.callerTrackSid !== undefined && trackId === route.callerTrackSid
    );
  }
  if (senderIdentity === undefined) return false;
  if (senderIdentity === route.localIdentity) return true;
  // Once the agent is bound, anything that is not the agent is the caller.
  return (
    route.agentIdentity !== undefined && senderIdentity !== route.agentIdentity
  );
}

/**
 * Read one transcript stream to its end and report the caller's words.
 *
 * Every stream is drained, including one this call ignores: an abandoned reader
 * keeps its subscription for the life of the process, and the agent publishes
 * one stream per turn.
 *
 * Each yield is the cumulative transcript so far, so every yield is delivered as
 * a partial and the last one as final. A partial exists so a wake phrase is
 * noticed as it is said; only a finished turn is a turn.
 */
export async function readTranscript(
  reader: TextStreamReader,
  senderIdentity: string | undefined,
  route: TranscriptRoute,
): Promise<void> {
  const mine = isCallerTranscript(reader.info?.attributes, senderIdentity, {
    callerTrackSid: route.callerTrackSid(),
    localIdentity: route.localIdentity,
    agentIdentity: route.agentIdentity(),
  });
  let latest = "";
  try {
    for await (const chunk of reader) {
      if (route.isClosed()) return;
      latest = chunk;
      // Drained either way, but only the caller's words are reported.
      if (mine && latest !== "") await route.deliver(latest, false);
    }
  } catch (err) {
    logger.debug(`standin: a transcript stream ended early: ${String(err)}`);
    return;
  }
  if (mine && latest !== "" && !route.isClosed())
    await route.deliver(latest, true);
}

/**
 * What goes on a topic: a JSON object carrying `text`, never the bare string.
 *
 * This is a cross-language contract. The Python SDK's `TeamsCall._on_data`
 * json-decodes the packet and requires a dict with a `text` string, so a bare
 * string is dropped in silence at the far end: every context sentence and the
 * goodbye would reach nothing.
 */
export function contextPayload(text: string): string {
  return JSON.stringify({ text });
}

/** How long a room token lives. Longer than any call, shorter than forever. */
const TOKEN_TTL = "6h";

/** A sink the avatar relay pushes encoded frames into. */
export interface TileSink {
  offerRgb(rgb: Buffer, width: number, height: number): void;
}

/** What the handler needs from a joined room. Tests substitute their own. */
export interface RoomPort {
  /**
   * Start relaying the agent's own avatar video onto the bot's tile.
   *
   * Optional, and absent on a room that has no video to relay. It returns a
   * stop function rather than taking a lifetime, because a track swap must
   * replace the drain rather than stack a second one on top of it.
   */
  startAvatarRelay?(sink: TileSink): Promise<() => void>;
  readonly isOpen: boolean;
  /** The identity of the participant whose audio is relayed to the caller. */
  readonly agentIdentity: string | undefined;
  /** Publish one frame of the caller's voice into the room. */
  sendCallerAudio(pcm: Buffer): void;
  /**
   * Publish one payload for the agent to read, verbatim.
   *
   * The caller builds the payload. Its SHAPE is a cross-language contract, not
   * a transport detail: see {@link contextPayload}.
   */
  publish(topic: string, payload: string): Promise<void>;
  aclose(): Promise<void>;
}

/** What a joined room tells the handler. */
export interface RoomHandlers {
  /** Agent audio at the wire rate, ready for the caller. */
  onAgentAudio: (pcm: Buffer) => void | Promise<void>;
  /** The room or the agent is gone, with the reason. */
  onClosed: (reason: string) => void | Promise<void>;
  /**
   * What the caller said, as it is said.
   *
   * Called with the transcript so far and whether the turn has finished. A
   * partial exists so a wake phrase is noticed as it is spoken; only a finished
   * turn is a turn, and a partial must never be able to close the follow-up
   * window.
   *
   * The text is for deciding whether to answer. It is never logged or stored.
   */
  onCallerTranscript?: (text: string, final: boolean) => void | Promise<void>;

  /**
   * An agent has actually taken the call.
   *
   * Fired on the agent's first AUDIO track, not on a participant connecting:
   * monitors, recorders and avatar workers all connect, and none of them is an
   * agent answering.
   */
  onAnswered?: () => void;
}

/**
 * Load the framework at USE time, never at module load.
 *
 * A missing framework must name the install that fixes it. A reader who gets
 * `Cannot find module '@livekit/rtc-node'` out of somebody else's package
 * cannot tell a missing optional dependency from a broken install.
 */
async function loadLiveKit(): Promise<{
  rtc: typeof import("@livekit/rtc-node");
  server: typeof import("livekit-server-sdk");
}> {
  try {
    const [rtc, server] = await Promise.all([
      import("@livekit/rtc-node"),
      import("livekit-server-sdk"),
    ]);
    return { rtc, server };
  } catch (err) {
    logger.debug(`standin: loading LiveKit failed: ${String(err)}`);
    throw new StandInError(
      "the livekit plugin needs @livekit/rtc-node and livekit-server-sdk: " +
        "run `npm install @livekit/rtc-node livekit-server-sdk`",
    );
  }
}

/**
 * Join a room for one call, publishing the caller and subscribing to the agent.
 *
 * `metadata` is handed to the dispatched agent as job metadata, which is how an
 * agent learns who is calling without this plugin inventing a side channel.
 */
/** The control-plane form of a room URL. The websocket one is not an API base. */
function httpUrl(url: string): string {
  const trimmed = url.trim();
  if (trimmed.startsWith("wss://")) return `https://${trimmed.slice(6)}`;
  if (trimmed.startsWith("ws://")) return `http://${trimmed.slice(5)}`;
  return trimmed;
}

export async function connectRoom(
  config: LiveKitConfig,
  callId: string,
  metadata: Record<string, string>,
  handlers: RoomHandlers,
): Promise<RoomPort> {
  const { rtc, server } = await loadLiveKit();

  // The call id arrives from a decoded URL segment, so a "%2F" would smuggle a
  // slash into the room name. Keep it to a safe charset and a bounded length.
  const safeCallId = callId.replace(/[^A-Za-z0-9._@:-]/g, "-");
  const roomName = `${config.roomPrefix}${safeCallId}`.slice(0, 100);

  const token = new server.AccessToken(config.apiKey, config.apiSecret, {
    identity: "standin-msteams",
    ttl: TOKEN_TTL,
  });
  token.addGrant({
    roomJoin: true,
    room: roomName,
    canPublish: true,
    canSubscribe: true,
    canPublishData: true,
  });
  if (config.agentName) {
    token.roomConfig = new server.RoomConfiguration({
      agents: [
        new server.RoomAgentDispatch({
          agentName: config.agentName,
          metadata: JSON.stringify(metadata),
        }),
      ],
    });
  }

  const room = new rtc.Room();
  await room.connect(config.url, await token.toJwt(), {
    autoSubscribe: true,
    dynacast: false,
  });
  const local = room.localParticipant;
  if (!local) {
    try {
      await room.disconnect();
    } catch {
      // already closing
    }
    throw new StandInError("the room connected without a local participant");
  }
  logger.info(
    `standin: joined LiveKit room "${roomName}"` +
      (config.agentName ? ` and dispatched "${config.agentName}"` : ""),
  );

  const source = new rtc.AudioSource(SAMPLE_RATE_HZ, NUM_CHANNELS);
  const track = rtc.LocalAudioTrack.createAudioTrack("msteams-caller", source);
  // The publication is KEPT, and its sid is read at use time. The SDK re-issues
  // the sid in place after a reconnect, so a cached string stops matching and
  // every caller transcript is then classified as the agent's own speech, which
  // takes the wake-phrase path silently dead for the rest of the call.
  const callerPublication = await local.publishTrack(
    track,
    new rtc.TrackPublishOptions({ source: rtc.TrackSource.SOURCE_MICROPHONE }),
  );

  let closed = false;
  // The identity whose audio we relay is "the agent". Captured on the first
  // audio subscription, and only THAT identity leaving ends the call: a monitor
  // or a second participant dropping out must not hang up on the caller.
  let agentIdentity: string | undefined;
  // Any value other than auto or off names the participant whose video the tile
  // relay should take, rather than letting the room decide.
  const pinnedIdentity =
    config.tileVideo === "auto" || config.tileVideo === "off"
      ? undefined
      : config.tileVideo;
  // One live pump at a time, reset when the stream ends, so an agent that
  // republishes its audio (an avatar track swap, a mute cycle) gets pumped
  // again instead of going silent for the rest of the call.
  let activePumpSid: string | undefined;

  const startPump = (
    remote: import("@livekit/rtc-node").RemoteTrack,
    identity: string,
  ): void => {
    if (activePumpSid !== undefined) return;
    activePumpSid = remote.sid ?? "unknown";
    void (async () => {
      try {
        // Ask for 16 kHz mono: the framework resamples, so our side stays a copy.
        const stream = new rtc.AudioStream(
          remote,
          SAMPLE_RATE_HZ,
          NUM_CHANNELS,
        );
        for await (const frame of stream) {
          if (closed) break;
          const pcm = Buffer.from(
            frame.data.buffer,
            frame.data.byteOffset,
            frame.data.length * 2,
          );
          await handlers.onAgentAudio(Buffer.from(pcm));
        }
      } catch (err) {
        if (!closed)
          logger.warn(`standin: the LiveKit audio pump failed: ${String(err)}`);
      } finally {
        activePumpSid = undefined;
        logger.debug(`standin: the audio pump for "${identity}" ended`);
      }
    })();
  };

  if (handlers.onCallerTranscript !== undefined) {
    // LiveKit publishes transcripts of BOTH sides on this one topic. Telling
    // them apart matters: an agent greeting that says its own name would
    // otherwise open the follow-up window and make the assistant answer the
    // next turn of a meeting nobody addressed it in.
    try {
      room.registerTextStreamHandler(
        TOPIC_TRANSCRIPTION,
        (reader: TextStreamReader, info: { identity?: string }) => {
          void readTranscript(reader, info?.identity, {
            callerTrackSid: () => callerPublication?.sid,
            localIdentity: LOCAL_IDENTITY,
            agentIdentity: () => agentIdentity,
            isClosed: () => closed,
            deliver: handlers.onCallerTranscript!,
          });
        },
      );
    } catch (err) {
      logger.warn(
        `standin: caller transcripts are unavailable on this room: ${String(err)}`,
      );
    }
  }

  room.on(
    rtc.RoomEvent.TrackSubscribed,
    (remote, _publication, participant) => {
      if (remote.kind !== rtc.TrackKind.KIND_AUDIO) return;
      if (agentIdentity === undefined) {
        agentIdentity = participant.identity;
        handlers.onAnswered?.();
      }
      startPump(remote, participant.identity);
    },
  );
  room.on(rtc.RoomEvent.TrackUnsubscribed, (remote) => {
    if (remote.sid !== undefined && remote.sid === activePumpSid)
      activePumpSid = undefined;
  });
  room.on(rtc.RoomEvent.ParticipantDisconnected, (participant) => {
    if (agentIdentity !== undefined && participant.identity === agentIdentity) {
      void handlers.onClosed(`the agent ${participant.identity} disconnected`);
    }
  });
  // Disconnected is FINAL: the framework retries transient drops internally
  // before this fires.
  room.on(rtc.RoomEvent.Disconnected, () => {
    void handlers.onClosed("the room disconnected");
  });

  /**
   * Relay the avatar's video onto the bot's tile.
   *
   * The participant is chosen by LiveKit's own publish-on-behalf attribute
   * first, because an avatar worker publishes video as a SEPARATE participant
   * acting for the agent, and only then by the agent's own identity.
   *
   * Tracks are matched by KIND, never by source: an avatar worker publishes its
   * video untagged, so it arrives as an unknown source rather than a camera. A
   * source filter would pick the right participant and then stream nothing,
   * which is the most expensive way to get this wrong.
   */
  const startAvatarRelay = async (sink: TileSink): Promise<() => void> => {
    let stopped = false;
    let activeSid: string | undefined;
    let activeReader:
      | ReadableStreamDefaultReader<import("@livekit/rtc-node").VideoFrameEvent>
      | undefined;

    const cancelActive = (): void => {
      const reader = activeReader;
      activeReader = undefined;
      activeSid = undefined;
      // Cancel through the LOCK HOLDER. A VideoStream is a ReadableStream, the
      // drain loop's reader locks it, and cancelling the stream itself rejects
      // while it is locked. Cancelling the reader also unblocks a parked read,
      // which a quiet or swapped track would otherwise never wake.
      if (reader) void reader.cancel().catch(() => undefined);
    };

    const drain = (
      track: import("@livekit/rtc-node").RemoteTrack,
      identity: string,
    ): void => {
      cancelActive();
      const sid = track.sid ?? "unknown";
      activeSid = sid;
      logger.info(`standin: relaying avatar video from "${identity}"`);
      void (async () => {
        const stream = new rtc.VideoStream(track);
        const reader = stream.getReader();
        activeReader = reader;
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done || stopped) break;
            const rgb = value.frame.convert(rtc.VideoBufferType.RGB24);
            sink.offerRgb(
              Buffer.from(
                rgb.data.buffer,
                rgb.data.byteOffset,
                rgb.data.length,
              ),
              rgb.width,
              rgb.height,
            );
          }
        } catch (err) {
          if (!stopped)
            logger.warn(
              `standin: the avatar video stream ended: ${String(err)}`,
            );
        } finally {
          reader.releaseLock();
          if (activeReader === reader) activeReader = undefined;
          if (activeSid === sid) activeSid = undefined;
        }
      })();
    };

    const pickParticipant = ():
      import("@livekit/rtc-node").RemoteParticipant | undefined => {
      const remotes = [...room.remoteParticipants.values()];
      // A pinned identity wins outright. It is set precisely when a separate
      // worker publishes the avatar and publish-on-behalf is NOT, in which case
      // guessing relays whichever participant published first.
      if (pinnedIdentity !== undefined) {
        return remotes.find((p) => p.identity === pinnedIdentity);
      }
      const behalf = remotes.find(
        (p) => p.attributes?.["lk.publish_on_behalf"] === agentIdentity,
      );
      if (behalf) return behalf;
      return remotes.find((p) => p.identity === agentIdentity);
    };

    const startFromExisting = (): void => {
      if (stopped || activeSid !== undefined) return;
      const chosen = pickParticipant();
      if (!chosen) return;
      for (const pub of chosen.trackPublications.values()) {
        if (pub.kind === rtc.TrackKind.KIND_VIDEO && pub.track) {
          drain(
            pub.track as import("@livekit/rtc-node").RemoteTrack,
            chosen.identity,
          );
          return;
        }
      }
    };

    const onSubscribed = (
      track: import("@livekit/rtc-node").RemoteTrack,
      _pub: unknown,
      participant: import("@livekit/rtc-node").RemoteParticipant,
    ): void => {
      if (stopped || activeSid !== undefined) return;
      if (track.kind === rtc.TrackKind.KIND_AUDIO) {
        // The audio subscribe is what binds the agent identity. The chosen
        // participant's video may already be subscribed, and that event will
        // not fire again, so re-scan here or the relay never starts.
        startFromExisting();
        return;
      }
      if (track.kind !== rtc.TrackKind.KIND_VIDEO) return;
      const chosen = pickParticipant();
      if (!chosen || chosen.identity !== participant.identity) return;
      drain(track, participant.identity);
    };

    const onUnsubscribed = (
      track: import("@livekit/rtc-node").RemoteTrack,
    ): void => {
      if (track.sid !== undefined && track.sid === activeSid) cancelActive();
    };

    room.on(rtc.RoomEvent.TrackSubscribed, onSubscribed);
    room.on(rtc.RoomEvent.TrackUnsubscribed, onUnsubscribed);
    startFromExisting();

    return () => {
      stopped = true;
      room.off(rtc.RoomEvent.TrackSubscribed, onSubscribed);
      room.off(rtc.RoomEvent.TrackUnsubscribed, onUnsubscribed);
      cancelActive();
    };
  };

  return {
    startAvatarRelay,
    get isOpen() {
      return !closed;
    },
    get agentIdentity() {
      return agentIdentity;
    },
    sendCallerAudio(pcm: Buffer): void {
      if (closed || pcm.length === 0) return;
      // Copied into a fresh, aligned buffer rather than viewed in place. A
      // Buffer from Node's pool can sit at an ODD byteOffset, and an Int16Array
      // view there does not read wrong, it throws RangeError and kills the
      // call. Predicting the allocator is not worth one memcpy per frame.
      const aligned = new ArrayBuffer(pcm.length);
      Buffer.from(aligned).set(pcm);
      const samples = new Int16Array(aligned);
      const frame = new rtc.AudioFrame(
        samples,
        SAMPLE_RATE_HZ,
        NUM_CHANNELS,
        samples.length / NUM_CHANNELS,
      );
      void source.captureFrame(frame).catch((err: unknown) => {
        if (!closed)
          logger.warn(
            `standin: publishing caller audio failed: ${String(err)}`,
          );
      });
    },
    async publish(topic: string, payload: string): Promise<void> {
      if (closed) return;
      try {
        await local.publishData(new TextEncoder().encode(payload), {
          topic,
          reliable: true,
        });
      } catch (err) {
        // Context is additive: an agent that never reads it is not a failed call.
        logger.warn(`standin: publishing ${topic} failed: ${String(err)}`);
      }
    },
    async aclose(): Promise<void> {
      if (closed) return;
      closed = true;
      try {
        await room.disconnect();
      } catch {
        // already closing
      }
      if (!config.deleteRoomOnEnd) return;
      // Deleted, not just left. A dispatched agent job whose room still exists
      // sits there until LiveKit's own empty-room timeout, which is minutes of
      // a worker slot doing nothing. Deleting it ends the job at once.
      try {
        const client = new server.RoomServiceClient(
          httpUrl(config.url),
          config.apiKey,
          config.apiSecret,
        );
        await client.deleteRoom(roomName);
      } catch (err) {
        logger.warn(
          `standin: could not delete the room, it will idle out: ${String(err)}`,
        );
      }
    },
  };
}
