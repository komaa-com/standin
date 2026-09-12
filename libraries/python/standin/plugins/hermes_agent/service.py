# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Starting the listener, from inside Hermes or on its own.

Two ways in, one code path:

* ``hermes msteams-bridge serve`` - the plugin is loaded in-process by Hermes,
  so the consult reaches the operator's real agent, their tools and their skills.
  This is the mode the plugin is for.
* ``python -m standin.plugins.hermes_agent`` - the same listener with no host. The
  realtime model answers the call and ``hermes_agent_consult`` says, in words,
  that it cannot reach its tools. Useful for checking a secret, a tunnel and a
  StandIn identity without standing up Hermes first.

What is NOT here is the transport. The socket, the handshake and its replay
guard, connection caps, draining, the wire protocol, the frame loop and both
watchdogs all belong to :class:`standin.CallServer`. This file resolves
configuration, builds one handler per call, and waits.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from standin import CallServer, StandInError

from .api import host_available, probe_boundaries
from .config import PluginConfig, resolve_config
from .handler import RealtimeHandler
from .log import logger
from .realtime import RealtimeConfig, realtime_config

__all__ = ["handler_factory", "main", "report_readiness", "serve"]


def handler_factory(
    *, config: RealtimeConfig | None = None, plugin: PluginConfig | None = None
) -> Any:
    """Build the per-call factory :class:`standin.CallServer` calls.

    Configuration is resolved ONCE here and closed over, not re-read per call:
    a config file edited mid-shift must not give two live calls different rules,
    and reading the host's config on the audio path is work a call does not need.
    """
    cfg = config or realtime_config()
    pol = plugin or resolve_config()

    def build() -> RealtimeHandler:
        return RealtimeHandler(config=cfg, plugin=pol)

    return build


def report_readiness() -> list[str]:
    """Lines worth printing before the first call arrives.

    Everything that can be checked without a caller is checked here, because the
    alternative is discovering it on turn forty with somebody on the line.
    Returns the lines rather than printing them, so the CLI and a test can both
    use it.
    """
    lines: list[str] = []
    if not host_available():
        lines.append(
            "warning: no Hermes host in this interpreter - the call will be answered, "
            "but hermes_agent_consult cannot run. Start with `hermes msteams-bridge serve`."
        )
    for row in probe_boundaries():
        if not row["ok"]:
            lines.append(f"warning: Hermes surface missing: {row['surface']} ({row['detail']})")
    if not realtime_config().configured:
        lines.append(
            "error: no realtime API key - set OPENAI_API_KEY, or "
            "MSTEAMS_BRIDGE_REALTIME_API_KEY / AZURE_OPENAI_API_KEY for Azure"
        )
    if not os.environ.get("STANDIN_SECRET", ""):
        lines.append("error: STANDIN_SECRET is not set - StandIn's dial cannot be authenticated")
    return lines


async def serve(
    *, config: RealtimeConfig | None = None, plugin: PluginConfig | None = None
) -> None:
    """Answer Microsoft Teams calls until cancelled."""
    for line in report_readiness():
        logger.warning("standin: %s", line)
    server = CallServer(handler_factory=handler_factory(config=config, plugin=plugin))
    await server.start()
    try:
        await asyncio.Event().wait()
    finally:
        await server.aclose()


def main() -> int:
    """``python -m standin.plugins.hermes_agent``. Returns a process exit code."""
    if not logging.getLogger().handlers:
        # Without a handler the root logger passes WARNING and above only, so
        # every session line - call ids, starts, teardown - vanishes from a
        # serve log that looks like it is working.
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    lines = report_readiness()
    for line in lines:
        print(line)
    if any(line.startswith("error:") for line in lines):
        return 1
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except StandInError as err:
        print(f"error: {err}")
        return 1
    return 0
