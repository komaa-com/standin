// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Who is allowed to call the agent.
 *
 * StandIn authenticates the SOCKET, not the human: the HMAC handshake proves the
 * call came from StandIn, and `session.start` then names a caller. Deciding
 * whether that caller may be answered is the deployment's business, so it lives
 * here rather than in the SDK.
 *
 * Framework-neutral on purpose: nothing here imports OpenClaw, so the policy can
 * be read, tested and reused without a gateway in the process.
 */

/** Normalize a phone number to digits only. */
export function normalizePhoneNumber(input?: string): string {
  if (!input) {
    return "";
  }
  return input.replace(/\D/g, "");
}

/**
 * True when the caller matches an allowlist entry - by phone number (digits
 * only) OR by exact caller id, case-insensitive.
 *
 * The id match is what lets a Microsoft Teams caller be allowlisted at all: their id is an
 * AAD object id, not a phone number, so phone normalization would reduce it to
 * the empty string and match nothing.
 */
export function isAllowlistedCaller(
  from: string | undefined,
  allowFrom: string[] | undefined,
): boolean {
  const raw = from?.trim();
  if (!raw) {
    return false;
  }
  const idFrom = raw.toLowerCase();
  const normalizedFrom = normalizePhoneNumber(raw);
  return (allowFrom ?? []).some((entry) => {
    const trimmed = entry.trim();
    if (!trimmed) {
      return false;
    }
    // Exact caller-id match (e.g. a Microsoft Teams AAD object id), case-insensitive.
    if (trimmed.toLowerCase() === idFrom) {
      return true;
    }
    // Phone-number match (digits only).
    const normalizedAllow = normalizePhoneNumber(trimmed);
    return (
      normalizedAllow !== "" &&
      normalizedFrom !== "" &&
      normalizedAllow === normalizedFrom
    );
  });
}

/**
 * Inbound-policy decision. `from` is the caller's AAD object id for a Microsoft Teams call.
 *
 * An unset or unknown policy REFUSES. Defaulting the other way would mean a
 * config typo silently opens the agent to anyone who can reach the number.
 */
export function isInboundCallAllowed(
  inboundPolicy: "disabled" | "allowlist" | "pairing" | "open" | undefined,
  allowFrom: string[] | undefined,
  from: string | undefined,
): boolean {
  switch (inboundPolicy) {
    case "open":
      return true;
    case "allowlist":
    case "pairing":
      return isAllowlistedCaller(from, allowFrom);
    default:
      return false;
  }
}

/**
 * An actionable log line for a caller the policy just refused.
 *
 * "pairing" is enforced as a plain allowlist here: no pairing codes, expirations
 * or approval prompts are issued for calls, so the operator's fix is the same as
 * under "allowlist".
 */
export function describeInboundRejection(
  inboundPolicy: "disabled" | "allowlist" | "pairing" | "open" | undefined,
  from: string | undefined,
): string {
  const policy = inboundPolicy ?? "disabled";
  const caller = from?.trim()
    ? `caller "${from.trim()}"`
    : "caller with no caller id";
  if (policy === "disabled" || policy === "open") {
    // "open" never rejects; kept for exhaustiveness if callers reuse this.
    return (
      `inbound call rejected by policy "${policy}": inbound calling is ` +
      (policy === "disabled"
        ? 'disabled - set inboundPolicy to "allowlist" and add callers to allowFrom to accept calls'
        : "open") +
      ` (${caller})`
    );
  }
  const pairingHint =
    policy === "pairing"
      ? ' Note: "pairing" currently enforces a plain allowlist (this plugin issues no pairing codes' +
        " or approvals for calls) - add the caller's AAD object id to allowFrom."
      : " Add the caller's AAD object id (or phone number) to allowFrom to accept them.";
  return `inbound call rejected by policy "${policy}": ${caller} is not in allowFrom.${pairingHint}`;
}
