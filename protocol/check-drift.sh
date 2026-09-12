#!/usr/bin/env bash
# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT
set -euo pipefail
SDK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec python3 "$SDK_ROOT/protocol/generate.py" --check "$@"
