// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * `MEDIA:` markers, the convention an agent uses to send a picture.
 *
 * Some agent frameworks let a reply name a file by writing a line like
 * `MEDIA:/tmp/chart.png` and expect the channel to attach it. A channel that
 * does not understand the convention posts that line as prose, and a caller
 * reads a temporary file path in their chat. On a call it is worse:
 * text-to-speech reads the path out, character by character.
 *
 * So the marker is an instruction to the channel, not something anyone should
 * see. {@link parseMedia} takes it out of the text and hands back what it
 * referred to. {@link loadMedia} turns one reference into bytes, through the
 * same guards everything else in this SDK uses.
 *
 * A local path is read only from a directory an operator named. Default: none.
 * An agent can be talked into writing `MEDIA:/etc/passwd`, and the answer to
 * that has to be a refusal rather than a file read followed by an upload into
 * somebody's chat.
 *
 * Identical in shape to the Python SDK's `standin.media`.
 */

import { realpathSync, statSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { delimiter, resolve, sep, basename } from "node:path";

import {
  OUTBOUND_IMAGE_MAX_BYTES,
  outboundImage,
  sniffImageType,
  type OutboundImage,
} from "./chat.js";
import { fetchPublicImage } from "./fetch.js";
import { logger } from "./log.js";

/** Where a local reference may be read from. Nothing until an operator says so. */
export const MEDIA_ROOTS_ENV = "STANDIN_MEDIA_ROOTS";

/**
 * The marker, as the convention defines it: case-insensitive, one per line, and
 * anchored to the END of the line so a path with spaces survives intact.
 *
 * Used only through `replace`, which resets `lastIndex`. A shared global regex
 * driven with `test` or `exec` carries state between calls and skips matches.
 */
const MARKER = /\bMEDIA:\s*`?([^\n]+?)`?\s*$/gim;

/** How long a reference has to look like one before its line is removed. */
const PATH_STARTS = ["http://", "https://", "/", "./", "../", "~/"];
const MEDIA_SUFFIXES = [".png", ".jpg", ".jpeg", ".gif", ".webp"];

/** A URL reference has the same budget as the picture it becomes. */
const FETCH_TIMEOUT_MS = 10_000;

/** A reply with its markers taken out, and what they pointed at. */
export interface AgentMedia {
  /** What is safe to post and to say out loud. */
  readonly text: string;
  /** What the markers referred to, in the order they were written. */
  readonly refs: string[];
}

/**
 * Whether this is plausibly a file or a URL.
 *
 * A deliberate narrowing. Stripping every line that merely begins with `MEDIA:`
 * eats a sentence like "MEDIA: we should talk to them" out of the answer, and
 * the person who wrote it never learns why.
 */
function looksLikeRef(ref: string): boolean {
  const lowered = ref.toLowerCase();
  if (PATH_STARTS.some((start) => lowered.startsWith(start))) return true;
  if (/^[a-z]:[\\/]/.test(lowered)) return true; // a Windows drive
  return MEDIA_SUFFIXES.some((suffix) => lowered.endsWith(suffix));
}

/**
 * Take the markers out of a reply and return them separately.
 *
 * The text that comes back is what to post AND what to say. Both, always: the
 * whole point is that nobody sees or hears the marker.
 */
export function parseMedia(reply: string): AgentMedia {
  const refs: string[] = [];
  let text = (reply ?? "").replace(MARKER, (whole, captured: string) => {
    const ref = captured.trim().replace(/^`|`$/g, "").trim();
    if (ref === "" || !looksLikeRef(ref)) {
      // Not a reference, so it was prose. Leave it alone.
      return whole;
    }
    refs.push(ref);
    return "";
  });
  // Removing a line leaves the blank line it sat on. Three or more become two,
  // which is a paragraph break; two are left alone, because they already are.
  text = text.replace(/\n{3,}/g, "\n\n").trim();
  return { text, refs };
}

/**
 * Directories a local reference may be read from.
 *
 * Empty by default, which makes local references unavailable until somebody
 * opts in. That is the right default for a path chosen by a model.
 */
export function mediaRoots(roots?: readonly string[]): string[] {
  const parts =
    roots ??
    (process.env[MEDIA_ROOTS_ENV] ?? "")
      .split(delimiter)
      .filter((p) => p.trim() !== "");
  const out: string[] = [];
  for (const part of parts) {
    try {
      out.push(realpathSync(expand(part)));
    } catch {
      // A root that does not exist cannot contain anything.
    }
  }
  return out;
}

function expand(path: string): string {
  return path.startsWith("~/") ? resolve(homedir(), path.slice(2)) : path;
}

/**
 * Whether a real path sits under one of these real roots.
 *
 * Both sides are resolved through symlinks first, and the comparison is
 * separator-terminated: without that, `/tmp/rootevil` passes for `/tmp/root`.
 */
function inside(path: string, roots: readonly string[]): boolean {
  let real: string;
  try {
    real = realpathSync(path);
  } catch {
    return false;
  }
  return roots.some((root) => {
    const prefix = root.replace(new RegExp(`${sep}+$`), "") + sep;
    return real === root || real.startsWith(prefix);
  });
}

/** Options for {@link loadMedia}. */
export interface LoadMediaOptions {
  roots?: readonly string[];
  maxBytes?: number;
  name?: string;
}

/**
 * Turn one reference into a picture ready to send.
 *
 * Throws with something worth reading, because the caller is on a path where
 * the alternative is a dropped answer.
 *
 * A URL goes through the SDK's own guard, so a reference pointed at a private
 * address is refused rather than fetched. A path is read only from a named root.
 * Everything else is refused by name: `file://` handed to a URL fetcher is the
 * usual way around a path guard.
 */
export async function loadMedia(
  ref: string,
  options: LoadMediaOptions = {},
): Promise<OutboundImage> {
  const target = (ref ?? "").trim();
  if (target === "") throw new Error("there was nothing to send");
  const maxBytes = options.maxBytes ?? OUTBOUND_IMAGE_MAX_BYTES;

  const lowered = target.toLowerCase();
  let data: Buffer;
  let declared = "";
  let suggested: string;

  if (lowered.startsWith("http://") || lowered.startsWith("https://")) {
    const fetched = await fetchPublicImage(target, maxBytes, FETCH_TIMEOUT_MS);
    data = fetched.bytes;
    declared = fetched.mime;
    suggested = options.name ?? basename(target.split("?")[0]!);
  } else if (lowered.includes("://") || lowered.startsWith("data:")) {
    throw new Error(`${lowered.split(":")[0]} references are not allowed here`);
  } else {
    const allowed = mediaRoots(options.roots);
    if (allowed.length === 0) {
      throw new Error(
        `sending a local file is off until a directory is named in ${MEDIA_ROOTS_ENV}`,
      );
    }
    const path = expand(target);
    if (!inside(path, allowed)) {
      throw new Error(
        "that file is outside the directories this worker may read",
      );
    }
    let size: number;
    try {
      // Checked before it is read: the point of a cap is not to load it.
      size = statSync(path).size;
    } catch {
      throw new Error("no such file");
    }
    if (size > maxBytes)
      throw new Error(`that file is ${size} bytes, over the ${maxBytes} limit`);
    data = readFileSync(path);
    suggested = options.name ?? basename(path);
  }

  // Re-checked here whatever anything upstream reported. A content type from a
  // response header or a file extension is a claim; the bytes are the fact.
  const actual = sniffImageType(data);
  if (actual === undefined)
    throw new Error("that file is not a picture this can send");
  if (
    declared !== "" &&
    declared !== "application/octet-stream" &&
    declared !== actual
  ) {
    throw new Error(
      `that was served as ${declared} but the bytes are ${actual}`,
    );
  }
  logger.info(
    `standin: attaching ${suggested || "a picture"} from a MEDIA marker`,
  );
  return outboundImage(data, actual, suggested);
}
