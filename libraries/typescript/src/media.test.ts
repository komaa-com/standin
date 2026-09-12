// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The MEDIA: marker convention.
 *
 * A marker is an instruction to the channel. A channel that does not understand
 * it posts a temporary file path into somebody's chat, or reads it out loud
 * character by character, which is the bug this exists to prevent.
 *
 * The Python twin is `tests/test_media.py`.
 */

import { mkdtempSync, mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

import { describe, expect, it } from "vitest";

import { MEDIA_ROOTS_ENV, loadMedia, mediaRoots, parseMedia } from "./media.js";

const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.alloc(20),
]);

function scratch(): string {
  return mkdtempSync(join(tmpdir(), "standin-media-"));
}

describe("taking a marker out of a reply", () => {
  it("removes it from the text", () => {
    const got = parseMedia("Here is the chart.\nMEDIA:/tmp/chart.png");
    expect(got.text).toBe("Here is the chart.");
    expect(got.refs).toEqual(["/tmp/chart.png"]);
  });

  it("keeps a path with spaces intact", () => {
    // The documented form is backticked precisely because paths have spaces.
    // Splitting on whitespace truncates them at the first one.
    expect(parseMedia("MEDIA:`/tmp/a b.png`").refs).toEqual(["/tmp/a b.png"]);
  });

  it("leaves prose that merely starts with the word alone", () => {
    // Stripping every such line eats a sentence out of the answer, and whoever
    // wrote it never learns why.
    const text = "MEDIA: we should talk to them about it";
    const got = parseMedia(text);
    expect(got.text).toBe(text);
    expect(got.refs).toEqual([]);
  });

  it("collapses the blank line a marker leaves", () => {
    expect(parseMedia("one\n\n\n\nMEDIA:/tmp/x.png\n\n\n\ntwo").text).toBe(
      "one\n\ntwo",
    );
  });

  it("keeps a deliberate paragraph break", () => {
    expect(parseMedia("one\n\ntwo").text).toBe("one\n\ntwo");
  });

  it("keeps the order they were written in", () => {
    expect(parseMedia("MEDIA:/tmp/a.png\nMEDIA:/tmp/b.png").refs).toEqual([
      "/tmp/a.png",
      "/tmp/b.png",
    ]);
  });

  it.each(["media:/tmp/x.png", "MEDIA:/tmp/x.png", "Media: /tmp/x.png"])(
    "is case insensitive (%s)",
    (marker) => {
      expect(parseMedia(marker).refs).toEqual(["/tmp/x.png"]);
    },
  );

  it("leaves a reply with no marker unchanged", () => {
    expect(parseMedia("just an answer").text).toBe("just an answer");
  });
});

describe("loading what a marker referred to", () => {
  it("sends a file inside a named root", async () => {
    const root = scratch();
    const png = join(root, "chart.png");
    writeFileSync(png, PNG);
    const image = await loadMedia(png, { roots: [root] });
    expect(image.contentType).toBe("image/png");
    expect(image.name).toBe("chart.png");
  });

  it("is off until a directory is named", async () => {
    const before = process.env[MEDIA_ROOTS_ENV];
    delete process.env[MEDIA_ROOTS_ENV];
    try {
      const root = scratch();
      const png = join(root, "chart.png");
      writeFileSync(png, PNG);
      await expect(loadMedia(png)).rejects.toThrow(MEDIA_ROOTS_ENV);
    } finally {
      if (before !== undefined) process.env[MEDIA_ROOTS_ENV] = before;
    }
  });

  it("refuses a file outside the roots", async () => {
    // An agent talked into naming a private file must get a refusal, not a read
    // followed by an upload into somebody's chat.
    const base = scratch();
    const root = join(base, "shared");
    mkdirSync(root);
    const outside = join(base, "secret.png");
    writeFileSync(outside, PNG);
    await expect(loadMedia(outside, { roots: [root] })).rejects.toThrow(
      /outside the directories/,
    );
  });

  it("judges a symlink by where it lands", async () => {
    const base = scratch();
    const root = join(base, "shared");
    mkdirSync(root);
    const secret = join(base, "secret.png");
    writeFileSync(secret, PNG);
    symlinkSync(secret, join(root, "innocent.png"));
    await expect(
      loadMedia(join(root, "innocent.png"), { roots: [root] }),
    ).rejects.toThrow(/outside the directories/);
  });

  it("does not let a sibling directory pass for the root", async () => {
    // Without a separator-terminated compare, /tmp/rootevil passes for /tmp/root.
    const base = scratch();
    const root = join(base, "root");
    const evil = join(base, "rootevil");
    mkdirSync(root);
    mkdirSync(evil);
    const png = join(evil, "x.png");
    writeFileSync(png, PNG);
    await expect(loadMedia(png, { roots: [root] })).rejects.toThrow(
      /outside the directories/,
    );
  });

  it.each(["file:///etc/passwd", "data:image/png;base64,AAAA", "s3://b/k"])(
    "refuses another scheme by name (%s)",
    async (ref) => {
      // A file:// URL handed to a URL fetcher is the usual way round a path guard.
      await expect(loadMedia(ref, { roots: [scratch()] })).rejects.toThrow(
        /not allowed here/,
      );
    },
  );

  it("refuses a file that is not a picture", async () => {
    const root = scratch();
    const path = join(root, "notes.txt");
    writeFileSync(path, "hello");
    await expect(loadMedia(path, { roots: [root] })).rejects.toThrow(
      /not a picture/,
    );
  });

  it("refuses an oversized file before reading it", async () => {
    const root = scratch();
    const path = join(root, "big.png");
    writeFileSync(path, Buffer.concat([PNG, Buffer.alloc(4096)]));
    await expect(
      loadMedia(path, { roots: [root], maxBytes: 64 }),
    ).rejects.toThrow(/over the/);
  });

  it("says so when there is nothing to send", async () => {
    await expect(loadMedia("   ", { roots: [scratch()] })).rejects.toThrow(
      /nothing to send/,
    );
  });

  it("reads the roots from the environment", () => {
    const root = scratch();
    const before = process.env[MEDIA_ROOTS_ENV];
    process.env[MEDIA_ROOTS_ENV] = `${root}${delimiter}${join(root, "gone")}`;
    try {
      // The one that does not exist cannot contain anything, so it is dropped.
      expect(mediaRoots()).toHaveLength(1);
    } finally {
      if (before === undefined) delete process.env[MEDIA_ROOTS_ENV];
      else process.env[MEDIA_ROOTS_ENV] = before;
    }
  });

  it("has no roots by default", () => {
    const before = process.env[MEDIA_ROOTS_ENV];
    delete process.env[MEDIA_ROOTS_ENV];
    try {
      expect(mediaRoots()).toEqual([]);
    } finally {
      if (before !== undefined) process.env[MEDIA_ROOTS_ENV] = before;
    }
  });
});
