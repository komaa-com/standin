// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** Every error this SDK raises. Mirrors the Python SDK's `StandInError`. */
export class StandInError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StandInError";
  }
}
