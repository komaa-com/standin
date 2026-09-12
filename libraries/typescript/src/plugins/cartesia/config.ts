// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** What the Cartesia plugin reads from the environment. */

import { StandInError } from "../../errors.js";

/** Refuse a host that is not Cartesia: the API key travels to it. */
const ALLOWED_HOST_SUFFIX = ".cartesia.ai";

/** Everything the Cartesia plugin needs, resolved once per worker. */
export interface CartesiaConfig {
  /**
   * `CARTESIA_API_KEY`. Used ONLY to mint a per-call token over HTTPS, so the
   * long-lived key never rides the agent socket itself.
   */
  apiKey: string;
  /** `CARTESIA_AGENT_ID`: which Line agent answers the call. */
  agentId: string;
  /** `CARTESIA_API_HOST`. */
  apiHost: string;
  /** `CARTESIA_VERSION`, sent as the API version header. */
  version: string;
  /** `CARTESIA_VOICE_ID`, to override the agent's configured voice. */
  voiceId?: string;
  /** `CARTESIA_INTRODUCTION`: what the agent says first. */
  introduction?: string;
  /**
   * `CARTESIA_SYSTEM_PROMPT`.
   *
   * Left unset, the agent keeps the prompt you wrote on Cartesia's platform and
   * this plugin adds nothing to it. Set it and the caller's details are
   * appended to YOUR prompt. Nothing here ever silently replaces a prompt
   * written on the other side.
   */
  systemPrompt?: string;
}

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(`${name} is required to answer calls with Cartesia`);
  return value;
}

/** Read the configuration, or throw naming the variable that is missing. */
export function cartesiaConfigFromEnv(): CartesiaConfig {
  const apiHost =
    (process.env.CARTESIA_API_HOST ?? "").trim() || "api.cartesia.ai";
  if (apiHost !== "cartesia.ai" && !apiHost.endsWith(ALLOWED_HOST_SUFFIX)) {
    throw new StandInError(
      `CARTESIA_API_HOST must be a cartesia.ai host, got ${apiHost}`,
    );
  }
  return {
    apiKey: required("CARTESIA_API_KEY"),
    agentId: required("CARTESIA_AGENT_ID"),
    apiHost,
    version: (process.env.CARTESIA_VERSION ?? "").trim() || "2025-04-16",
    voiceId: (process.env.CARTESIA_VOICE_ID ?? "").trim() || undefined,
    introduction: (process.env.CARTESIA_INTRODUCTION ?? "").trim() || undefined,
    systemPrompt:
      (process.env.CARTESIA_SYSTEM_PROMPT ?? "").trim() || undefined,
  };
}
