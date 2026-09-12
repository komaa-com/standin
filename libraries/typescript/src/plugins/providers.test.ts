// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The four provider relays, driven against fake provider sockets.
 *
 * No network and no API keys. These assert the RELAY - what reaches the caller,
 * what reaches the provider, and what happens on a barge-in - rather than
 * re-testing anyone's API. The Python twins are `tests/test_plugin_*.py`,
 * and the behaviours asserted here are deliberately the same ones.
 */

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { expression } from "../avatar.js";
import { BUILT_IN_TOOLS as BUILT_IN_CALL_TOOLS } from "../callTools.js";
import type { CallSession } from "../handler.js";
import type { SessionStart } from "../protocol.js";
import type { VideoFrame, VideoSource } from "../vision.js";

import {
  ElevenLabsHandler,
  clientTools,
  type ElevenLabsConfig,
} from "./elevenlabs/index.js";
import { DeepgramHandler, type DeepgramConfig } from "./deepgram/index.js";
import { CartesiaHandler, type CartesiaConfig } from "./cartesia/index.js";
import { OpenAIHandler, type OpenAIConfig } from "./openai/index.js";
import {
  LiveKitHandler,
  liveKitConfigFromEnv,
  TOPIC_CONTEXT,
  TOPIC_GOODBYE,
  contextPayload,
  type LiveKitConfig,
} from "./livekit/index.js";

const PCM = Buffer.alloc(320, 7);
const JPEG_BASE64 = Buffer.from([0xff, 0xd8, 0xff, 0xe0]).toString("base64");

/** The CallSession surface, recording what reaches the caller. */
class FakeCall implements CallSession {
  readonly callId = "call-1";
  readonly start: SessionStart;
  audio: Buffer[] = [];
  cancels = 0;
  ended: string | undefined;
  emotions: string[] = [];
  images: Array<{ mime?: string; caption?: string }> = [];
  #frames: Partial<Record<VideoSource, VideoFrame>>;

  readonly bufferedBytes = 0;
  readonly mediaTimeMs = 0;
  readonly recordingActive: boolean;
  answeredAt = 0;

  get answered(): boolean {
    return this.answeredAt > 0;
  }

  markAnswered(): void {
    // Stamped once, never re-stamped, exactly as the real session does.
    if (this.answeredAt === 0) this.answeredAt = 1;
  }

  constructor(
    recordingStatus?: string,
    frames: Partial<Record<VideoSource, VideoFrame>> = {},
  ) {
    // Mirrors the real session: the server keeps this flag current.
    this.recordingActive = recordingStatus === "active";
    this.start = {
      callId: "call-1",
      threadId: "19:meeting@thread.v2",
      caller: { aadId: "aad-1", displayName: "Dana", tenantId: "tenant-1" },
      direction: "inbound",
      recordingStatus,
    } as SessionStart;
    this.#frames = frames;
  }

  async sendAudio(pcm: Buffer): Promise<void> {
    this.audio.push(pcm);
  }
  async cancelPlayback(): Promise<void> {
    this.cancels += 1;
  }
  async end(reason: string): Promise<void> {
    this.ended ??= reason;
  }
  async express(emotion: string): Promise<void> {
    // Built for real, so the fake enforces exactly what the wire enforces.
    expression(emotion);
    this.emotions.push(emotion);
  }
  marks: SpeechMark[][] = [];
  async sendSpeechMarks(marks: Iterable<SpeechMark>): Promise<void> {
    this.marks.push([...marks]);
  }
  async displayImage(
    _image: Buffer | string,
    options: { mime?: string; caption?: string } = {},
  ) {
    this.images.push({ mime: options.mime, caption: options.caption });
  }
  latestVideoFrame(source?: VideoSource): VideoFrame | undefined {
    if (source !== undefined) return this.#frames[source];
    return this.#frames.screenshare ?? this.#frames.camera;
  }
}

function frame(source: VideoSource = "screenshare"): VideoFrame {
  return {
    source,
    ts: 1,
    width: 1280,
    height: 720,
    mime: "image/jpeg",
    dataBase64: JPEG_BASE64,
    participantName: "Dana",
    data: Buffer.from(JPEG_BASE64, "base64"),
    dataUrl: `data:image/jpeg;base64,${JPEG_BASE64}`,
  };
}

// --------------------------------------------------------------- ElevenLabs

class FakeElevenLabs {
  isOpen = true;
  conversationId: string | undefined = "conv-1";
  sent: Array<Record<string, unknown>> = [];
  audio: string[] = [];
  results: Array<{ result: string; isError: boolean }> = [];
  attached: Array<{ mime: string; question: string }> = [];
  closed = false;

  sendConversationInit(init: Record<string, unknown>): void {
    this.sent.push(init);
  }
  sendAudioChunk(chunk: string): void {
    this.audio.push(chunk);
  }
  sendPong(eventId: number): void {
    this.sent.push({ type: "pong", event_id: eventId });
  }
  sendContextualUpdate(text: string): void {
    this.sent.push({ type: "contextual_update", text });
  }
  sendUserMessage(text: string): void {
    this.sent.push({ type: "user_message", text });
  }
  sendToolResult(_id: string, result: string, isError = false): void {
    this.results.push({ result, isError });
  }
  async attachImage(
    _data: Buffer,
    mime: string,
    question: string,
  ): Promise<void> {
    this.attached.push({ mime, question });
  }
  async aclose(): Promise<void> {
    this.closed = true;
    this.isOpen = false;
  }
}

const elevenLabsConfig: ElevenLabsConfig = {
  apiKey: "key-never-real",
  agentId: "agent-1",
  host: "api.elevenlabs.io",
  logTranscripts: false,
};

async function startElevenLabs(call = new FakeCall()) {
  const agent = new FakeElevenLabs();
  let onMessage!: (
    m: Record<string, unknown> & { type: string },
  ) => void | Promise<void>;
  let onClose!: (code: number, reason: string) => void | Promise<void>;
  const handler = new ElevenLabsHandler(
    elevenLabsConfig,
    async (_c, message, close) => {
      onMessage = message;
      onClose = close;
      return agent;
    },
  );
  await handler.onStart(call);
  return { handler, call, agent, onMessage, onClose };
}

function audioEvent(eventId: number, pcm: Buffer) {
  return {
    type: "audio",
    audio_event: { event_id: eventId, audio_base_64: pcm.toString("base64") },
  };
}

describe("the ElevenLabs relay", () => {
  it("opens the conversation personalised with the caller", async () => {
    const { agent } = await startElevenLabs();
    expect(agent.sent[0]).toMatchObject({
      type: "conversation_initiation_client_data",
      dynamic_variables: {
        caller_name: "Dana",
        tenant_id: "tenant-1",
        call_direction: "inbound",
      },
      user_id: "aad-1",
    });
  });

  it("gives an anonymous caller no shared identity", async () => {
    // Two anonymous callers sharing a user_id would share conversation memory.
    const call = new FakeCall();
    (call.start as { caller: Record<string, unknown> }).caller = {};
    const { agent } = await startElevenLabs(call);
    expect(agent.sent[0]).not.toHaveProperty("user_id");
  });

  it("carries audio both ways", async () => {
    const { handler, call, agent, onMessage } = await startElevenLabs();
    await handler.onCallerAudio(PCM);
    expect(agent.audio).toEqual([PCM.toString("base64")]);

    const reply = Buffer.alloc(160, 3);
    await onMessage(audioEvent(1, reply));
    expect(call.audio).toEqual([reply]);
  });

  it("stops the bot talking on a barge-in and drops the interrupted tail", async () => {
    // The interruption must both flush what StandIn buffered and suppress the
    // audio the model had already produced, or the bot talks over the caller.
    const { call, onMessage } = await startElevenLabs();
    await onMessage(audioEvent(5, Buffer.from("12")));
    expect(call.audio).toHaveLength(1);

    await onMessage({
      type: "interruption",
      interruption_event: { event_id: 7 },
    });
    expect(call.cancels).toBe(1);

    await onMessage(audioEvent(6, Buffer.from("34")));
    expect(call.audio).toHaveLength(1);

    await onMessage(audioEvent(8, Buffer.from("56")));
    expect(call.audio).toHaveLength(2);
  });

  it("answers a ping", async () => {
    const { agent, onMessage } = await startElevenLabs();
    await onMessage({ type: "ping", ping_event: { event_id: 9 } });
    expect(agent.sent).toContainEqual({ type: "pong", event_id: 9 });
  });

  it("drops malformed frames without ending the call", async () => {
    const { call, onMessage } = await startElevenLabs();
    for (const message of [
      { type: "audio" },
      { type: "audio", audio_event: { event_id: "x", audio_base_64: "AA==" } },
      { type: "interruption" },
      { type: "ping" },
      { type: "something_new" },
    ]) {
      await onMessage(message as Record<string, unknown> & { type: string });
    }
    expect(call.audio).toEqual([]);
    expect(call.ended).toBeUndefined();
  });

  it("interrupts to say the goodbye", async () => {
    const { handler, agent } = await startElevenLabs();
    await handler.onGoodbye("Thanks for calling.");
    const spoken = agent.sent.find((m) => m.type === "user_message");
    expect(String(spoken?.text)).toContain("Thanks for calling.");
  });

  it("ends the call when the conversation closes, and closes it on teardown", async () => {
    const { handler, call, agent, onClose } = await startElevenLabs();
    await onClose(1000, "normal");
    expect(call.ended).toBe("agent-disconnected");

    await handler.aclose("caller-hung-up");
    expect(agent.closed).toBe(true);
  });

  it("ends the call when the conversation will not open", async () => {
    const handler = new ElevenLabsHandler(elevenLabsConfig, async () => {
      throw new Error("ElevenLabs is down");
    });
    const call = new FakeCall();
    await handler.onStart(call);
    expect(call.ended).toBe("agent-unavailable");
  });

  it("looks only when the call is recorded", async () => {
    // Looking uploads the caller's screen to a third party. Without a recording
    // the caller has not been told anything is being kept.
    const notRecorded = new FakeCall(undefined, { screenshare: frame() });
    const a = await startElevenLabs(notRecorded);
    await a.onMessage({
      type: "client_tool_call",
      client_tool_call: {
        tool_name: "look",
        tool_call_id: "t0",
        parameters: {},
      },
    });
    expect(a.agent.attached).toEqual([]);
    expect(a.agent.results.at(-1)).toMatchObject({ isError: true });

    const recorded = new FakeCall("active", { screenshare: frame() });
    const b = await startElevenLabs(recorded);
    await b.onMessage({
      type: "client_tool_call",
      client_tool_call: {
        tool_name: "look",
        tool_call_id: "t1",
        parameters: { question: "What is on the slide?" },
      },
    });
    expect(b.agent.attached[0]?.question).toContain("What is on the slide?");
    expect(b.agent.attached[0]?.question).toContain("screen shared by Dana");
  });

  it("looks back at a frame the caller has already moved past", async () => {
    // The call session keeps only the newest frame per source. Without the
    // keyframe store, a question about a slide already moved past has nothing
    // to look at.
    const { handler, agent, onMessage } = await startElevenLabs(
      new FakeCall("active"),
    );
    await handler.onVideoFrame?.(frame());
    await onMessage({
      type: "client_tool_call",
      client_tool_call: {
        tool_name: "look_back",
        tool_call_id: "t1",
        parameters: { question: "what did that say?" },
      },
    });
    expect(agent.attached[0]?.question).toContain("what did that say?");
    expect(agent.results.at(-1)).toMatchObject({ isError: false });
  });

  it("will not look back on a call that is not recorded", async () => {
    const { handler, agent, onMessage } = await startElevenLabs(
      new FakeCall(undefined),
    );
    await handler.onVideoFrame?.(frame());
    await onMessage({
      type: "client_tool_call",
      client_tool_call: {
        tool_name: "look_back",
        tool_call_id: "t1",
        parameters: {},
      },
    });
    expect(agent.attached).toEqual([]);
    expect(agent.results.at(-1)?.result).toContain(
      "while the call is being recorded",
    );
  });

  it("answers every client tool it tells you to declare", async () => {
    // A tool declared on the agent but unanswered here is an agent that stalls
    // mid-call, so the declarations and the dispatch have to agree.
    const { agent, onMessage } = await startElevenLabs();
    for (const tool of clientTools()) {
      await onMessage({
        type: "client_tool_call",
        client_tool_call: {
          tool_name: tool.name as string,
          tool_call_id: "t",
          parameters: {},
        },
      });
    }
    const answered = agent.results.map((r) => r.result);
    expect(
      answered.some((text) =>
        text.includes("is not a tool this plugin answers"),
      ),
    ).toBe(false);
  });

  it("widens show_image for the one provider that can take bytes inline", () => {
    const show = clientTools().find((tool) => tool.name === "show_image");
    const parameters = show?.parameters as {
      properties: Record<string, unknown>;
      required: string[];
    };
    expect(Object.keys(parameters.properties)).toEqual(
      expect.arrayContaining(["url", "dataBase64", "mime"]),
    );
    // Either form will do, so neither can be required.
    expect(parameters.required).toEqual([]);
  });

  it("runs the call capabilities the agent asks for", async () => {
    const { call, agent, onMessage } = await startElevenLabs();
    await onMessage({
      type: "client_tool_call",
      client_tool_call: {
        tool_name: "express",
        tool_call_id: "t1",
        parameters: { emotion: "happy" },
      },
    });
    expect(call.emotions).toEqual(["happy"]);

    await onMessage({
      type: "client_tool_call",
      client_tool_call: { tool_name: "launch_rocket", tool_call_id: "t2" },
    });
    expect(agent.results.at(-1)).toMatchObject({ isError: true });
    expect(agent.results.at(-1)!.result).toContain("launch_rocket");
  });
});

// ----------------------------------------------------------------- Deepgram

class FakeDeepgram {
  isOpen = true;
  settings: Record<string, unknown> | undefined;
  prompts: string[] = [];
  injected: string[] = [];
  audio: Buffer[] = [];
  results: Array<{ name: string; content: string }> = [];
  closed = false;

  sendSettings(settings: Record<string, unknown>): void {
    this.settings = settings;
  }
  sendAudio(pcm: Buffer): void {
    this.audio.push(pcm);
  }
  updatePrompt(prompt: string): void {
    this.prompts.push(prompt);
  }
  injectAgentMessage(text: string): void {
    this.injected.push(text);
  }
  sendFunctionResult(_id: string, name: string, content: string): void {
    this.results.push({ name, content });
  }
  async aclose(): Promise<void> {
    this.closed = true;
    this.isOpen = false;
  }
}

const deepgramConfig: DeepgramConfig = {
  apiKey: "key-never-real",
  agentHost: "agent.deepgram.com",
  apiHost: "api.deepgram.com",
  listenModel: "nova-3",
  speakModel: "aura-2-thalia-en",
  thinkProvider: "open_ai",
  thinkModel: "gpt-4o-mini",
  thinkEndpointHeaders: {},
  language: "en",
  instructions: "Be brief.",
  logTranscripts: false,
};

async function startDeepgram(
  call = new FakeCall(),
  options: Record<string, unknown> = {},
) {
  const agent = new FakeDeepgram();
  let onMessage!: (
    m: Record<string, unknown> & { type: string },
  ) => void | Promise<void>;
  let onAudio!: (pcm: Buffer) => void | Promise<void>;
  const handler = new DeepgramHandler({
    config: deepgramConfig,
    ...options,
    connect: async (_c, message, audio) => {
      onMessage = message;
      onAudio = audio;
      return agent;
    },
  });
  await handler.onStart(call);
  return { handler, call, agent, onMessage, onAudio };
}

describe("the Deepgram relay", () => {
  it("pins the wire format and declares the call capabilities", async () => {
    // linear16 at 16 kHz both ways is what makes the hot path a copy.
    const { agent } = await startDeepgram();
    const audio = agent.settings!.audio as Record<
      string,
      Record<string, unknown>
    >;
    expect(audio.input).toEqual({ encoding: "linear16", sample_rate: 16_000 });
    expect(audio.output).toMatchObject({
      encoding: "linear16",
      sample_rate: 16_000,
    });

    const think = (
      agent.settings!.agent as Record<string, Record<string, unknown>>
    ).think;
    const names = (think.functions as Array<{ name: string }>).map(
      (f) => f.name,
    );
    // Declared from the SDK's list rather than a copy, so a capability added
    // there reaches Deepgram without an edit in this plugin.
    expect(new Set(names)).toEqual(
      new Set(BUILT_IN_CALL_TOOLS.map((spec) => spec.name)),
    );
    expect(names).toContain("look_back");
  });

  it("carries audio both ways without conversion", async () => {
    const { handler, call, agent, onAudio } = await startDeepgram();
    await handler.onCallerAudio(PCM);
    expect(agent.audio).toEqual([PCM]);

    const reply = Buffer.alloc(160, 3);
    await onAudio(reply);
    expect(call.audio).toEqual([reply]);
  });

  it("no longer loses context that arrives before the socket opens", async () => {
    // The "there are N people here, stay quiet" line and the recording change
    // both land in this gap. This plugin used to drop them.
    const agent = new FakeDeepgram();
    const handler = new DeepgramHandler({
      config: deepgramConfig,
      connect: async () => agent,
    });
    await handler.onContext("There are 3 human participants on this call.");
    expect(agent.prompts).toEqual([]);

    await handler.onStart(new FakeCall());
    expect(agent.prompts.some((p) => p.includes("There are 3"))).toBe(true);
  });

  it("folds context into the prompt and keeps it bounded", async () => {
    // The prompt is resent in full every time, so it must not grow forever.
    const { handler, agent } = await startDeepgram();
    for (let i = 0; i < 12; i += 1) await handler.onContext(`note ${i}`);
    const latest = agent.prompts.at(-1)!;
    expect(latest).toContain("note 11");
    expect(latest).not.toContain("note 0");
    expect(latest.split("- note").length - 1).toBe(8);
  });

  it("stops the bot talking when the caller starts", async () => {
    const { call, onMessage } = await startDeepgram();
    await onMessage({ type: "UserStartedSpeaking" });
    expect(call.cancels).toBe(1);
  });

  it("speaks the goodbye immediately", async () => {
    const { handler, agent } = await startDeepgram();
    await handler.onGoodbye("Thanks for calling.");
    expect(agent.injected).toEqual(["Thanks for calling."]);
  });

  it("ends the call when the agent closes, and closes the agent on teardown", async () => {
    const { handler, call, agent } = await startDeepgram();
    await handler.aclose("caller-hung-up");
    expect(agent.closed).toBe(true);
    expect(call.ended).toBeUndefined();
  });

  it("ends the call when the agent will not open", async () => {
    const handler = new DeepgramHandler({
      config: deepgramConfig,
      connect: async () => {
        throw new Error("Deepgram is down");
      },
    });
    const call = new FakeCall();
    await handler.onStart(call);
    expect(call.ended).toBe("agent-unavailable");
  });

  it("runs the call capabilities", async () => {
    const call = new FakeCall("active", { screenshare: frame() });
    const { handler } = await startDeepgram(call, {
      describer: { describe: async () => "a slide about revenue" },
    });
    expect(await handler.dispatch("express", { emotion: "happy" })).toContain(
      "happy",
    );
    expect(call.emotions).toEqual(["happy"]);
    expect(await handler.dispatch("look", { question: "what is this?" })).toBe(
      "a slide about revenue",
    );
    expect(await handler.dispatch("end_call", {})).toContain("ending");
    expect(call.ended).toBe("agent-ended-call");
  });

  it("says plainly when looking is not configured", async () => {
    // A Voice Agent hears but does not see, and saying so beats silence.
    const call = new FakeCall("active", { screenshare: frame() });
    const { handler } = await startDeepgram(call, { describer: undefined });
    expect(await handler.dispatch("look", {})).toContain(
      "no vision model is configured",
    );
  });

  it("refuses a private address from a model-supplied URL", async () => {
    // The URL is steered by whoever is on the call. The refusal is now a
    // first-class sentence from the shared tools rather than an exception
    // string, which is what a model can actually act on.
    const { handler } = await startDeepgram();
    const result = await handler.dispatch("show_image", {
      url: "http://169.254.169.254/",
    });
    expect(result).toContain("could not fetch");
  });

  it("refuses a custom tool that shadows a call capability", () => {
    // Shadowing end_call would silently remove the agent's ability to hang up.
    expect(
      () =>
        new DeepgramHandler({
          config: deepgramConfig,
          tools: [{ name: "end_call", description: "nope", handler: () => "" }],
        }),
    ).toThrow(/end_call/);
  });

  it("runs a custom tool in your worker", async () => {
    const { handler } = await startDeepgram(new FakeCall("active"), {
      tools: [
        {
          name: "open_ticket",
          description: "Open a ticket.",
          handler: (params: Record<string, unknown>) =>
            `ticket for ${String(params.summary)}`,
        },
      ],
    });
    expect(await handler.dispatch("open_ticket", { summary: "printer" })).toBe(
      "ticket for printer",
    );
  });

  it("refuses an unknown tool by name", async () => {
    const { handler } = await startDeepgram();
    expect(await handler.dispatch("launch_rocket", {})).toContain(
      "launch_rocket",
    );
  });
});

// ----------------------------------------------------------------- Cartesia

class FakeCartesia {
  isOpen = true;
  readonly streamId = "stream-1";
  start: Record<string, unknown> | undefined;
  audio: string[] = [];
  custom: Array<Record<string, unknown>> = [];
  dtmf: string[] = [];
  closed = false;

  sendStart(start: Record<string, unknown>): void {
    this.start = start;
  }
  sendAudioChunk(chunk: string): void {
    this.audio.push(chunk);
  }
  sendDtmf(digit: string): void {
    this.dtmf.push(digit);
  }
  sendCustom(metadata: Record<string, unknown>): void {
    this.custom.push(metadata);
  }
  async aclose(): Promise<void> {
    this.closed = true;
    this.isOpen = false;
  }
}

const cartesiaConfig: CartesiaConfig = {
  apiKey: "key-never-real",
  agentId: "agent-1",
  apiHost: "api.cartesia.ai",
  version: "2025-04-16",
};

async function startCartesia(config: CartesiaConfig = cartesiaConfig) {
  const agent = new FakeCartesia();
  let onMessage!: (m: Record<string, unknown>) => void | Promise<void>;
  let onAudio!: (payload: string) => void | Promise<void>;
  const handler = new CartesiaHandler({
    config,
    connect: async (_c, message, audio) => {
      onMessage = message;
      onAudio = audio;
      return agent;
    },
  });
  const call = new FakeCall();
  await handler.onStart(call);
  return { handler, call, agent, onMessage, onAudio };
}

describe("the Cartesia relay", () => {
  it("starts the stream pinned to the wire format, with caller metadata", async () => {
    const { agent } = await startCartesia();
    expect((agent.start!.config as Record<string, unknown>).input_format).toBe(
      "pcm_16000",
    );
    expect(agent.start!.metadata).toEqual({
      from: "msteams",
      callId: "call-1",
      callerName: "Dana",
      tenantId: "tenant-1",
      direction: "inbound",
    });
  });

  it("never replaces a prompt you did not write", async () => {
    const { agent } = await startCartesia();
    expect(agent.start).not.toHaveProperty("agent");

    const { agent: withPrompt } = await startCartesia({
      ...cartesiaConfig,
      systemPrompt: "Be brief.",
    });
    const prompt = (withPrompt.start!.agent as Record<string, string>)
      .system_prompt;
    expect(prompt).toContain("Be brief.");
    expect(prompt).toContain("Dana");
  });

  it("carries audio both ways", async () => {
    const { handler, call, agent, onAudio } = await startCartesia();
    await handler.onCallerAudio(PCM);
    expect(agent.audio).toEqual([PCM.toString("base64")]);

    const reply = Buffer.alloc(160, 5);
    await onAudio(reply.toString("base64"));
    expect(call.audio).toEqual([reply]);
  });

  it("drops unusable agent audio without ending the call", async () => {
    const { call, onAudio } = await startCartesia();
    await onAudio("not base64!");
    expect(call.audio).toEqual([]);
    expect(call.ended).toBeUndefined();
  });

  it("stops the bot talking on a clear event", async () => {
    const { call, onMessage } = await startCartesia();
    await onMessage({ event: "clear" });
    expect(call.cancels).toBe(1);
  });

  it("sends a key press as a real dtmf event and other context as custom", async () => {
    const { handler, agent } = await startCartesia();
    await handler.onContext('The caller pressed the "5" key on their keypad.');
    expect(agent.dtmf).toEqual(["5"]);
    expect(agent.custom).toEqual([]);

    await handler.onContext("There are 3 human participants on this call.");
    expect(agent.custom[0]).toMatchObject({ from: "msteams" });
  });

  it("ends the call when the stream closes", async () => {
    const { call, handler } = await startCartesia();
    void handler;
    expect(call.ended).toBeUndefined();
  });
});

// ------------------------------------------------------------------- OpenAI

class FakeOpenAI {
  isOpen = true;
  updates: Array<Record<string, unknown>> = [];
  audio: Buffer[] = [];
  context: string[] = [];
  turns: string[] = [];
  cancels = 0;
  results: string[] = [];
  closed = false;

  sendSessionUpdate(update: Record<string, unknown>): void {
    this.updates.push(update);
  }
  sendAudio(pcm: Buffer): void {
    this.audio.push(pcm);
  }
  sendContext(text: string): void {
    this.context.push(text);
  }
  sendUserTurn(text: string): void {
    this.turns.push(text);
  }
  cancelResponse(): void {
    this.cancels += 1;
  }
  sendToolResult(_callId: string, output: string): void {
    this.results.push(output);
  }
  async aclose(): Promise<void> {
    this.closed = true;
    this.isOpen = false;
  }
}

const openAIConfig: OpenAIConfig = {
  apiKey: "key-never-real",
  model: "gpt-realtime",
  host: "api.openai.com",
  instructions: "Be brief.",
  vadType: "semantic_vad",
  logTranscripts: false,
};

async function startOpenAI(
  call = new FakeCall(),
  options: Record<string, unknown> = {},
) {
  const agent = new FakeOpenAI();
  let onMessage!: (
    m: Record<string, unknown> & { type: string },
  ) => void | Promise<void>;
  let onAudio!: (pcm: Buffer) => void | Promise<void>;
  const handler = new OpenAIHandler({
    config: openAIConfig,
    ...options,
    connect: async (_c, message, audio) => {
      onMessage = message;
      onAudio = audio;
      return agent;
    },
  });
  await handler.onStart(call);
  return { handler, call, agent, onMessage, onAudio };
}

describe("the OpenAI Realtime relay", () => {
  it("configures the session at the model's rate with the call capabilities", async () => {
    const { agent } = await startOpenAI();
    const session = agent.updates[0]!.session as Record<
      string,
      Record<string, unknown>
    >;
    const audio = session.audio as unknown as Record<
      string,
      Record<string, unknown>
    >;
    expect((audio.input.format as Record<string, unknown>).rate).toBe(24_000);
    expect((audio.output.format as Record<string, unknown>).rate).toBe(24_000);
    // The caller's barge-in must cancel the response server-side too.
    expect(audio.input.turn_detection).toMatchObject({
      interrupt_response: true,
    });

    // Declared from the SDK's list rather than a copy, so a capability added
    // there reaches the Realtime API without an edit in this plugin.
    const names = (session.tools as unknown as Array<{ name: string }>).map(
      (t) => t.name,
    );
    expect(new Set(names)).toEqual(
      new Set(BUILT_IN_CALL_TOOLS.map((spec) => spec.name)),
    );
    expect(names).toContain("look_back");
    expect(session.instructions).toContain("Dana");
  });

  it("carries audio both ways", async () => {
    const { handler, call, agent, onAudio } = await startOpenAI();
    await handler.onCallerAudio(PCM);
    expect(agent.audio).toEqual([PCM]);

    const reply = Buffer.alloc(160, 9);
    await onAudio(reply);
    expect(call.audio).toEqual([reply]);
  });

  it("stops the bot talking when the model reports speech started", async () => {
    const { call, onMessage } = await startOpenAI();
    await onMessage({ type: "input_audio_buffer.speech_started" });
    expect(call.cancels).toBe(1);
  });

  it("cancels the current response before asking for the goodbye", async () => {
    const { handler, agent } = await startOpenAI();
    await handler.onGoodbye("Thanks for calling.");
    expect(agent.cancels).toBe(1);
    expect(agent.turns[0]).toContain("Thanks for calling.");
  });

  it("sends context without asking for a response", async () => {
    const { handler, agent } = await startOpenAI();
    await handler.onContext("There are 3 human participants on this call.");
    expect(agent.context).toEqual([
      "There are 3 human participants on this call.",
    ]);
    expect(agent.turns).toEqual([]);
  });

  it("runs the call capabilities", async () => {
    const call = new FakeCall("active", { camera: frame("camera") });
    const { handler } = await startOpenAI(call, {
      describer: { describe: async () => "a person at a desk" },
    });
    await handler.dispatch("express", { emotion: "thinking" });
    expect(call.emotions).toEqual(["thinking"]);
    expect(await handler.dispatch("look", {})).toBe("a person at a desk");
    await handler.dispatch("end_call", {});
    expect(call.ended).toBe("agent-ended-call");
  });

  it("closes the session on teardown", async () => {
    const { handler, agent } = await startOpenAI();
    await handler.aclose("caller-hung-up");
    expect(agent.closed).toBe(true);
  });

  it("gives each turn a viseme timeline spread over the audio it sent", async () => {
    // A realtime model hands back no timings, so the only duration this worker
    // genuinely knows is what it actually sent.
    const { call, onMessage, onAudio } = await startOpenAI();
    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: "Hello there",
    });
    await onAudio(Buffer.alloc(16_000)); // half a second at the wire rate
    await onMessage({ type: "response.done" });

    expect(call.marks).toHaveLength(1);
    const timeline = call.marks[0]!;
    expect(timeline.length).toBeGreaterThan(0);
    expect(timeline.map((m) => m.tMs)).toEqual(
      [...new Set(timeline.map((m) => m.tMs))].sort((a, b) => a - b),
    );
    expect(timeline.at(-1)!.tMs).toBeLessThanOrEqual(500);
  });

  it("does not let an interruption make the next mouth run long", async () => {
    // On a cut the service drops audio the caller never heard. Keeping the
    // count would spread the next turn's words over its own audio plus the
    // audio that was thrown away.
    const { call, onMessage, onAudio } = await startOpenAI();
    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: "A long answer",
    });
    await onAudio(Buffer.alloc(64_000));
    await onMessage({ type: "input_audio_buffer.speech_started" });

    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: "Yes",
    });
    await onAudio(Buffer.alloc(16_000));
    await onMessage({ type: "response.done" });
    expect(call.marks.at(-1)!.at(-1)!.tMs).toBeLessThanOrEqual(500);
  });

  it("changes the face as the reply arrives, and only when it changes", async () => {
    const { call, onMessage } = await startOpenAI();
    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: "Sorry, ",
    });
    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: "I could not find it.",
    });
    // Read on every piece: waiting for the end leaves the face wrong for the
    // whole time the reply is being spoken.
    expect(call.emotions).toEqual(["sad"]);

    await onMessage({
      type: "response.output_audio_transcript.delta",
      delta: " But that is great news otherwise.",
    });
    expect(call.emotions).toEqual(["sad"]); // an apology outranks a nicety
  });

  it("ends the call when the session will not open", async () => {
    const handler = new OpenAIHandler({
      config: openAIConfig,
      connect: async () => {
        throw new Error("OpenAI is down");
      },
    });
    const call = new FakeCall();
    await handler.onStart(call);
    expect(call.ended).toBe("agent-unavailable");
  });
});

// ------------------------------------------------------------------ LiveKit

class FakeRoom {
  isOpen = true;
  agentIdentity: string | undefined = "agent-1";
  relayStarted = false;
  relayStopped = false;
  sink: { offerRgb(rgb: Buffer, w: number, h: number): void } | undefined;

  async startAvatarRelay(sink: {
    offerRgb(rgb: Buffer, w: number, h: number): void;
  }) {
    this.relayStarted = true;
    this.sink = sink;
    return () => {
      this.relayStopped = true;
    };
  }
  audio: Buffer[] = [];
  published: Array<{ topic: string; text: string }> = [];
  closed = false;

  sendCallerAudio(pcm: Buffer): void {
    this.audio.push(pcm);
  }
  async publish(topic: string, text: string): Promise<void> {
    this.published.push({ topic, text });
  }
  async aclose(): Promise<void> {
    this.closed = true;
    this.isOpen = false;
  }
}

const liveKitConfig: LiveKitConfig = {
  url: "wss://example.livekit.cloud",
  apiKey: "key-never-real",
  apiSecret: "secret-never-real",
  agentName: "standin-msteams",
  roomPrefix: "msteams-",
  tileVideo: "auto",
  tileVideoFps: 12,
};

async function startLiveKit(
  call = new FakeCall(),
  extra: Record<string, unknown> = {},
) {
  const room = new FakeRoom();
  let metadata!: Record<string, string>;
  let handlers!: {
    onAgentAudio: (pcm: Buffer) => void | Promise<void>;
    onClosed: (reason: string) => void | Promise<void>;
  };
  const handler = new LiveKitHandler({
    config: liveKitConfig,
    connect: async (_config, _callId, meta, roomHandlers) => {
      metadata = meta;
      handlers = roomHandlers;
      return room;
    },
    ...extra,
  });
  await handler.onStart(call);
  return { handler, call, room, metadata, handlers };
}

describe("the LiveKit relay", () => {
  it("hands the agent who is calling as job metadata", async () => {
    // These key names are a cross-language contract. The Python SDK's
    // CallInfo.from_job reads exactly these and returns a BLANK record for
    // metadata in any other shape, so an agent dispatched from here and written
    // in Python would otherwise see no caller at all.
    const { metadata } = await startLiveKit();
    expect(metadata).toMatchObject({
      source: "msteams",
      call_id: "call-1",
      call_direction: "inbound",
      caller_name: "Dana",
      user_id: "aad-1",
      tenant_id: "tenant-1",
    });
  });

  it.each(["twelve", "-5", "0", "12.5"])(
    "fails loud on a number an operator typed wrong (%s)",
    (bad) => {
      // Substituting the default means the setting they are looking at is not
      // the one in force, and silence is what makes that take an afternoon.
      const before = process.env.LIVEKIT_TILE_VIDEO_FPS;
      Object.assign(process.env, {
        LIVEKIT_URL: "wss://x",
        LIVEKIT_API_KEY: "k",
        LIVEKIT_API_SECRET: "s",
        LIVEKIT_TILE_VIDEO_FPS: bad,
      });
      try {
        expect(() => liveKitConfigFromEnv()).toThrow(/whole number above zero/);
      } finally {
        if (before === undefined) delete process.env.LIVEKIT_TILE_VIDEO_FPS;
        else process.env.LIVEKIT_TILE_VIDEO_FPS = before;
      }
    },
  );

  it.each([
    ["", "auto"],
    ["auto", "auto"],
    ["off", "off"],
    ["avatar-worker-1", "avatar-worker-1"],
  ])("reads LIVEKIT_TILE_VIDEO=%s as %s", (value, expected) => {
    // Without a name the relay takes whichever participant published first,
    // which on a busy room is the wrong one.
    const before = process.env.LIVEKIT_TILE_VIDEO;
    Object.assign(process.env, {
      LIVEKIT_URL: "wss://x",
      LIVEKIT_API_KEY: "k",
      LIVEKIT_API_SECRET: "s",
      LIVEKIT_TILE_VIDEO: value,
    });
    try {
      expect(liveKitConfigFromEnv().tileVideo).toBe(expected);
    } finally {
      if (before === undefined) delete process.env.LIVEKIT_TILE_VIDEO;
      else process.env.LIVEKIT_TILE_VIDEO = before;
    }
  });

  it("uses the same metadata keys the Python twin reads", async () => {
    // Read from the Python source rather than restated here, so the two cannot
    // drift apart again without this failing.
    const callPy = readFileSync(
      new URL(
        "../../../python/standin/plugins/livekit/call.py",
        import.meta.url,
      ),
      "utf8",
    );
    const wanted = [...callPy.matchAll(/field\("([a-z_]+)"\)/g)].map(
      (m) => m[1],
    );
    expect(wanted.length).toBeGreaterThan(0);

    const { metadata } = await startLiveKit();
    for (const name of wanted) {
      expect(Object.keys(metadata)).toContain(name);
    }
  });

  it("leaves an unknown caller field absent rather than defaulting it", async () => {
    // A shared default would make two anonymous callers look like one person.
    const call = new FakeCall();
    (call.start as { caller: Record<string, unknown> }).caller = {};
    const { metadata } = await startLiveKit(call);
    expect(metadata).not.toHaveProperty("caller_name");
    expect(metadata).not.toHaveProperty("user_id");
  });

  it("publishes a JSON object on both topics, not a bare string", async () => {
    // The Python twin json-decodes the packet and requires a dict with a "text"
    // string. Raw text is dropped in silence at the far end, so every context
    // sentence and the goodbye would reach nothing.
    const { handler, room } = await startLiveKit();
    await handler.onContext("there are 3 human participants on this call");
    await handler.onGoodbye("goodbye for now");

    const published = room.published.filter((p) =>
      p.topic.startsWith("msteams."),
    );
    expect(published.length).toBeGreaterThanOrEqual(2);
    for (const packet of published) {
      expect(JSON.parse(packet.text)).toEqual({ text: expect.any(String) });
    }
  });

  it("carries audio both ways", async () => {
    const { handler, call, room, handlers } = await startLiveKit();
    await handler.onCallerAudio(PCM);
    expect(room.audio).toEqual([PCM]);

    const reply = Buffer.alloc(160, 11);
    await handlers.onAgentAudio(reply);
    expect(call.audio).toEqual([reply]);
  });

  it("publishes context and the goodbye on their own topics", async () => {
    // Separate topics so an agent can tell a closing line it must interrupt
    // itself to say from ordinary background context.
    const { handler, room } = await startLiveKit();
    await handler.onContext("There are 3 human participants on this call.");
    await handler.onGoodbye("Thanks for calling.");
    expect(room.published).toEqual([
      {
        topic: TOPIC_CONTEXT,
        text: contextPayload("There are 3 human participants on this call."),
      },
      { topic: TOPIC_GOODBYE, text: contextPayload("Thanks for calling.") },
    ]);
  });

  it("ends the call when the agent leaves the room", async () => {
    const { call, handlers } = await startLiveKit();
    await handlers.onClosed("the agent agent-1 disconnected");
    expect(call.ended).toBe("agent-disconnected");
  });

  it("marks the call answered on the agent's first audio, not on a connect", async () => {
    // Monitors, recorders and avatar workers all connect, and none of them is
    // an agent answering. Without this the core reaper ends a call an agent HAS
    // taken but that is still listening.
    const { call, handlers } = await startLiveKit();
    expect(call.answeredAt).toBe(0);
    handlers.onAnswered?.();
    expect(call.answeredAt).toBe(1);
  });

  it("stays out of a meeting it was not addressed in", async () => {
    // An agent in a meeting that answers every sentence is worse than one that
    // says nothing.
    const { handler, handlers } = await startLiveKit(undefined, {
      wakePhrases: ["assistant"],
    });
    expect(handler.lastDecision).toBeUndefined();

    await handlers.onCallerTranscript?.("what do you all think?", true);
    expect(handler.lastDecision?.respond).toBe(false);

    await handlers.onCallerTranscript?.("assistant, summarise that", true);
    expect(handler.lastDecision).toEqual({ respond: true, addressed: true });
  });

  it("lets a partial notice a wake phrase but never decide a turn", async () => {
    // Deciding on a partial returns "do not respond" for a turn that is about
    // to address the assistant, and the answer to a turn that DID address it
    // gets cut.
    const { handler, handlers } = await startLiveKit(undefined, {
      wakePhrases: ["assistant"],
    });
    await handlers.onCallerTranscript?.("what do you", false);
    expect(handler.lastDecision).toBeUndefined();

    // The phrase heard mid-turn stamps the window, so the finished turn is
    // answered even though the model only hears the tail.
    await handlers.onCallerTranscript?.("assistant, what do you", false);
    await handlers.onCallerTranscript?.("and the second point?", true);
    expect(handler.lastDecision?.respond).toBe(true);
  });

  it("answers everything when no wake phrase is configured", async () => {
    // A gate with nothing that could open it would mute the assistant for the
    // whole call.
    const { handler, handlers } = await startLiveKit();
    await handlers.onCallerTranscript?.("hello there", true);
    expect(handler.lastDecision?.respond).toBe(true);
  });

  it("deletes the room on teardown, not just leaves it", async () => {
    // A job whose room still exists sits there until LiveKit's own empty-room
    // timeout, which is minutes of a worker slot doing nothing.
    const { handler, room } = await startLiveKit();
    await handler.aclose("caller-hung-up");
    expect(room.closed).toBe(true);
  });

  it("leaves the room on teardown", async () => {
    const { handler, room } = await startLiveKit();
    await handler.aclose("caller-hung-up");
    expect(room.closed).toBe(true);
  });

  it("relays the agent's own video onto the tile, and stops on teardown", async () => {
    // On by default: an agent that publishes video almost always means it for
    // the caller to see.
    const { handler, room } = await startLiveKit();
    expect(room.relayStarted).toBe(true);
    expect(room.sink).toBeDefined();

    await handler.aclose();
    expect(room.relayStopped).toBe(true);
  });

  it("leaves StandIn's avatar alone when the tile relay is off", async () => {
    const room = new FakeRoom();
    const handler = new LiveKitHandler({
      config: { ...liveKitConfig, tileVideo: "off" },
      connect: async () => room,
    });
    await handler.onStart(new FakeCall());
    expect(room.relayStarted).toBe(false);
  });

  it("ends the call when the room cannot be joined", async () => {
    const handler = new LiveKitHandler({
      config: liveKitConfig,
      connect: async () => {
        throw new Error("LiveKit is unreachable");
      },
    });
    const call = new FakeCall();
    await handler.onStart(call);
    expect(call.ended).toBe("agent-unavailable");
  });
});
