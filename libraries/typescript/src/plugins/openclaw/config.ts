// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * `api.pluginConfig` in, a typed settings object out.
 *
 * OpenClaw validates the raw config against `openclaw.plugin.json`'s
 * `configSchema` BEFORE this runs and before it resolves any secret reference,
 * so this is a boundary adapter over input that has already been shape-checked
 * but whose secrets may or may not have resolved. Tolerant casts belong here and
 * nowhere else in the plugin.
 */

/** Everything the plugin reads, resolved and defaulted. */
export interface ResolvedPluginConfig {
  /** `enabled: false` turns the plugin off without uninstalling it. */
  enabled: boolean;
  /** The call listener - what {@link CallServer} is constructed with. */
  media: {
    port: number;
    bindAddress: string | undefined;
    path: string;
    /** The StandIn connection secret. Empty means refuse to start. */
    secret: string;
    maxConcurrentCalls: number;
  };
  /** How the agent behaves on a call. */
  voice: {
    inboundPolicy?: "disabled" | "allowlist" | "pairing" | "open";
    allowFrom?: string[];
    /** Spoken on pickup. Omitted means the agent waits for the caller. */
    inboundGreeting?: string;
    /**
     * Hold caller audio back until Microsoft Teams reports recording active.
     *
     * This is a Microsoft Media Access API obligation, not a preference: a bot
     * that processes call media before `updateRecordingStatus` goes active is
     * out of policy. Default OFF because the hosted service can be configured
     * either way, and a gate nobody asked for is a call of silence.
     */
    requireRecordingStatus: boolean;
    realtime: {
      provider?: string;
      providers?: Record<string, Record<string, unknown>>;
      instructions?: string;
      suppressInputDuringPlayback?: boolean;
      echoSuppressionWindowMs?: number;
      echoBargeInRms?: number;
    };
  };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Raw = Record<string, any>;

/**
 * A config value only counts when it is a non-empty STRING.
 *
 * This is the second line of defence on the secret, and it is deliberate. The
 * manifest types `secret` as a string and OpenClaw rejects anything else before
 * this resolver runs - but if an UNRESOLVED secret reference ever did arrive (an
 * env reference whose variable is unset), `String({})` yields the literal
 * "[object Object]": a non-empty, guessable secret that the fail-closed check in
 * the entry point would happily accept. Coercing a non-string to "" makes it fail
 * CLOSED, with the listener refusing to start, instead.
 */
const str = (v: unknown): string => (typeof v === "string" ? v : "");

const asObject = (v: unknown): Raw | undefined =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Raw) : undefined;

/** Resolve the host's raw plugin config. Never throws: an unusable config fails closed. */
export function resolvePluginConfig(rawInput: unknown): ResolvedPluginConfig {
  const c: Raw = (rawInput as Raw) ?? {};
  const r: Raw = asObject(c.realtime) ?? {};
  return {
    enabled: c.enabled !== false,
    media: {
      port: Number(c.callingPort ?? 9442),
      // undefined, not "": CallServer's own default (0.0.0.0) should win when the
      // operator said nothing, and an empty string is not a bind address.
      bindAddress: str(c.bindAddress) || undefined,
      path: str(c.path) || "/msteams/calling",
      secret: str(c.secret),
      maxConcurrentCalls: Number(c.maxConcurrentCalls ?? 4),
    },
    voice: {
      inboundPolicy: c.inboundPolicy,
      allowFrom: Array.isArray(c.allowFrom)
        ? c.allowFrom.map(String)
        : undefined,
      inboundGreeting: str(c.inboundGreeting) || undefined,
      requireRecordingStatus: c.requireRecordingStatus === true,
      realtime: {
        provider: str(r.provider) || undefined,
        providers: asObject(r.providers) as
          Record<string, Record<string, unknown>> | undefined,
        instructions: str(r.instructions) || undefined,
        suppressInputDuringPlayback:
          typeof r.suppressInputDuringPlayback === "boolean"
            ? r.suppressInputDuringPlayback
            : undefined,
        echoSuppressionWindowMs:
          r.echoSuppressionWindowMs === undefined
            ? undefined
            : Number(r.echoSuppressionWindowMs),
        echoBargeInRms:
          r.echoBargeInRms === undefined ? undefined : Number(r.echoBargeInRms),
      },
    },
  };
}
