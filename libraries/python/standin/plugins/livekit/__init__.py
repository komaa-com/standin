# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""Answer Microsoft Teams calls with a LiveKit Agent.

StandIn (https://standin.komaa.com) answers the Microsoft Teams call and dials this
worker. This plugin answers that dial, creates one LiveKit room per call,
dispatches this worker's own agent into it, and relays the audio both ways.

Your file is shaped like every other agent example - nothing starts except
through ``cli.run_app(server)``::

    from livekit.agents import AgentServer, AgentSession, JobContext, cli
    from standin.plugins import livekit as standin

    server = AgentServer()

    @server.rtc_session(agent_name="standin-msteams")
    async def entrypoint(ctx: JobContext):
        session = AgentSession(llm=...)
        call = await standin.TeamsCall().start(session, ctx=ctx)
        await session.start(agent=MyAgent(call), room=ctx.room)

    if __name__ == "__main__":
        cli.run_app(server)

Importing this module arms it; setting ``STANDIN_SECRET`` starts it. The call
listener starts with the worker and stops with it, and :class:`TeamsCall` binds
the Microsoft Teams-only surface (caller identity, call context, the closing line StandIn
wants spoken)
inside the entrypoint.

    pip install "standin-sdk[livekit]"

If your agent publishes video of its own - an avatar worker, a rendered face -
it is relayed onto the bot's video tile automatically, so the caller sees your
agent rather than StandIn's avatar. On unless ``LIVEKIT_TILE_VIDEO=off``, and it
needs the ``tile`` extra to encode frames: without it the relay stays off with
one log line and the audio is unaffected.

That extra is what makes this module importable at all. Every OTHER module in
the SDK loads with aiohttp alone; this one is the deliberate exception, because
importing it is how a LiveKit worker is armed, and arming a worker means
registering with a framework that has to be there. Reach it and LiveKit is
imported; leave it alone - which is what ``import standin`` does, through the
lazy hook in :mod:`standin` - and it costs nothing. Without the extra you get
:class:`standin.PluginNotInstalled` naming the line above, not a
ModuleNotFoundError from inside livekit-agents.

See https://docs.komaa.com/livekit/installation for setup.
"""

from standin import StandInError
from standin.plugins._lazy import require
from standin.version import __version__

from .call import TOPIC_CONTEXT, TOPIC_GOODBYE, CallInfo, TeamsCall
from .handler import TeamsCallHandler
from .log import logger

__all__ = [
    "TOPIC_CONTEXT",
    "TOPIC_GOODBYE",
    "CallInfo",
    "StandInError",
    "TeamsCall",
    "TeamsCallHandler",
    "__version__",
]


def _arm() -> None:
    """Register with LiveKit and hook the worker lifecycle, once, at import.

    Every framework import this plugin makes is inside this function, so
    the framework is touched when somebody reaches for the plugin and
    never when the SDK is merely imported. Subclassing ``Plugin`` has to happen
    here too: a class statement evaluates its base at definition time, which is
    exactly the module-load-time import that is not allowed.
    """
    Plugin = require("livekit.agents", plugin="livekit", extra="livekit").Plugin

    class StandInPlugin(Plugin):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__(__name__, __version__, __package__, logger)

    Plugin.register_plugin(StandInPlugin())

    # Arm the zero-wiring startup: every AgentServer gets the worker_started
    # hook, which starts the call listener only when STANDIN_SECRET is set.
    # See service.py.
    from .service import _install as _service_install

    _service_install()


_arm()

# Keep the generated API docs to the exported surface.
_module = dir()
NOT_IN_ALL = [m for m in _module if m not in __all__]

__pdoc__ = {}

for n in NOT_IN_ALL:
    __pdoc__[n] = False
