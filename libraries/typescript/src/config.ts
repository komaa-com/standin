// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Reading configuration, and failing usefully when it is wrong.
 *
 * Every plugin needs the same four things: a value that must be set, one that
 * may be, a boolean, and a check that a vendor host is really that vendor's.
 * Each one had written its own, so the error a user saw for a missing key
 * depended on which provider they happened to pick.
 *
 * The host check is the one worth reading twice. Your API key travels to
 * whatever host the configuration names, so a mistyped or injected host is not a
 * failed call, it is credential exfiltration.
 *
 * Identical in shape to the Python SDK's `standin.config`.
 */

import { StandInError } from "./errors.js";

/**
 * Read a variable that must be set, or throw naming it.
 *
 * `purpose` completes the sentence "X is required to ...", so write it as a verb
 * phrase: `"answer calls with ElevenLabs"`.
 */
export function required(name: string, purpose = ""): string {
  const value = (process.env[name] ?? "").trim();
  if (!value)
    throw new StandInError(
      `${name} is required${purpose ? ` to ${purpose}` : ""}`,
    );
  return value;
}

/** Read a variable that may be set. Blank reads as absent. */
export function optional(name: string, fallback?: string): string | undefined {
  return (process.env[name] ?? "").trim() || fallback;
}

/**
 * Read a boolean. Only `true` is true, so a typo is off rather than on.
 *
 * Deliberately strict. A setting that turns a guard OFF must not be turned off
 * by `TRUE`, `1` or `yes` landing in a config file by accident.
 */
export function flag(name: string, fallback = false): boolean {
  const raw = (process.env[name] ?? "").trim().toLowerCase();
  if (!raw) return fallback;
  return raw === "true";
}

/**
 * Read a host and refuse one that is not the vendor's.
 *
 * Your API key travels to this host. A mistyped or injected value is credential
 * exfiltration rather than a failed call, which is why this throws instead of
 * warning.
 */
export function vendorHost(
  name: string,
  fallback: string,
  suffix: string,
): string {
  const host = (process.env[name] ?? "").trim() || fallback;
  const bare = suffix.replace(/^\./, "");
  if (host !== bare && !host.endsWith(suffix)) {
    throw new StandInError(`${name} must be a ${bare} host, got ${host}`);
  }
  return host;
}

/**
 * Read a JSON object of strings, or throw saying it must be one.
 *
 * Used for header maps, which carry YOUR credentials to somebody else's
 * endpoint, so the value is never logged on the failure path.
 */
export function jsonObject(name: string): Record<string, string> {
  const raw = (process.env[name] ?? "").trim();
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new StandInError(`${name} must be a JSON object`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new StandInError(`${name} must be a JSON object`);
  }
  return Object.fromEntries(
    Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [
      k,
      String(v),
    ]),
  );
}
