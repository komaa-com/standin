# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT
"""Exercise reproducibility, drift detection and incompatible schema changes."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import generate


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "sdk"
        self.source = Path(self.temp.name) / "schema.yaml"
        self.source.write_bytes(generate.SCHEMA.read_bytes())

    def run_generator(self, *args):
        return subprocess.run(
            [
                sys.executable,
                str(Path(generate.__file__)),
                "--schema",
                str(self.source),
                "--out-dir",
                str(self.output),
                *args,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def import_generated(self, source):
        # Load only the generated module and its dependency-free runtime. The
        # SDK package initializer also imports optional transport dependencies.
        package = types.ModuleType("generated_sdk_test")
        package.__path__ = [str(generate.ROOT / "libraries/python/standin")]
        module = types.ModuleType(f"{package.__name__}.protocol")
        module.__package__ = package.__name__
        with patch.dict(sys.modules, {package.__name__: package, module.__name__: module}):
            exec(compile(source, "generated_protocol.py", "exec"), module.__dict__)
        return module

    def test_reproducible_and_detects_manual_edits_without_overwriting(self):
        first = self.run_generator()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.run_generator("--check").returncode, 0)
        for relative in (generate.PYTHON, generate.TYPESCRIPT):
            target = self.output / relative
            original = target.read_text()
            target.write_text(original + "\nmanual edit\n")
            checked = self.run_generator("--check")
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn(str(relative), checked.stdout)
            self.assertEqual(target.read_text(), original + "\nmanual edit\n")
            target.write_text(original)

    def test_schema_change_updates_both_languages_and_snapshot(self):
        self.assertEqual(self.run_generator().returncode, 0)
        self.source.write_text(self.source.read_text().replace("value: 16000", "value: 24000"))
        checked = self.run_generator("--check")
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("protocol/schema.yaml", checked.stdout)
        self.assertEqual(self.run_generator().returncode, 0)
        for relative in (generate.PYTHON, generate.TYPESCRIPT):
            self.assertIn("SAMPLE_RATE_HZ = 24_000", (self.output / relative).read_text())
        self.assertEqual(
            self.source.read_bytes(),
            (self.output / "protocol/schema.yaml").read_bytes(),
        )
        self.assertEqual(self.run_generator("--check").returncode, 0)

    def test_new_optional_context_fields_import_and_normalize_values(self):
        schema = generate.parse_yaml(self.source.read_text())
        generate.fields_for(schema, "session.start").append(
            {"name": "meetingLabel", "type": "string", "optional": True}
        )
        schema["types"]["CallerInfo"]["fields"].append(
            {"name": "pronouns", "type": "string", "optional": True}
        )
        generate.validate(schema)
        python = generate.emit_python(schema, "test")
        module = self.import_generated(python)
        parsed = module.parse_session_start(
            {
                "callId": "call-1",
                "meetingLabel": "  Planning  ",
                "caller": {"pronouns": "  they/them  "},
            }
        )
        self.assertEqual(parsed.meeting_label, "Planning")
        self.assertEqual(parsed.caller.pronouns, "they/them")
        self.assertEqual(parsed.direction, "inbound")
        empty = module.parse_session_start({"callId": "call-2"})
        self.assertIsNone(empty.meeting_label)
        self.assertIsNone(empty.caller.pronouns)
        caller = module.Caller("aad", "Name", "tenant")
        self.assertEqual(module.SessionStart("call-3", "", caller).caller.aad_id, "aad")
        typescript = generate.emit_typescript(schema, "test")
        self.assertIn("readonly meetingLabel?: string;", typescript)
        self.assertIn("meetingLabel: clean(msg.meetingLabel)", typescript)
        self.assertIn("readonly pronouns?: string;", typescript)
        self.assertIn("pronouns: clean(callerObj.pronouns)", typescript)

    def test_new_optional_outbound_fields_preserve_builder_arguments_and_payloads(self):
        schema = generate.parse_yaml(self.source.read_text())
        expected_python = generate.emit_python(schema, "test")
        expected_typescript = generate.emit_typescript(schema, "test")
        for name in generate.BUILDER_FIELDS:
            generate.fields_for(schema, name).append(
                {"name": "extension", "type": "string", "optional": True}
            )
        generate.validate(schema)
        self.assertEqual(generate.emit_python(schema, "test"), expected_python)
        self.assertEqual(generate.emit_typescript(schema, "test"), expected_typescript)
        module = self.import_generated(generate.emit_python(schema, "test"))
        self.assertEqual(
            json.loads(module.assistant_cancel(7)), {"type": "assistant.cancel", "turnId": 7}
        )
        self.assertEqual(
            json.loads(module.session_end("finished")),
            {"type": "session.end", "reason": "finished"},
        )

    def test_schema_field_order_does_not_change_public_constructor_or_builder_order(self):
        schema = generate.parse_yaml(self.source.read_text())
        expected_python = generate.emit_python(schema, "test")
        expected_typescript = generate.emit_typescript(schema, "test")
        for name in ("session.start", *generate.BUILDER_FIELDS):
            generate.fields_for(schema, name).reverse()
        schema["types"]["CallerInfo"]["fields"].reverse()
        generate.validate(schema)
        self.assertEqual(generate.emit_python(schema, "test"), expected_python)
        self.assertEqual(generate.emit_typescript(schema, "test"), expected_typescript)

    def test_unknown_direction_fails_before_writing(self):
        self.source.write_text(
            self.source.read_text().replace("direction: both", "direction: typo")
        )
        result = self.run_generator()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown direction", result.stderr)
        self.assertFalse(self.output.exists())

    def test_new_required_outbound_field_requires_an_explicit_sdk_binding(self):
        schema = generate.parse_yaml(self.source.read_text())
        generate.fields_for(schema, "audio.frame").append({"name": "codec", "type": "string"})
        with self.assertRaisesRegex(ValueError, "argument binding for audio.frame"):
            generate.validate(schema)

    def test_incompatible_context_extensions_are_rejected(self):
        baseline = generate.parse_yaml(self.source.read_text())
        for target in ("session.start", "CallerInfo"):
            for field in (
                {"name": "extension", "type": "string"},
                {"name": "extension", "type": "int", "optional": True},
            ):
                with self.subTest(target=target, field=field):
                    schema = copy.deepcopy(baseline)
                    fields = (
                        generate.fields_for(schema, target)
                        if target == "session.start"
                        else schema["types"][target]["fields"]
                    )
                    fields.append(field)
                    with self.assertRaisesRegex(ValueError, "must be optional strings"):
                        generate.validate(schema)

    def test_changed_existing_field_types_or_requiredness_are_rejected(self):
        baseline = generate.parse_yaml(self.source.read_text())
        for target, name, change in (
            ("session.start", "callId", {"type": "int"}),
            ("session.start", "callId", {"optional": True}),
            ("session.start", "direction", {"optional": False}),
            ("CallerInfo", "aadId", {"type": "int"}),
            ("CallerInfo", "aadId", {"optional": False}),
            ("assistant.cancel", "turnId", {"type": "string"}),
            ("pong", "ts", {"optional": True}),
        ):
            with self.subTest(target=target, name=name, change=change):
                schema = copy.deepcopy(baseline)
                fields = (
                    schema["types"][target]["fields"]
                    if target == "CallerInfo"
                    else generate.fields_for(schema, target)
                )
                next(field for field in fields if field["name"] == name).update(change)
                with self.assertRaisesRegex(ValueError, "SDK .* binding"):
                    generate.validate(schema)

    def test_discriminator_and_message_direction_changes_are_rejected(self):
        baseline = generate.parse_yaml(self.source.read_text())
        schema = copy.deepcopy(baseline)
        schema["protocol"]["discriminator"] = "kind"
        with self.assertRaisesRegex(ValueError, "discriminator"):
            generate.validate(schema)
        # Each message is flipped to the OPPOSITE valid direction, so the
        # generator must reject it for being the wrong way round rather than for
        # being an unrecognised value. Using an unknown string here would pass
        # for the wrong reason.
        for name, direction in (
            ("session.start", "worker_to_service"),
            ("pong", "service_to_worker"),
        ):
            with self.subTest(name=name):
                schema = copy.deepcopy(baseline)
                next(msg for msg in schema["messages"] if msg["type"] == name)["direction"] = (
                    direction
                )
                with self.assertRaisesRegex(ValueError, "no longer"):
                    generate.validate(schema)

    def test_duplicate_or_colliding_caller_fields_are_rejected(self):
        for name in ("aadId", "aad_id"):
            with self.subTest(name=name):
                schema = generate.parse_yaml(self.source.read_text())
                schema["types"]["CallerInfo"]["fields"].append(
                    {"name": name, "type": "string", "optional": True}
                )
                with self.assertRaisesRegex(ValueError, "duplicate|colliding"):
                    generate.validate(schema)

    def test_parser_rejects_duplicate_keys_and_unconsumed_input(self):
        for source in (
            "protocol:\n  discriminator: type\n  discriminator: kind\n",
            "messages:\n  - type: ping\n    type: pong\n",
            "protocol: ignored\n- unexpected\n",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                generate.parse_yaml(source)


if __name__ == "__main__":
    unittest.main()
