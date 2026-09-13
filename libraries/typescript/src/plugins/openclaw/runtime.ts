// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The plugin's background service: one call listener, shared by every call.
 *
 * OpenClaw starts this on boot and stops it on shutdown or reload, so it is the
 * teardown hook. It is short because the WebSocket server, the handshake, the
 * replay guard, the frame loop and the watchdogs all belong to the SDK's
 * `CallServer`. What is left is the two things the SDK cannot know: which
 * realtime provider the host resolved, and how many calls this operator agreed
 * to take at once.
 *
 * Two frictions with the SDK's defaults are handled here on purpose, both by
 * passing the value explicitly rather than letting the default win:
 *
 * - `CallServer` reads `STANDIN_SECRET` from the environment. The secret arrives
 *   through `api.pluginConfig`, having been resolved by the host from a secret
 *   reference, so it is passed in. An env var would silently override a portal
 *   value the operator can actually see.
 * - `CallServer` binds 0.0.0.0, which suits a worker behind an ingress but not a
 *   gateway reached through a local tunnel. Neither is wrong, so `bindAddress` is
 *   passed when the operator set it and the SDK's default stands when they did not.
 */

import { CallServer, ChatChannel, PersonalChats } from "../../index.js";
import type { OpenClawConfig } from "openclaw/plugin-sdk/config-contracts";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import {
  consultRealtimeVoiceAgent,
  resolveConfiguredRealtimeVoiceProvider,
} from "openclaw/plugin-sdk/realtime-voice";

import type { ResolvedPluginConfig } from "./config.js";
import {
  TeamsCallHandler,
  type CallRegistry,
  type ResolvedRealtime,
} from "./handler.js";
import { drainMeetingRecap } from "./recap.js";
import type { RealtimeCall } from "./realtime.js";

/**
 * Live calls, and the operator's concurrency cap.
 *
 * A slot is reserved BEFORE the model session is built, so two calls arriving
 * together cannot both pass the check and then both open a provider socket. A
 * reserved-but-unbound entry is a call still in `onStart`.
 */
class LiveCalls implements CallRegistry {
  readonly #max: number;
  readonly #calls = new Map<string, RealtimeCall | undefined>();

  constructor(max: number) {
    this.#max = max;
  }

  get size(): number {
    return this.#calls.size;
  }

  acquire(callId: string): boolean {
    if (this.#calls.size >= this.#max) return false;
    this.#calls.set(callId, undefined);
    return true;
  }

  bind(callId: string, call: RealtimeCall): void {
    // Only if the slot is still ours: a teardown that raced onStart has already
    // released it, and re-adding here would leak the slot for the worker's life.
    if (this.#calls.has(callId)) this.#calls.set(callId, call);
  }

  release(callId: string): void {
    this.#calls.delete(callId);
  }

  /** Close every live call. Used by shutdown, before the listener goes away. */
  closeAll(): void {
    for (const call of this.#calls.values()) call?.close();
    this.#calls.clear();
  }
}

/** The service OpenClaw starts and stops. One per gateway process. */
export class StandInCallRuntime {
  readonly #api: OpenClawPluginApi;
  readonly #config: ResolvedPluginConfig;
  readonly #calls: LiveCalls;
  #server: CallServer | undefined;
  #realtime: ResolvedRealtime | undefined;
  #chat: ChatChannel | undefined;
  #chats: PersonalChats | undefined;

  constructor(api: OpenClawPluginApi, config: ResolvedPluginConfig) {
    this.#api = api;
    this.#config = config;
    this.#calls = new LiveCalls(config.media.maxConcurrentCalls);
  }

  /** Live calls right now. Useful to a health check, and to a test. */
  get activeCalls(): number {
    return this.#calls.size;
  }

  async start(): Promise<void> {
    const log = this.#api.logger;
    this.#realtime = this.#resolveRealtime();

    // Fail LOUD, not closed. A gateway that boots healthy and then refuses every
    // call with "realtime-unavailable" is the single most expensive way to
    // discover a missing API key, so name the configured provider here where
    // someone is reading the log, rather than only on the first call that fails.
    if (!this.#realtime) {
      const providerId = this.#config.voice.realtime.provider;
      log.warn(
        "standin-msteams: no realtime voice provider resolved" +
          (providerId
            ? ` (configured provider "${providerId}" has no usable credentials)`
            : " (no realtime provider configured)") +
          '. Every call will be refused with "realtime-unavailable". Set the provider API key.',
      );
    }

    if (this.#config.voice.meetingRecap) {
      this.#chats = new PersonalChats();
      try {
        // Listen-only: this lane exists to POST minutes, and to remember who has
        // a 1:1 chat with the bot so a personal call has somewhere to post them.
        // It answers nothing. A canned reply here would compete with whatever
        // already answers this connection's chat and teach people the bot is
        // deaf.
        const chat = new ChatChannel({
          secret: this.#config.media.secret,
          chats: this.#chats,
          listenOnly: true,
          respond: async () => "",
        });
        await chat.start();
        this.#chat = chat;
        log.info(
          "standin-msteams: chat lane open for meeting recap (posts minutes, answers nothing)",
        );
        void drainMeetingRecap({
          chat,
          chats: this.#chats,
          summarise: (key, prompt) => this.#summarise(key, prompt),
          logger: log,
        });
      } catch (err) {
        log.warn(
          `standin-msteams: chat lane for recap did not open - ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }

    const server = new CallServer({
      handlerFactory: () =>
        new TeamsCallHandler({
          config: this.#config,
          realtime: this.#realtime,
          cfg: this.#api.config as unknown as OpenClawConfig,
          registry: this.#calls,
          logger: log,
          chat: this.#chat,
          chats: this.#chats,
          consult: (key, prompt) => this.#summarise(key, prompt),
        }),
      secret: this.#config.media.secret,
      port: this.#config.media.port,
      wsPath: this.#config.media.path,
      maxConnections: this.#config.media.maxConcurrentCalls,
      ...(this.#config.media.bindAddress
        ? { host: this.#config.media.bindAddress }
        : {}),
    });

    await server.start();
    this.#server = server;
    log.info(
      `standin-msteams: listening on ${this.#config.media.bindAddress ?? "0.0.0.0"}:${server.port}${server.wsPath}` +
        ` (max ${this.#config.media.maxConcurrentCalls} concurrent)`,
    );
  }

  /**
   * Close live calls first, then the listener.
   *
   * That order is not cosmetic: the SDK's teardown frees a call's slot only once
   * its close settles, so tearing the listener down over live calls is how a
   * shutdown ends up waiting on sockets nobody is reading any more.
   */
  async stop(): Promise<void> {
    this.#calls.closeAll();
    const server = this.#server;
    this.#server = undefined;
    if (server) await server.aclose();
    const chat = this.#chat;
    this.#chat = undefined;
    this.#chats = undefined;
    if (chat) await chat.aclose();
    this.#api.logger.info("standin-msteams: stopped");
  }

  /**
   * Ask the host's text agent to write minutes. Session scope is the consult
   * key, so per-aad / per-thread memory carries across calls.
   *
   * Returns "" when the agent produced nothing or failed, and postMinutes then
   * posts nothing. The raw transcript is NOT a fallback: a misconfigured agent
   * must not end every call with a verbatim dump of the whole conversation in
   * the meeting thread.
   */
  async #summarise(sessionKey: string, prompt: string): Promise<string> {
    try {
      const result = await consultRealtimeVoiceAgent({
        cfg: this.#api.config as unknown as OpenClawConfig,
        agentRuntime: this.#api.runtime.agent,
        logger: this.#api.logger,
        sessionKey,
        messageProvider: "standin-msteams",
        lane: "voice",
        runIdPrefix: "standin-recap",
        args: { question: prompt },
        transcript: [],
        surface: "microsoft-teams",
        userLabel: "Caller",
        assistantLabel: "Assistant",
        extraSystemPrompt: "Output only the meeting minutes, briefly and factually.",
        fallbackText: "",
      });
      return (result.text ?? "").trim();
    } catch (err) {
      this.#api.logger.warn(
        `standin-msteams: recap consult failed - ${err instanceof Error ? err.message : String(err)}`,
      );
      return "";
    }
  }

  /**
   * Ask the host which realtime provider is configured.
   *
   * Returns undefined rather than throwing on ANY failure, including a throw from
   * the host resolver: a provider that cannot be resolved is a call that gets
   * refused, not a gateway that will not boot. The plugin is one service among
   * many in that process.
   */
  #resolveRealtime(): ResolvedRealtime | undefined {
    try {
      const resolved = resolveConfiguredRealtimeVoiceProvider({
        configuredProviderId: this.#config.voice.realtime.provider,
        providerConfigs: this.#config.voice.realtime.providers,
        cfg: this.#api.config as unknown as OpenClawConfig,
      }) as Partial<ResolvedRealtime> | undefined;
      if (!resolved?.provider || !resolved.providerConfig) return undefined;
      return {
        provider: resolved.provider,
        providerConfig: resolved.providerConfig,
      };
    } catch (err) {
      this.#api.logger.warn(
        `standin-msteams: realtime provider resolution failed - ${err instanceof Error ? err.message : String(err)}`,
      );
      return undefined;
    }
  }
}
