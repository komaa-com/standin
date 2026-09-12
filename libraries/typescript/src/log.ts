// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The SDK's log sink.
 *
 * Console by default, because a library that silently swallows "this worker
 * takes no Microsoft Teams calls" is a library people debug for an hour. Replace it with
 * `setLogger()` to route into your own framework's logger - the Python SDK does
 * the same thing through the stdlib `logging` tree.
 */
export interface Logger {
  debug(message: string): void;
  info(message: string): void;
  warn(message: string): void;
  error(message: string): void;
}

const consoleLogger: Logger = {
  debug: (m) => console.debug(m),
  info: (m) => console.info(m),
  warn: (m) => console.warn(m),
  error: (m) => console.error(m),
};

let active: Logger = consoleLogger;

/** Route SDK logs into your own logger. */
export function setLogger(next: Logger): void {
  active = next;
}

export const logger: Logger = {
  debug: (m) => active.debug(m),
  info: (m) => active.info(m),
  warn: (m) => active.warn(m),
  error: (m) => active.error(m),
};
