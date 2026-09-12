#!/usr/bin/env python3
# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Everything that has to be true before either package is published.

A release is the one action in this repository that cannot be taken back. PyPI
refuses a re-upload of a version that already exists, and an npm unpublish is a
72-hour window and a public record. So the checks that matter run BEFORE the
tag, not after it.

Each rule below exists because getting it wrong ships something broken:

* **The two packages carry one version.** They are halves of one SDK, released
  together, and a reader comparing ``standin-sdk`` 0.2.0 against
  ``@komaa/standin-sdk`` 0.1.0 has no way to know they are the same release.

* **The TypeScript version is written twice.** ``package.json`` is what npm
  publishes and ``src/version.ts`` is what ``VERSION`` reports at runtime.
  Nothing in the build ties them together, so they drift silently and the
  package tells the truth about itself only by luck.

* **The tag agrees with both.** ``git tag v0.2.0`` on a tree that says 0.1.0
  publishes 0.1.0, and the tag then points at a version that was never built.

* **Both artifacts build and pass their own validators.** ``twine check``
  catches a readme PyPI will reject, which is otherwise discovered at upload
  time with the version already burned.

Run it with ``make release-check``. It builds into ``dist/`` and publishes
nothing: there is no upload path in this file at all.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / "libraries" / "python"
TYPESCRIPT = ROOT / "libraries" / "typescript"


def run(command: list[str], cwd: Path) -> tuple[int, str]:
    """Run one command and hand back its code and its output, merged."""
    finished = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return finished.returncode, (finished.stdout + finished.stderr).strip()


def python_version() -> str:
    text = (PYTHON / "standin" / "version.py").read_text()
    found = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return found.group(1) if found else ""


def npm_version() -> str:
    return str(json.loads((TYPESCRIPT / "package.json").read_text()).get("version", ""))


def runtime_version() -> str:
    text = (TYPESCRIPT / "src" / "version.ts").read_text()
    found = re.search(r'VERSION\s*=\s*"([^"]+)"', text)
    return found.group(1) if found else ""


def tagged_version() -> str:
    """The version a ``v`` tag on HEAD names, or empty when there is none.

    Only an exact match counts. A tag two commits back describes a different
    tree, and releasing from it would ship code nobody tagged.
    """
    code, out = run(["git", "describe", "--tags", "--exact-match"], ROOT)
    return out[1:] if code == 0 and out.startswith("v") else ""


def main() -> int:
    problems: list[str] = []

    py, npm, runtime = python_version(), npm_version(), runtime_version()
    if not py:
        problems.append("libraries/python/standin/version.py has no __version__")
    if not npm:
        problems.append("libraries/typescript/package.json has no version")
    if not runtime:
        problems.append("libraries/typescript/src/version.ts has no VERSION")

    if py and npm and py != npm:
        problems.append(
            f"the two packages disagree: standin-sdk is {py} and "
            f"@komaa/standin-sdk is {npm}. They are halves of one SDK and "
            "ship together, so the versions have to match."
        )
    if npm and runtime and npm != runtime:
        problems.append(
            f"package.json says {npm} and src/version.ts says {runtime}. The "
            "first is what npm publishes and the second is what VERSION "
            "reports at runtime, so the package would misreport itself."
        )

    tag = tagged_version()
    if tag and py and tag != py:
        problems.append(
            f"HEAD is tagged v{tag} but the tree is {py}. Publishing from here "
            f"would upload {py} under a tag that names {tag}."
        )

    print(f"version  python={py or '?'}  npm={npm or '?'}  runtime={runtime or '?'}"
          f"  tag={tag or 'none'}")

    code, out = run(["uv", "build"], PYTHON)
    if code != 0:
        problems.append(f"the Python package does not build:\n{out}")
    else:
        code, out = run(["uv", "run", "--with", "twine", "python", "-m", "twine", "check",
                         "dist/*"], PYTHON)
        # twine reads the metadata PyPI will read, so a readme it rejects is
        # caught here rather than at upload with the version already burned.
        if code != 0 or "FAILED" in out:
            problems.append(f"twine refuses the Python artifacts:\n{out}")
        else:
            print("python   built, and twine accepts both artifacts")

    code, out = run(["pnpm", "build"], TYPESCRIPT)
    if code != 0:
        problems.append(f"the TypeScript package does not build:\n{out}")
    else:
        code, out = run(["npm", "pack", "--dry-run"], TYPESCRIPT)
        if code != 0:
            problems.append(f"npm cannot pack the TypeScript package:\n{out}")
        else:
            for required in ("LICENSE", "README.md"):
                if required not in out:
                    problems.append(f"the npm tarball is missing {required}")
            print("typescript  built, and npm packs it with its licence and readme")

    if problems:
        print()
        for problem in problems:
            print(f"  {problem}")
        print(f"\n{len(problems)} things to fix before releasing")
        return 1
    print("\nready: nothing here published anything")
    return 0


if __name__ == "__main__":
    sys.exit(main())
