// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** What the OpenAI Realtime plugin reads from the environment. */

import { StandInError } from "../../errors.js";

/** Refuse a host that is not OpenAI or Azure: the API key travels to it. */
const ALLOWED_HOST_SUFFIXES = [".openai.com", ".azure.com"];

export const DEFAULT_INSTRUCTIONS =
  "You are a helpful voice assistant on a live Microsoft Teams call. You are speaking " +
  "aloud: keep replies short, natural and conversational, and never use markdown, " +
  "lists or emoji.";

/** Everything the OpenAI plugin needs, resolved once per worker. */
export interface OpenAIConfig {
  /** `OPENAI_API_KEY`. Never logged, never sent to StandIn. */
  apiKey: string;
  /** `OPENAI_REALTIME_MODEL`. */
  model: string;
  /** `OPENAI_REALTIME_HOST`. */
  host: string;
  /** `OPENAI_VOICE`, or the model's default when unset. */
  voice?: string;
  /** `OPENAI_INSTRUCTIONS`: the agent's base prompt. */
  instructions: string;
  /**
   * `OPENAI_VAD_TYPE`: `server_vad` hears silence, `semantic_vad` hears
   * finished thoughts. Semantic interrupts less often mid-sentence.
   */
  vadType: "server_vad" | "semantic_vad";
  /** `OPENAI_TRANSCRIPTION_MODEL`, to get transcripts alongside the audio. */
  transcriptionModel?: string;
  /**
   * `OPENAI_LOG_TRANSCRIPTS`. Off by default, and gated a second time on the
   * call actually being recorded.
   */
  logTranscripts: boolean;
}

function required(name: string): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(`${name} is required to answer calls with OpenAI`);
  return value;
}

/** Read the configuration, or throw naming the variable that is missing. */
export function openAIConfigFromEnv(): OpenAIConfig {
  const host =
    (process.env.OPENAI_REALTIME_HOST ?? "").trim() || "api.openai.com";
  if (!ALLOWED_HOST_SUFFIXES.some((suffix) => host.endsWith(suffix))) {
    throw new StandInError(
      `OPENAI_REALTIME_HOST must be an openai.com or azure.com host, got ${host}`,
    );
  }
  const vad = (process.env.OPENAI_VAD_TYPE ?? "").trim() || "semantic_vad";
  if (vad !== "server_vad" && vad !== "semantic_vad") {
    throw new StandInError(
      `OPENAI_VAD_TYPE must be server_vad or semantic_vad, got ${vad}`,
    );
  }
  return {
    apiKey: required("OPENAI_API_KEY"),
    model: (process.env.OPENAI_REALTIME_MODEL ?? "").trim() || "gpt-realtime",
    host,
    voice: (process.env.OPENAI_VOICE ?? "").trim() || undefined,
    instructions:
      (process.env.OPENAI_INSTRUCTIONS ?? "").trim() || DEFAULT_INSTRUCTIONS,
    vadType: vad,
    transcriptionModel:
      (process.env.OPENAI_TRANSCRIPTION_MODEL ?? "").trim() || undefined,
    logTranscripts: process.env.OPENAI_LOG_TRANSCRIPTS === "true",
  };
}
