// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What this plugin reads from the environment.
 *
 * Environment only, matching how the providers themselves expect their keys to
 * arrive. Nothing is read at import: {@link elevenLabsConfigFromEnv} runs when
 * you build a handler, so a worker that never uses ElevenLabs never needs an
 * ElevenLabs key.
 *
 * One prefix, `ELEVENLABS_`. The standalone bridge this replaces had grown two,
 * which is the sort of thing that survives only until somebody documents it.
 */

import { StandInError } from "../../errors.js";

/** Refuse a host that is not ElevenLabs: the API key and agent id travel to it. */
const ALLOWED_HOST_SUFFIX = ".elevenlabs.io";

/** Everything the ElevenLabs plugin needs, resolved once per worker. */
export interface ElevenLabsConfig {
  /** `ELEVENLABS_API_KEY`. Never logged, never sent to StandIn. */
  apiKey: string;
  /** `ELEVENLABS_AGENT_ID`: which agent answers the call. */
  agentId: string;
  /** `ELEVENLABS_HOST`. Must be an elevenlabs.io host. */
  host: string;
  /** `ELEVENLABS_ENVIRONMENT`, for agents deployed to a named environment. */
  environment?: string;
  /**
   * `ELEVENLABS_FIRST_MESSAGE` overrides the agent's opening line, and only
   * when the agent's own security settings allow the override.
   */
  firstMessage?: string;
  /** `ELEVENLABS_AGENT_BRANCH_ID`, to answer with a specific branch. */
  agentBranchId?: string;
  /**
   * `ELEVENLABS_LOG_TRANSCRIPTS`. Off by default, and gated a second time on
   * the call being recorded: a transcript in your logs is a recording of the
   * caller that they did not agree to.
   */
  logTranscripts: boolean;
}

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(
      `${name} is required to answer calls with ElevenLabs`,
    );
  return value;
}

function optional(name: string): string | undefined {
  return (process.env[name] ?? "").trim() || undefined;
}

/** Read the configuration, or throw naming the variable that is missing. */
export function elevenLabsConfigFromEnv(): ElevenLabsConfig {
  const host =
    (process.env.ELEVENLABS_HOST ?? "").trim() || "api.elevenlabs.io";
  if (host !== "elevenlabs.io" && !host.endsWith(ALLOWED_HOST_SUFFIX)) {
    throw new StandInError(
      `ELEVENLABS_HOST must be an elevenlabs.io host, got ${host}`,
    );
  }
  return {
    apiKey: required("ELEVENLABS_API_KEY"),
    agentId: required("ELEVENLABS_AGENT_ID"),
    host,
    environment: optional("ELEVENLABS_ENVIRONMENT"),
    firstMessage: optional("ELEVENLABS_FIRST_MESSAGE"),
    agentBranchId: optional("ELEVENLABS_AGENT_BRANCH_ID"),
    logTranscripts: process.env.ELEVENLABS_LOG_TRANSCRIPTS === "true",
  };
}
