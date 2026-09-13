#!/usr/bin/env python3
# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT
"""Generate both SDK bindings from the shared call schema.

protocol/schema.yaml is the wire contract, and both SDK protocol modules are
written from it so the two languages cannot drift apart by hand. Pass --check
to verify the checked-in output without writing anything, which is what CI
runs.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
from pathlib import Path

from _schema import parse_yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "protocol/schema.yaml"
PYTHON = Path("libraries/python/standin/protocol.py")
TYPESCRIPT = Path("libraries/typescript/src/protocol.ts")
PROVENANCE = Path("protocol/schema.sha256")
TWO_WAY = ("both", "bidirectional")

# SDK binding policy, not wire definitions. Preserve constructor order and
# lenient call-context defaults while taking field names/types from the schema.
SESSION_ORDER = (
    "callId",
    "threadId",
    "caller",
    "direction",
    "recordingStatus",
    "tenantId",
)
CALLER_ORDER = ("aadId", "displayName", "tenantId")
BUILDER_FIELDS = {
    "audio.frame": {"seq": "long", "timestampMs": "long", "payloadBase64": "string"},
    "pong": {"ts": "long"},
    "assistant.cancel": {"turnId": "long"},
    "session.end": {"reason": "string"},
}
SESSION_BINDINGS = {
    "callId": ("string", False),
    "threadId": ("string", False),
    "caller": ("object:CallerInfo", False),
    "direction": ("string", True),
    "recordingStatus": ("string", True),
    "tenantId": ("string", True),
}


def snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).replace(".", "_").lower()


def camel(name: str) -> str:
    first, *rest = snake(name).split("_")
    return first + "".join(word.title() for word in rest)


def literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def fields_for(schema: dict, name: str) -> list[dict]:
    return next(msg["fields"] for msg in schema["messages"] if msg["type"] == name)


def session_fields(schema: dict) -> list[dict]:
    return ordered_fields(fields_for(schema, "session.start"), SESSION_ORDER)


def caller_fields(schema: dict) -> list[dict]:
    return ordered_fields(schema["types"]["CallerInfo"]["fields"], CALLER_ORDER)


def ordered_fields(fields: list[dict], order: tuple[str, ...]) -> list[dict]:
    by_name = {field["name"]: field for field in fields}
    return [by_name[name] for name in order if name in by_name] + [
        field for field in fields if field["name"] not in order
    ]


def builder_fields(schema: dict, name: str) -> list[dict]:
    # Extra optional wire fields do not change the established SDK arguments.
    by_name = {field["name"]: field for field in fields_for(schema, name)}
    return [by_name[field] for field in BUILDER_FIELDS[name]]


def validate_fields(fields: list[dict], label: str) -> None:
    names = [field["name"] for field in fields]
    if len(names) != len(set(names)):
        raise ValueError(f"duplicate field in {label}")
    bindings = [snake(name) for name in names]
    if len(bindings) != len(set(bindings)):
        raise ValueError(f"colliding SDK field names in {label}")


def validate_context(fields: list[dict], expected: dict, label: str) -> None:
    validate_fields(fields, label)
    by_name = {field["name"]: field for field in fields}
    for name, (kind, optional) in expected.items():
        field = by_name.get(name)
        if field is None or field["type"] != kind or bool(field.get("optional")) != optional:
            raise ValueError(f"update the SDK context binding for {label}.{name}")
    for field in fields:
        if field["name"] not in expected and (
            field["type"] != "string" or not field.get("optional")
        ):
            raise ValueError(f"new {label} fields must be optional strings")


def validate(schema: dict) -> None:
    # ONE direction vocabulary, the one this repo's schema.yaml is written in. There was a translation
    # layer here that accepted a second spelling and rewrote it; nothing in this repo ever produced that
    # spelling, so it was unreachable, and an unreachable branch in the one function every check and every
    # emitter goes through is a place for the two vocabularies to disagree in silence.
    if schema["protocol"]["discriminator"] != "type":
        raise ValueError("the SDK requires the 'type' discriminator")
    messages = {msg["type"]: msg for msg in schema["messages"]}
    if len(messages) != len(schema["messages"]):
        raise ValueError("duplicate message discriminator")
    for msg in messages.values():
        if msg["direction"] not in ("service_to_worker", "worker_to_service") + TWO_WAY:
            raise ValueError(f"unknown direction for {msg['type']}: {msg['direction']}")
        validate_fields(msg["fields"], msg["type"])
    if (
        "session.start" not in messages
        or messages["session.start"]["direction"] not in ("service_to_worker",) + TWO_WAY
    ):
        raise ValueError("SDK session.start is no longer inbound")
    validate_context(fields_for(schema, "session.start"), SESSION_BINDINGS, "session.start")
    validate_context(
        schema["types"]["CallerInfo"]["fields"],
        dict.fromkeys(CALLER_ORDER, ("string", True)),
        "CallerInfo",
    )
    direction = next(f for f in fields_for(schema, "session.start") if f["name"] == "direction")
    if direction.get("enum") != "CallDirection" or not {"inbound", "outbound"} <= set(
        schema["enums"]["CallDirection"]["values"]
    ):
        raise ValueError("the SDK requires the inbound and outbound call directions")
    for name, bound in BUILDER_FIELDS.items():
        if name not in messages:
            raise ValueError(f"missing SDK builder message {name}")
        if messages[name]["direction"] not in ("worker_to_service",) + TWO_WAY:
            raise ValueError(f"SDK builder {name} is no longer outbound")
        fields = fields_for(schema, name)
        by_name = {field["name"]: field for field in fields}
        required = {field["name"] for field in fields if not field.get("optional")}
        if required != set(bound) or any(
            by_name[field]["type"] != kind for field, kind in bound.items()
        ):
            raise ValueError(f"update the SDK argument binding for {name}")


def header(prefix: str, digest: str) -> list[str]:
    return [
        f"{prefix} Copyright (c) 2026 Komaa DigiTech",
        f"{prefix} SPDX-License-Identifier: MIT",
        f"{prefix} GENERATED from protocol/schema.yaml; do not hand-edit.",
        f"{prefix} Schema SHA-256: {digest}",
        f"{prefix} Regenerate with: python3 protocol/generate.py",
        "",
    ]


def py_type(field: dict) -> str:
    kind = field["type"]
    if kind == "object:CallerInfo":
        return "Caller"
    if kind == "string":
        return "str"
    if kind in ("long", "int"):
        return "int"
    raise ValueError(f"unsupported SDK field type: {kind}")


def py_read(field: dict, obj: str) -> str:
    value = f"{obj}.get({literal(field['name'])})"
    if field["type"] == "string":
        return f"clean({value})"
    if field["type"] in ("long", "int"):
        return f"normalize_pong_timestamp({value})"
    raise ValueError(f"unsupported SDK parser field type: {field['type']}")


def emit_python(schema: dict, digest: str) -> str:
    out = header("#", digest)
    out += [
        '"""Generated call context and wire builders with stable SDK defaults.',
        "",
        "Codec validation and additive unknown-message handling live in",
        "``_protocol_runtime``. Avatar messages remain outside the handler API.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import base64",
        "from dataclasses import dataclass",
        "from typing import Any",
        "",
        "from ._protocol_runtime import (",
        "    clean,",
        "    encode,",
        "    normalize_pong_timestamp,",
        ")",
        "from ._protocol_runtime import decode_pcm as decode_pcm",
        "from ._protocol_runtime import parse_message as parse_message",
        "",
    ]
    rate = next(c["value"] for c in schema["constants"] if c["name"] == "PCM_SAMPLE_RATE_HZ")
    out += [f"SAMPLE_RATE_HZ = {rate:_}", "NUM_CHANNELS = 1", ""]
    for msg in schema["messages"]:
        out.append(f"TYPE_{snake(msg['type']).upper()} = {literal(msg['type'])}")
    out += [
        "",
        "",
        "@dataclass(frozen=True)",
        "class Caller:",
        '    """Caller identity; blank or absent values normalize to None."""',
        "",
    ]
    for field in caller_fields(schema):
        out.append(f"    {snake(field['name'])}: {py_type(field)} | None = None")
    out += [
        "",
        "",
        "@dataclass(frozen=True)",
        "class SessionStart:",
        '    """Call context with the SDK\'s compatible constructor order and defaults."""',
        "",
    ]
    for field in session_fields(schema):
        ann = py_type(field)
        default = ""
        if field["name"] == "direction":
            default = ' = "inbound"'
        elif field.get("optional"):
            ann += " | None"
            default = " = None"
        out.append(f"    {snake(field['name'])}: {ann}{default}")
    out += [
        "",
        "",
        "def parse_session_start(msg: dict[str, Any]) -> SessionStart:",
        '    """Read call context; only callId lacks a safe default."""',
        '    call_id = clean(msg.get("callId"))',
        "    if not call_id:",
        '        raise ValueError("session.start is missing callId")',
        '    raw_caller = msg.get("caller")',
        "    caller_obj = raw_caller if isinstance(raw_caller, dict) else {}",
        '    direction = clean(msg.get("direction")) or "inbound"',
        "    return SessionStart(",
    ]
    for field in session_fields(schema):
        name = field["name"]
        if name == "caller":
            out.append("        caller=Caller(")
            for child in caller_fields(schema):
                out.append(f"            {snake(child['name'])}={py_read(child, 'caller_obj')},")
            out.append("        ),")
            continue
        if name == "callId":
            expr = "call_id"
        elif name == "direction":
            choices = ", ".join(literal(v) for v in schema["enums"][field["enum"]]["values"])
            expr = f'direction if direction in [{choices}] else "inbound"'
        else:
            expr = py_read(field, "msg")
            if not field.get("optional") and field["type"] == "string":
                expr += f" or {literal(field.get('pyDefault', ''))}"
        out.append(f"        {snake(name)}={expr},")
    out.append("    )")
    for name in BUILDER_FIELDS:
        fields = builder_fields(schema, name)
        if name == "audio.frame":
            params = "seq: int, timestamp_ms: int, pcm: bytes"
        elif name == "pong":
            params = "ts: Any"
        else:
            params = ", ".join(f"{snake(f['name'])}: {py_type(f)}" for f in fields)
        out += [
            "",
            "",
            f"def {snake(name)}({params}) -> str:",
            f'    """Build an outbound ``{name}`` JSON frame."""',
            "    return encode(",
            "        {",
            f'            "type": TYPE_{snake(name).upper()},',
        ]
        for field in fields:
            if field.get("optional"):
                continue
            key = field["name"]
            expr = snake(key)
            if name == "audio.frame" and key == "payloadBase64":
                expr = 'base64.b64encode(pcm).decode("ascii")'
            elif name == "pong":
                expr = "normalize_pong_timestamp(ts)"
            out.append(f"            {literal(key)}: {expr},")
        out += ["        }", "    )"]
    return "\n".join(out) + "\n"


def ts_type(field: dict, schema: dict) -> str:
    if field["name"] == "direction":
        return " | ".join(literal(v) for v in schema["enums"][field["enum"]]["values"])
    return {
        "string": "string",
        "int": "number",
        "long": "number",
        "object:CallerInfo": "Caller",
    }[field["type"]]


def emit_typescript(schema: dict, digest: str) -> str:
    out = header("//", digest)
    out += [
        'import { clean, normalizePongTimestamp } from "./protocolRuntime.js";',
        'export { contextSentences, decodePcm, parseMessage } from "./protocolRuntime.js";',
        "",
    ]
    rate = next(c["value"] for c in schema["constants"] if c["name"] == "PCM_SAMPLE_RATE_HZ")
    out += [
        f"export const SAMPLE_RATE_HZ = {rate:_};",
        "export const NUM_CHANNELS = 1;",
        "",
    ]
    for msg in schema["messages"]:
        out.append(f"export const TYPE_{snake(msg['type']).upper()} = {literal(msg['type'])};")
    out += [
        "",
        "/** Caller identity; blank or absent values normalize to undefined. */",
        "export interface Caller {",
    ]
    for field in caller_fields(schema):
        out.append(f"  readonly {field['name']}?: {ts_type(field, schema)};")
    out += [
        "}",
        "",
        "/** Call context with the SDK's compatible defaults. */",
        "export interface SessionStart {",
    ]
    for field in session_fields(schema):
        optional = "?" if field.get("optional") and field["name"] != "direction" else ""
        out.append(f"  readonly {field['name']}{optional}: {ts_type(field, schema)};")
    out += [
        "}",
        "",
        "/** Read call context; only callId lacks a safe default. */",
        "export function parseSessionStart(msg: Record<string, unknown>): SessionStart {",
        "  const callId = clean(msg.callId);",
        '  if (!callId) throw new Error("session.start is missing callId");',
        "  const rawCaller = msg.caller;",
        "  const callerObj: Record<string, unknown> =",
        '    typeof rawCaller === "object" && rawCaller !== null && !Array.isArray(rawCaller)',
        "      ? (rawCaller as Record<string, unknown>) : {};",
        '  const direction = clean(msg.direction) ?? "inbound";',
        "  return {",
    ]
    for field in session_fields(schema):
        name = field["name"]
        if name == "caller":
            out.append("    caller: {")
            for child in caller_fields(schema):
                out.append(f"      {child['name']}: clean(callerObj.{child['name']}),")
            out.append("    },")
            continue
        if name == "callId":
            expr = "callId"
        elif name == "direction":
            conditions = " || ".join(
                f"direction === {literal(v)}" for v in schema["enums"][field["enum"]]["values"]
            )
            expr = f'{conditions} ? direction : "inbound"'
        else:
            helper = "clean" if field["type"] == "string" else "normalizePongTimestamp"
            expr = f"{helper}(msg.{name})"
            if not field.get("optional") and field["type"] == "string":
                expr += f" ?? {literal(field.get('pyDefault', ''))}"
        out.append(f"    {name}: {expr},")
    out += ["  };", "}"]
    for name in BUILDER_FIELDS:
        fields = builder_fields(schema, name)
        if name == "audio.frame":
            params = "seq: number, timestampMs: number, pcm: Buffer"
        elif name == "pong":
            params = "ts: unknown"
        else:
            params = ", ".join(f"{f['name']}: {ts_type(f, schema)}" for f in fields)
        out += [
            "",
            f"/** Build an outbound `{name}` JSON frame. */",
            f"export function {camel(name)}({params}): string {{",
            "  return JSON.stringify({",
            f"    type: TYPE_{snake(name).upper()},",
        ]
        for field in fields:
            if field.get("optional"):
                continue
            key = field["name"]
            expr = key
            if name == "audio.frame" and key == "payloadBase64":
                expr = 'pcm.toString("base64")'
            elif name == "pong":
                expr = "normalizePongTimestamp(ts)"
            out.append(f"    {key}: {expr},")
        out += ["  });", "}"]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        type=Path,
        default=SCHEMA,
        help="schema to generate from (default: protocol/schema.yaml)",
    )
    parser.add_argument(
        "--check", action="store_true", help="fail on drift without modifying files"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT,
        help="repository root to write into (default: the repository root)",
    )
    args = parser.parse_args()
    source = args.schema.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    schema = parse_yaml(source.decode("utf-8"))
    validate(schema)
    artifacts = {
        Path("protocol/schema.yaml"): source.decode("utf-8"),
        PROVENANCE: f"{digest}  schema.yaml\n",
        PYTHON: emit_python(schema, digest),
        TYPESCRIPT: emit_typescript(schema, digest),
    }
    failed = False
    for relative, expected in artifacts.items():
        target = args.out_dir / relative
        if args.check:
            actual = target.read_text() if target.exists() else ""
            if actual != expected:
                failed = True
                print(f"DRIFT: {relative}")
                print(
                    "".join(
                        difflib.unified_diff(
                            actual.splitlines(True),
                            expected.splitlines(True),
                            fromfile=str(relative),
                            tofile="generated",
                        )
                    ),
                    end="",
                )
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(expected)
            print(f"wrote {target}")
    if args.check and not failed:
        print("ok: schema snapshot and both SDK protocol bindings")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
