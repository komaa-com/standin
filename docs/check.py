#!/usr/bin/env python3
# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Fail the build on the documentation mistakes that actually happen here.

Not a linter for its own sake. Every rule below is one that has already shipped
a broken page or a wrong claim on this site at least once:

* A navigation entry pointing at a file that does not exist takes the whole
  Mintlify build down, and the error names the path rather than the page that
  referenced it.
* A page on disk that nothing links to is invisible in navigation and still
  reachable by URL, so it rots in public.
* An internal link to a heading that was renamed is a dead end a reader finds
  before anyone else does.
* An em dash is a house style rule, and the reason it is enforced by a script
  rather than by review is that it is the one thing review always misses.
* A page with no description gets a search result with no summary under it.
* A sample importing a name the package does not export looks right until a
  reader runs it, which is the most expensive kind of wrong a doc can be.
* A public name with no prose anywhere is a capability nobody can find. This
  site once carried a hundred of them, and the page that was meant to list what
  the SDK could do said four of its lanes did not exist.
* A sentence that names an internal component, address or route tells a reader
  how the service is built, which is the one thing this site must never do.

Run it with ``make docs-check``. It reads only; it never rewrites a page.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

DOCS = Path(__file__).resolve().parent

#: Spellings that are wrong often enough to be worth failing over. The key is
#: what must never appear; the value is what it should be.
BRANDS = {
    "Standin": "StandIn",
    "StandIn SDK's": "the StandIn SDK's",
    "Openclaw": "OpenClaw",
    "openClaw": "OpenClaw",
    "Livekit": "LiveKit",
    "liveKit": "LiveKit",
    "Elevenlabs": "ElevenLabs",
    "elevenLabs": "ElevenLabs",
    "MS Teams": "Microsoft Teams",
}

#: "Teams" on its own reads as the generic word. The brand is "Microsoft Teams",
#: and a bare one is nearly always a slip rather than a second reference.
BARE_TEAMS = re.compile(r"(?<!Microsoft )(?<!microsoft-)(?<![\w/-])Teams(?![\w-])")

#: Wording that gives away how the service is built. Each one was on the site
#: once: an internal component name, an internal control address, an internal
#: portal route, and a service-side refresh interval nobody outside can observe.
#: The rule outranks every other rule here, so it fails the build like the rest.
LEAKS = {
    r"StandIn gateway": 'name the product, not a component of it: say "StandIn"',
    r"the gateway (rejects|rejected)": 'say "StandIn rejects"',
    r"gateway-bound": 'say "the reply StandIn expects"',
    r"127\.0\.0\.1:9440": "an internal control address does not belong on a page",
    r"/api/identities": "an internal portal route does not belong on a page",
    r"/api/chat/reply": "use /api/calls, which the published package READMEs already carry",
    r"(regional|self-hosted) gateway": "do not describe the service's own topology",
}

#: Product names that legitimately contain the word, so the rule above does not
#: fire on somebody else's software.
NOT_OURS = re.compile(r"(Hermes|OpenClaw|openclaw) gateway")

#: Dashes the house style forbids, by name so the failure reads clearly.
DASHES = {"—": "em dash", "–": "en dash"}

#: A fenced code block. Brand casing and dashes inside one are the author's
#: business: it may be quoting a real command or a real error string.
FENCE = re.compile(r"^```", re.MULTILINE)

LINK = re.compile(r"\[[^\]]*\]\((/[^)\s]*)\)")

#: An import in a SAMPLE. Only fenced blocks are scanned: a page may quote a
#: name that deliberately does not resolve, to warn a reader off it, and that
#: sentence is the opposite of a defect.
PY_IMPORT = re.compile(r"^from standin import \(([^)]*)\)|^from standin import ([^\n(]+)", re.MULTILINE)
TS_IMPORT = re.compile(r'import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*"@komaa/standin-sdk"')
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def pages_in_nav(config: dict) -> list[str]:
    """Every page path the navigation references, in order."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "pages" and isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            found.append(item)
                        else:
                            walk(item)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(config)
    return found


def anchors(heading: str) -> set[str]:
    """Every slug a renderer might plausibly give this heading.

    Lowercased, punctuation dropped, spaces to hyphens. Inline code and bold
    markers are stripped first, because a heading written as ``## The `seam` ``
    anchors as ``the-seam`` and a link written against the source text would
    never resolve.

    Two spellings are accepted for a heading carrying an underscore, such as
    ``## on_start``: sluggers disagree about whether an underscore survives, and
    this script cannot run the site to find out. Accepting both keeps it from
    failing a link that is fine, which is the failure mode that gets a checker
    switched off.
    """
    text = re.sub(r"[`*]", "", heading)
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    kept = re.sub(r"\s+", "-", text)
    return {kept, re.sub(r"[\s_]+", "-", text)}


def inside_code(text: str) -> str:
    """Only the fenced blocks, which is where a sample lives."""
    parts = FENCE.split(text)
    return "\n".join(part for index, part in enumerate(parts) if index % 2 == 1)


def listed(block: str, renamed_to: bool = False) -> list[str]:
    """The names in one import or export list.

    ``a as b`` means different things on the two sides: an import asks for
    ``a`` and an export offers ``b``, so the caller says which end it wants.
    """
    out = []
    for piece in re.sub(r"//.*", "", block).replace("\n", ",").split(","):
        piece = re.sub(r"#.*", "", piece).strip()
        piece = re.sub(r"^type\s+", "", piece)
        piece = piece.split(" as ")[-1 if renamed_to else 0].strip()
        if re.fullmatch(r"[A-Za-z_$][\w$]*", piece):
            out.append(piece)
    return out


def exported() -> tuple[set[str], set[str]]:
    """What each package barrel actually offers.

    Read from the source rather than by importing, so this runs with neither
    SDK installed. An example importing a name the barrel does not export is
    the defect that costs a reader the most: it looks right until it is run.
    """
    root = DOCS.parent / "libraries"
    python = set()
    init = (root / "python" / "standin" / "__init__.py").read_text()
    block = init.split("__all__ = [", 1)
    if len(block) == 2:
        python = {m for m in re.findall(r'"([^"]+)"', block[1].split("]", 1)[0])}

    source = (root / "typescript" / "src" / "index.ts").read_text()
    typescript = set()
    for group in re.findall(r"export\s*(?:type\s*)?\{([^}]*)\}\s*from", source, re.DOTALL):
        typescript.update(listed(group, renamed_to=True))
    typescript.update(
        re.findall(r"export\s+(?:const|class|function|type|interface)\s+([A-Za-z_$][\w$]*)", source)
    )
    return python, typescript


def outside_code(text: str) -> str:
    """The page with its fenced blocks removed."""
    parts = FENCE.split(text)
    # Even-indexed parts are outside a fence, odd-indexed are inside one.
    return "\n".join(part for index, part in enumerate(parts) if index % 2 == 0)


def undocumented(python_exports: set[str], text: str) -> list[str]:
    """Public names with no prose anywhere on the site.

    Failing rather than warning is the point. A warning would have let the
    hundred that accumulated here accumulate, and the fix is cheap: document
    the name, or write it in ``docs/undocumented.txt`` with the reason it does
    not need documenting. Either way somebody decided.
    """
    excused = set()
    allow = DOCS / "undocumented.txt"
    if allow.exists():
        for line in allow.read_text().splitlines():
            name = line.split("#", 1)[0].strip()
            if name:
                excused.add(name)
    return [n for n in sorted(python_exports) if n not in excused and n not in text]


def main() -> int:
    config = json.loads((DOCS / "docs.json").read_text())
    problems: list[str] = []
    python_exports, typescript_exports = exported()

    nav = pages_in_nav(config)
    on_disk = {str(p.relative_to(DOCS).with_suffix("")) for p in DOCS.rglob("*.mdx")}

    for page in nav:
        if not (DOCS / f"{page}.mdx").exists():
            problems.append(f"docs.json: navigation points at {page}, which is not on disk")

    for page in sorted(on_disk - set(nav)):
        problems.append(f"{page}.mdx: on disk but not in navigation, so nothing links to it")

    seen: dict[str, str] = {}
    for page in sorted(on_disk):
        path = DOCS / f"{page}.mdx"
        text = path.read_text()

        matter = FRONTMATTER.match(text)
        if matter is None:
            problems.append(f"{page}.mdx: no frontmatter")
        else:
            block = matter.group(1)
            for field in ("title", "description"):
                if not re.search(rf"^{field}:\s*\S", block, re.MULTILINE):
                    problems.append(f"{page}.mdx: frontmatter has no {field}")
            described = re.search(r"^description:\s*(.+)$", block, re.MULTILINE)
            if described:
                value = described.group(1).strip().strip('"')
                if value in seen:
                    problems.append(
                        f"{page}.mdx: description is identical to {seen[value]}.mdx, "
                        "so both get the same search summary"
                    )
                seen[value] = page

        samples = inside_code(text)
        for match in PY_IMPORT.finditer(samples):
            for name in listed(match.group(1) or match.group(2) or ""):
                if python_exports and name not in python_exports:
                    problems.append(f"{page}.mdx: a sample imports {name} from standin, which does not export it")
        for match in TS_IMPORT.finditer(samples):
            for name in listed(match.group(1)):
                if typescript_exports and name not in typescript_exports:
                    problems.append(f"{page}.mdx: a sample imports {name} from the package, which does not export it")

        prose = outside_code(text)
        for dash, name in DASHES.items():
            if dash in text:
                line = text[: text.index(dash)].count("\n") + 1
                problems.append(f"{page}.mdx:{line}: {name}, which the house style forbids")
        for wrong, right in BRANDS.items():
            if re.search(rf"(?<![\w/-]){re.escape(wrong)}(?![\w-])", prose):
                problems.append(f'{page}.mdx: "{wrong}" should be "{right}"')
        for pattern, why in LEAKS.items():
            for match in re.finditer(pattern, text):
                window = text[max(0, match.start() - 20) : match.end()]
                if NOT_OURS.search(window):
                    continue
                line = text[: match.start()].count("\n") + 1
                problems.append(f"{page}.mdx:{line}: {match.group(0)!r}: {why}")
        for match in BARE_TEAMS.finditer(prose):
            line = prose[: match.start()].count("\n") + 1
            problems.append(f'{page}.mdx:{line}: bare "Teams" should be "Microsoft Teams"')

        for target in LINK.findall(prose):
            page_part, _, fragment = target.partition("#")
            page_part = page_part.rstrip("/")
            if not page_part:
                continue
            resolved = page_part.lstrip("/")
            if resolved not in on_disk:
                problems.append(f"{page}.mdx: links to {target}, which is not a page")
                continue
            if fragment:
                target_text = (DOCS / f"{resolved}.mdx").read_text()
                known = set().union(*(anchors(h) for h in HEADING.findall(target_text)))
                if fragment not in known:
                    problems.append(
                        f"{page}.mdx: links to {target}, but that page has no such heading"
                    )

    corpus = "\n".join((DOCS / f"{page}.mdx").read_text() for page in sorted(on_disk))
    for name in undocumented(python_exports, corpus):
        problems.append(
            f"standin.{name} is public and appears on no page: document it, "
            "or add it to docs/undocumented.txt with the reason"
        )

    for redirect in config.get("redirects", []):
        destination = str(redirect.get("destination", "")).split("#")[0].strip("/")
        if destination and destination not in on_disk:
            problems.append(
                f"docs.json: redirect to /{destination} does not resolve to a page"
            )

    if problems:
        for problem in problems:
            print(problem)
        print(f"\n{len(problems)} documentation problems")
        return 1
    print(f"ok: {len(nav)} pages, navigation, links, anchors, frontmatter, imports, coverage, style and boundaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
