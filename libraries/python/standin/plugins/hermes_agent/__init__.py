# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Answer Microsoft Teams calls with your Hermes agent.

StandIn (https://standin.komaa.com) answers the Microsoft Teams call and dials this
worker. This plugin answers that dial, connects a realtime speech-to-speech
model for the conversation, and gives that model one door into Hermes - so the
caller talks to the same assistant they know from chat, with their own tools,
files and skills behind it.

Hermes loads this plugin IN-PROCESS, through the ``hermes_agent.plugins`` entry
point. That is the whole design: there is no HTTP hop to Hermes, no session
header, no second service to run. Enable it and serve::

    pip install "standin-sdk[hermes-agent]"

    # config.yaml
    plugins:
      enabled: [msteams_bridge]
      entries:
        msteams_bridge:
          config:
            allowlist: ["<the caller's AAD object id>"]

    export STANDIN_SECRET=...     # from the StandIn portal
    export OPENAI_API_KEY=...     # the realtime model
    hermes msteams-bridge serve

Then expose port 9442 and register the public URL as your StandIn identity's
agent voice URL.

**Who is the brain.** The realtime model is: it hears the caller and answers
them. Hermes is reached when the model calls ``hermes_agent_consult``, and never
sees audio. The model is good at conversation; Hermes is good at work.

See https://docs.komaa.com/hermes/installation for setup.
"""

from standin import StandInError
from standin.version import __version__

from .api import HermesUnavailable
from .config import PluginConfig, resolve_config
from .consult import AgentConsult
from .handler import RealtimeHandler
from .realtime import RealtimeConfig, realtime_config
from .service import handler_factory, main, report_readiness, serve

__all__ = [
    "AgentConsult",
    "HermesUnavailable",
    "PluginConfig",
    "RealtimeConfig",
    "RealtimeHandler",
    "StandInError",
    "__version__",
    "handler_factory",
    "main",
    "realtime_config",
    "register",
    "report_readiness",
    "resolve_config",
    "serve",
]

#: The ``msteams_bridge_status`` tool. Zero arguments on purpose: it answers
#: "will a call work right now?", and there is nothing to parameterise.
STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "msteams_bridge_status",
        "description": (
            "Report whether the Microsoft Teams call bridge is ready: the StandIn "
            "secret, the realtime provider, and the Hermes surfaces the call needs."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


def handle_status(**_kwargs: object) -> str:
    """The ``msteams_bridge_status`` handler. A JSON string, as tools return."""
    import json

    lines = report_readiness()
    return json.dumps(
        {
            "version": __version__,
            "ok": not any(line.startswith("error:") for line in lines),
            "notes": lines,
        },
        indent=2,
    )


def register(ctx: object) -> None:
    """The Hermes plugin entry point. Called once when the plugin is enabled.

    Captures the ``PluginContext`` behind the :mod:`~.api` boundary and
    registers the status tool and the ``hermes msteams-bridge`` command. It does
    NOT start the listener: a plugin that opened a port on import would bind it
    in every Hermes process, including short-lived CLI invocations. The listener
    starts when the operator asks for it, with ``serve``.
    """
    from . import api

    api.set_plugin_context(ctx)

    register_tool = getattr(ctx, "register_tool", None)
    if callable(register_tool):
        register_tool(
            name="msteams_bridge_status",
            toolset="msteams_bridge",
            schema=STATUS_SCHEMA,
            handler=handle_status,
            emoji="\N{TELEPHONE RECEIVER}",
        )

    register_cli = getattr(ctx, "register_cli_command", None)
    if callable(register_cli):
        from .cli import command, setup

        register_cli(
            name="msteams-bridge",
            help="Answer Microsoft Teams calls with this agent (serve, status)",
            setup_fn=setup,
            handler_fn=command,
            description=(
                "Run the StandIn call listener in this Hermes process, so a Microsoft Teams "
                "call reaches your real agent. See: hermes msteams-bridge status"
            ),
        )


# Keep the generated API docs to the exported surface.
_module = dir()
NOT_IN_ALL = [m for m in _module if m not in __all__]

__pdoc__ = {}

for n in NOT_IN_ALL:
    __pdoc__[n] = False
