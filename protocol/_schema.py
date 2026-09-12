# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Dependency-free parser for the shared protocol schema YAML subset."""

from __future__ import annotations

import re


def _parse_scalar(token: str):
    token = token.strip()
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in inner.split(",")]
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1]
    if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    if token in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    return token


def parse_yaml(text: str):
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        lines.append((indent, raw.strip()))

    pos = 0

    def parse_block(indent: int):
        return parse_list(indent) if lines[pos][1].startswith("- ") else parse_map(indent)

    def parse_map(indent: int):
        nonlocal pos
        result: dict = {}
        while pos < len(lines):
            ind, content = lines[pos]
            if ind < indent or content.startswith("- "):
                break
            if ind > indent:
                raise ValueError(f"unexpected indent: {content!r}")
            key, sep, rest = content.partition(":")
            if not sep:
                raise ValueError(f"expected 'key:' line, got {content!r}")
            key = key.strip()
            if key in result:
                raise ValueError(f"duplicate mapping key: {key}")
            rest = rest.strip()
            pos += 1
            if rest:
                result[key] = _parse_scalar(rest)
            elif pos < len(lines) and lines[pos][0] > indent:
                result[key] = parse_block(lines[pos][0])
            else:
                result[key] = None
        return result

    def parse_list(indent: int):
        nonlocal pos
        result: list = []
        while pos < len(lines):
            ind, content = lines[pos]
            if ind != indent or not content.startswith("- "):
                if ind < indent:
                    break
                raise ValueError(f"unexpected line in list: {content!r}")
            item = content[2:].strip()
            pos += 1
            if ":" in item:
                key, _, rest = item.partition(":")
                first = {key.strip(): _parse_scalar(rest) if rest.strip() else None}
                if (
                    pos < len(lines)
                    and lines[pos][0] == indent + 2
                    and not lines[pos][1].startswith("- ")
                ):
                    rest_map = parse_map(indent + 2)
                    if first.keys() & rest_map.keys():
                        raise ValueError(f"duplicate mapping key: {key.strip()}")
                    first.update(rest_map)
                result.append(first)
            else:
                result.append(_parse_scalar(item))
        return result

    result = parse_map(0)
    if pos != len(lines):
        raise ValueError(f"unexpected trailing input: {lines[pos][1]!r}")
    return result
