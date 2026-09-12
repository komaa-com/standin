# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""The ``hermes msteams-bridge`` subcommands.

Three, and the set is deliberate: ``status`` answers "would a call work right
now?" from configuration alone, ``smoke`` answers the same question by actually
placing one against this worker on loopback, and ``serve`` runs the listener in
the foreground so a container has something to supervise.

Separate from :mod:`~.service` because argparse wiring is not the service, and
because ``register(ctx)`` must be able to point Hermes at these without
importing the whole handler stack at plugin-load time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

__all__ = ["command", "setup"]


def setup(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes msteams-bridge`` argparse tree."""
    subs = subparser.add_subparsers(dest="msteams_bridge_command")
    subs.add_parser("status", help="Report bridge configuration and readiness")
    subs.add_parser("serve", help="Answer Microsoft Teams calls in the foreground")
    subs.add_parser("smoke", help="Ring this worker's own handler and report what worked")


def command(args: Any) -> int:
    """Dispatch a subcommand. Returns a process exit code."""
    from .service import report_readiness, serve

    which = getattr(args, "msteams_bridge_command", None)

    if which == "status":
        lines = report_readiness()
        print(
            json.dumps(
                {"ok": not any(x.startswith("error:") for x in lines), "notes": lines}, indent=2
            )
        )
        return 0 if not any(x.startswith("error:") for x in lines) else 1

    if which == "smoke":
        # A real call against this worker, on loopback, with a credential
        # generated for the run. Nothing that delivers is started, so nobody is
        # rung and nothing is posted as a side effect of checking an install.
        from standin.smoke import report, run_smoke

        from .service import handler_factory

        result = asyncio.run(run_smoke(handler_factory()))
        print(report(result))
        return 0 if result.ok else 1

    if which == "serve":
        lines = report_readiness()
        for line in lines:
            print(line)
        if any(line.startswith("error:") for line in lines):
            return 1
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            pass
        return 0

    print("usage: hermes msteams-bridge {status,serve,smoke}")
    return 2
