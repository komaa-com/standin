// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Kept so this plugin's historical import path still resolves.
 *
 * The caller policy moved into the SDK core as `policy.ts`. It was never
 * OpenClaw-specific: it already said so itself, and every plugin that answers a
 * call has to decide whether this caller may be answered.
 */

export {
  describeInboundRejection,
  isAllowlistedCaller,
  isInboundCallAllowed,
  normalizePhoneNumber,
} from "../../policy.js";
