#!/usr/bin/env node
// Copyright (c) 2026 Komaa DigiTech
// SPDX-License-Identifier: MIT

/** `npx standin-elevenlabs` */

import { serve } from "./index.js";

serve().catch((err: unknown) => {
  console.error(String(err));
  process.exitCode = 1;
});
