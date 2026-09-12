// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The StandIn plugin for OpenClaw: your agent, in a real Microsoft Teams call.
 *
 * This is not a worker you run. It is an OpenClaw plugin, and it has to be: it
 * consumes the host's realtime speech-to-speech session, its provider registry
 * and its logger, all of which are in-process objects inside the gateway.
 * OpenClaw's external HTTP surface is text-only, so a standalone worker could only
 * use OpenClaw as a text brain and bring its own STT and TTS - which is exactly
 * the part this plugin exists to avoid.
 *
 * It lives inside `@komaa/standin-sdk` rather than in a package of its own, and
 * nothing in the SDK core imports it, so an install that never touches OpenClaw
 * never loads this file. The `openclaw` imports below are therefore at module
 * scope on purpose: by the time anything reaches here, the host IS the process.
 *
 * OpenClaw does not load a plugin by package specifier. It is given a DIRECTORY
 * through `plugins.load.paths`, so the built form of this file ships as a
 * self-describing directory inside the published package:
 *
 * ```jsonc
 * // openclaw.json
 * { "plugins": {
 *     "load": { "paths": ["node_modules/@komaa/standin-sdk/dist/plugins/openclaw"] },
 *     "entries": { "standin-msteams": { "enabled": true, "config": {
 *       "secret": "sk_standin_...",
 *       "inboundPolicy": "allowlist",
 *       "allowFrom": ["<caller AAD object id>"],
 *       "realtime": { "provider": "openai", "providers": { "openai": { "apiKey": "sk-..." } } }
 *     } } }
 * } }
 * ```
 *
 * `openclaw.plugin.json` and the directory's own `package.json` are placed beside
 * the emitted `index.js` by `scripts/emit-openclaw-plugin-dir.mjs`, because `tsc`
 * emits only `.ts` and a manifest one level up is a manifest the host never sees.
 *
 * The plugin registers ONE host-managed background service, so OpenClaw's own
 * lifecycle starts the call listener at boot and tears it down on shutdown or
 * reload. There is nothing to supervise separately.
 */

import {
  definePluginEntry,
  type OpenClawPluginDefinition,
} from "openclaw/plugin-sdk/core";

import { resolvePluginConfig } from "./config.js";
import { StandInCallRuntime } from "./runtime.js";

export { resolvePluginConfig, type ResolvedPluginConfig } from "./config.js";
export {
  TeamsCallHandler,
  type CallRegistry,
  type HandlerDeps,
} from "./handler.js";
export {
  createRealtimeCall,
  type RealtimeCall,
  type RealtimeCallDeps,
} from "./realtime.js";
export { StandInCallRuntime } from "./runtime.js";

/**
 * The entry OpenClaw loads.
 *
 * Annotated rather than inferred: the inferred type reaches into an internal
 * OpenClaw declaration file whose name is a build hash, and a `.d.ts` that names
 * it would not survive that package being rebuilt.
 */
const entry: OpenClawPluginDefinition = definePluginEntry({
  id: "standin-msteams",
  name: "Microsoft Teams calls by StandIn",
  description:
    "Put your OpenClaw agent in a real Microsoft Teams call, through StandIn.",
  register(api) {
    const config = resolvePluginConfig(
      (api as { pluginConfig?: unknown }).pluginConfig,
    );
    if (!config.enabled) return;

    // Fail CLOSED on the secret. Every socket StandIn dials is HMAC-authenticated
    // with it, so starting without one would mean either refusing every call or,
    // worse, accepting anything - and `CallServer` rightly throws rather than
    // choose. Say why here, where an operator is reading the log, instead of
    // letting the service registration throw during boot.
    if (!config.media.secret) {
      api.logger.warn(
        "standin-msteams: no secret configured - set `secret` to the value the StandIn portal shows you. Nothing started.",
      );
      return;
    }

    let runtime: StandInCallRuntime | undefined;

    api.registerService({
      id: "standin-msteams",
      start: async () => {
        runtime = new StandInCallRuntime(api, config);
        await runtime.start();
      },
      stop: async () => {
        await runtime?.stop();
        runtime = undefined;
      },
    });
  },
});

export default entry;
