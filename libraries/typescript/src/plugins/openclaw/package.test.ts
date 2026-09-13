// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * The built plugin directory, checked against the installed OpenClaw host.
 *
 * OpenClaw is given a DIRECTORY through `plugins.load.paths` and discovers what
 * is inside it, so the thing under test is `dist/plugins/openclaw/`, not
 * this package's root. That directory is assembled by `tsc` plus
 * `scripts/emit-openclaw-plugin-dir.mjs`, which means this test only passes
 * after a build - deliberately, because a load path that is not built is exactly
 * the failure an operator would otherwise hit at gateway boot.
 *
 * The documented load path is:
 *
 *     node_modules/@komaa/standin-sdk/dist/plugins/openclaw
 */

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { describe, expect, it } from "vitest";

const packageRoot = fileURLToPath(new URL("../../..", import.meta.url));
const pluginDir = join(packageRoot, "dist", "plugins", "openclaw");
const require = createRequire(import.meta.url);
const hostDist = resolve(
  dirname(require.resolve("openclaw/plugin-sdk/core")),
  "..",
);

// OpenClaw's package/manifest validators are internal build chunks. Locate the
// installed implementation by its named function, since chunk hashes and
// exported aliases change on every upstream build. Do not reproduce its rules.
const manifestFile = readdirSync(hostDist).find(
  (name) =>
    /^manifest-[\w-]+\.js$/.test(name) &&
    readFileSync(join(hostDist, name), "utf8").includes(
      "function resolvePackageExtensionEntries(",
    ),
);
if (!manifestFile)
  throw new Error("Installed OpenClaw package validators could not be located");
const hostExports = await import(
  pathToFileURL(join(hostDist, manifestFile)).href
);

function hostFunction(name: string): (...args: unknown[]) => any {
  const fn = Object.values(hostExports).find(
    (value) => typeof value === "function" && value.name === name,
  );
  if (typeof fn !== "function")
    throw new Error(`Installed OpenClaw validator ${name} is missing`);
  return fn as (...args: unknown[]) => any;
}

const resolveEntries = hostFunction("resolvePackageExtensionEntries");
const loadManifest = hostFunction("loadPluginManifest");

describe("built plugin directory, against the installed OpenClaw host", () => {
  it("accepts the entry declaration and finds its built runtime", () => {
    const declaration = JSON.parse(
      readFileSync(join(pluginDir, "package.json"), "utf8"),
    );
    const result = resolveEntries(declaration);
    expect(result).toEqual({ status: "ok", entries: ["./index.js"] });
    for (const entry of result.entries)
      expect(existsSync(join(pluginDir, entry))).toBe(true);
  });

  it("loads the manifest used for plugin discovery", () => {
    const result = loadManifest(pluginDir);
    expect(result.ok).toBe(true);
    expect(result.manifest.id).toBe("standin-msteams");
    expect(result.manifest.activation.onStartup).toBe(true);
  });

  it("ships the documented load path in the published package layout", () => {
    const pkg = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));
    expect(pkg.files).toEqual(expect.arrayContaining(["dist"]));
    expect(existsSync(join(pluginDir, "openclaw.plugin.json"))).toBe(true);
    expect(existsSync(join(pluginDir, "index.js"))).toBe(true);
    expect(existsSync(join(pluginDir, "package.json"))).toBe(true);
  });
});
