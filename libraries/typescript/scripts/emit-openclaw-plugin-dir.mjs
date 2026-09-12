// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/**
 * Finish `dist/plugins/openclaw/` so the OpenClaw gateway can load it.
 *
 * OpenClaw does not import a plugin by package specifier. It is given a
 * DIRECTORY through `plugins.load.paths` and discovers what is inside it, so the
 * directory has to be self-describing. `tsc` emits only the `.ts` files, which
 * leaves two things to place here after the compile:
 *
 * - `openclaw.plugin.json`, the plugin manifest. The host reads it from the
 *   directory it was pointed at, which is why it is copied rather than bundled:
 *   a manifest one level up is a manifest the host never sees.
 * - a small `package.json`. The manifest has no field for it, so this file is
 *   the ONLY place the host can read `openclaw.compat.pluginApi`. Without it a
 *   too-old gateway loads the plugin and fails somewhere inside the realtime
 *   bridge; with it, discovery refuses up front and says which API it wanted.
 *   It also names the entry explicitly instead of relying on the host's
 *   `index.js` convention, so a future rename cannot silently stop loading.
 *
 * `"type": "module"` is not decoration: a nested package.json becomes the
 * closest package boundary for the emitted `.js`, and without it Node would read
 * those files as CommonJS and the import would fail at load.
 */

import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const sourceDir = join(packageRoot, "src", "plugins", "openclaw");
const outDir = join(packageRoot, "dist", "plugins", "openclaw");

/** The compat floor lives next to the peer range it mirrors, not in two places. */
const rootPackage = JSON.parse(readFileSync(join(packageRoot, "package.json"), "utf8"));
const pluginApi = rootPackage.peerDependencies.openclaw;

mkdirSync(outDir, { recursive: true });
copyFileSync(join(sourceDir, "openclaw.plugin.json"), join(outDir, "openclaw.plugin.json"));

writeFileSync(
  join(outDir, "package.json"),
  `${JSON.stringify(
    {
      name: "@komaa/standin-sdk-openclaw",
      version: rootPackage.version,
      // Never published on its own: it ships inside @komaa/standin-sdk and is
      // reached by path. private keeps an accidental publish from this directory
      // from creating a second, half-empty package under a confusing name.
      private: true,
      type: "module",
      openclaw: {
        extensions: ["./index.js"],
        compat: { pluginApi },
      },
    },
    null,
    2,
  )}\n`,
);

console.info(`openclaw plugin directory ready: ${outDir}`);
