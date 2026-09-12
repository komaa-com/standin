// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * What the Deepgram plugin reads from the environment.
 *
 * One prefix, `DEEPGRAM_`, plus the SDK-wide `STANDIN_VISION_*` that every
 * plugin shares for looking at a screen share.
 */

import { StandInError } from "../../errors.js";

/** Refuse a host that is not Deepgram: the API key travels to it. */
const ALLOWED_HOST_SUFFIX = ".deepgram.com";

export const DEFAULT_INSTRUCTIONS =
  "You are a helpful voice assistant on a live Microsoft Teams call. You are speaking " +
  "aloud: keep replies short, natural and conversational, and never use markdown, " +
  "lists or emoji.";

/** Everything the Deepgram plugin needs, resolved once per worker. */
export interface DeepgramConfig {
  /** `DEEPGRAM_API_KEY`. Never logged, never sent to StandIn. */
  apiKey: string;
  /** `DEEPGRAM_AGENT_HOST`, the Voice Agent socket host. */
  agentHost: string;
  /** `DEEPGRAM_API_HOST`, for the REST calls. */
  apiHost: string;
  /** `DEEPGRAM_LISTEN_MODEL`: speech to text. */
  listenModel: string;
  /** `DEEPGRAM_SPEAK_MODEL`: text to speech. */
  speakModel: string;
  /** `DEEPGRAM_THINK_PROVIDER`: which LLM vendor Deepgram should reason with. */
  thinkProvider: string;
  /** `DEEPGRAM_THINK_MODEL`. */
  thinkModel: string;
  /** `DEEPGRAM_THINK_ENDPOINT_URL`, to point the thinking step at your own model. */
  thinkEndpointUrl?: string;
  /**
   * `DEEPGRAM_THINK_ENDPOINT_HEADERS`, a JSON object. This carries YOUR model
   * credentials, so it is never logged.
   */
  thinkEndpointHeaders: Record<string, string>;
  /** `DEEPGRAM_LANGUAGE`. */
  language: string;
  /** `DEEPGRAM_INSTRUCTIONS`: the agent's base prompt. */
  instructions: string;
  /** `DEEPGRAM_GREETING`: what the agent says first, if anything. */
  greeting?: string;
  /**
   * `DEEPGRAM_LOG_TRANSCRIPTS`. Off by default, and gated a second time on the
   * call actually being recorded.
   */
  logTranscripts: boolean;
}

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(`${name} is required to answer calls with Deepgram`);
  return value;
}

function host(name: string, fallback: string): string {
  const value = (process.env[name] ?? "").trim() || fallback;
  if (value !== "deepgram.com" && !value.endsWith(ALLOWED_HOST_SUFFIX)) {
    throw new StandInError(`${name} must be a deepgram.com host, got ${value}`);
  }
  return value;
}

/** Read the configuration, or throw naming the variable that is missing. */
export function deepgramConfigFromEnv(): DeepgramConfig {
  const rawHeaders = (process.env.DEEPGRAM_THINK_ENDPOINT_HEADERS ?? "").trim();
  let thinkEndpointHeaders: Record<string, string> = {};
  if (rawHeaders) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawHeaders);
    } catch {
      throw new StandInError(
        "DEEPGRAM_THINK_ENDPOINT_HEADERS must be a JSON object",
      );
    }
    if (
      typeof parsed !== "object" ||
      parsed === null ||
      Array.isArray(parsed)
    ) {
      throw new StandInError(
        "DEEPGRAM_THINK_ENDPOINT_HEADERS must be a JSON object",
      );
    }
    thinkEndpointHeaders = Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [
        k,
        String(v),
      ]),
    );
  }
  return {
    apiKey: required("DEEPGRAM_API_KEY"),
    agentHost: host("DEEPGRAM_AGENT_HOST", "agent.deepgram.com"),
    apiHost: host("DEEPGRAM_API_HOST", "api.deepgram.com"),
    listenModel: (process.env.DEEPGRAM_LISTEN_MODEL ?? "").trim() || "nova-3",
    speakModel:
      (process.env.DEEPGRAM_SPEAK_MODEL ?? "").trim() || "aura-2-thalia-en",
    thinkProvider:
      (process.env.DEEPGRAM_THINK_PROVIDER ?? "").trim() || "open_ai",
    thinkModel:
      (process.env.DEEPGRAM_THINK_MODEL ?? "").trim() || "gpt-4o-mini",
    thinkEndpointUrl:
      (process.env.DEEPGRAM_THINK_ENDPOINT_URL ?? "").trim() || undefined,
    thinkEndpointHeaders,
    language: (process.env.DEEPGRAM_LANGUAGE ?? "").trim() || "en",
    instructions:
      (process.env.DEEPGRAM_INSTRUCTIONS ?? "").trim() || DEFAULT_INSTRUCTIONS,
    greeting: (process.env.DEEPGRAM_GREETING ?? "").trim() || undefined,
    logTranscripts: process.env.DEEPGRAM_LOG_TRANSCRIPTS === "true",
  };
}
