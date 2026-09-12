// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What the LiveKit plugin reads from the environment.
 *
 * The `LIVEKIT_*` names are LiveKit's own, which a worker deployment already
 * has set. Nothing new to learn, and nothing duplicated.
 */

import { StandInError } from "../../errors.js";

/** Everything the LiveKit plugin needs, resolved once per worker. */
export interface LiveKitConfig {
  /** `LIVEKIT_URL`, the `wss://` project URL. */
  url: string;
  /** `LIVEKIT_API_KEY`. */
  apiKey: string;
  /** `LIVEKIT_API_SECRET`. Never logged, never sent to StandIn. */
  apiSecret: string;
  /**
   * `LIVEKIT_AGENT_NAME`: the name your worker registered with.
   *
   * Set it and each call explicitly dispatches that agent into its room. Leave
   * it and the room creation itself assigns the job, which is LiveKit's
   * automatic dispatch.
   */
  agentName?: string;
  /** `LIVEKIT_ROOM_PREFIX`, prepended to the call id to name the room. */
  roomPrefix: string;

  /**
   * `LIVEKIT_TILE_VIDEO`: `auto` (the default) relays whichever participant's
   * video the room offers, `off` leaves StandIn's avatar there, and any other
   * value is a participant IDENTITY to pin the relay to.
   *
   * Naming an identity matters when a separate avatar worker publishes the
   * video and publish-on-behalf is not set: `auto` would relay whichever
   * participant happened to publish first, which on a busy room is the wrong
   * one.
   *
   * On by default because an agent that publishes video almost always means it
   * for the caller to see. It needs the optional `sharp` package to encode
   * frames; without it the relay stays off with one log line and the audio is
   * unaffected.
   */
  tileVideo: "auto" | "off" | (string & {});

  /**
   * Delete the room at teardown, so the dispatched agent job ends at once.
   *
   * On by default. A job whose room still exists sits there until LiveKit's own
   * empty-room timeout, which is minutes of a worker slot doing nothing.
   */
  deleteRoomOnEnd: boolean;

  /** `LIVEKIT_TILE_VIDEO_FPS`, clamped by the SDK to a sane ceiling. */
  tileVideoFps: number;
}

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(`${name} is required to answer calls with LiveKit`);
  return value;
}

/**
 * A whole positive number, or an error naming the variable.
 *
 * Fails loud rather than substituting the default. An operator who typed
 * `LIVEKIT_TILE_VIDEO_FPS=twelve` is looking at a setting that is not the one in
 * force, and silence is what makes that take an afternoon to find.
 */
function positiveInt(name: string, fallback: number): number {
  const raw = (process.env[name] ?? "").trim();
  if (raw === "") return fallback;
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new StandInError(
      `${name} must be a whole number above zero, not ${JSON.stringify(raw)}`,
    );
  }
  return value;
}

/** Read the configuration, or throw naming the variable that is missing. */
export function liveKitConfigFromEnv(): LiveKitConfig {
  return {
    url: required("LIVEKIT_URL"),
    apiKey: required("LIVEKIT_API_KEY"),
    apiSecret: required("LIVEKIT_API_SECRET"),
    agentName: (process.env.LIVEKIT_AGENT_NAME ?? "").trim() || undefined,
    roomPrefix: (process.env.LIVEKIT_ROOM_PREFIX ?? "").trim() || "msteams-",
    deleteRoomOnEnd: (process.env.LIVEKIT_DELETE_ROOM ?? "").trim() !== "off",
    tileVideo: (process.env.LIVEKIT_TILE_VIDEO ?? "").trim() || "auto",
    tileVideoFps: positiveInt("LIVEKIT_TILE_VIDEO_FPS", 12),
  };
}
