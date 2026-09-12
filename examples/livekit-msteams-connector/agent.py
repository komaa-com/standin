# Copyright (c) 2026 Komaa DigiTech
# SPDX-License-Identifier: MIT

"""A LiveKit Agent that answers Microsoft Teams calls.

This is a normal LiveKit worker. The only StandIn-specific lines are the import
and the one `TeamsCall().start(...)` call - everything else is the shape every
LiveKit agent example already has, and `cli.run_app(server)` is still the only
thing that starts anything.

    pip install "standin-sdk[livekit]" "livekit-agents[openai,silero]"
    export STANDIN_SECRET=...      # from the StandIn portal
    export LIVEKIT_URL=... LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=...
    export OPENAI_API_KEY=...
    python agent.py dev

Importing the plugin arms it; STANDIN_SECRET starts it. A worker without
that variable behaves exactly as if the plugin were not there, so the same
file serves your web and SIP rooms unchanged.
"""

from __future__ import annotations

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import openai, silero
from standin.plugins import livekit as standin


class Receptionist(Agent):
    """The agent on the line. Ordinary LiveKit - it does not know about Microsoft Teams."""

    def __init__(self, call: standin.CallInfo) -> None:
        # `call` is the Microsoft Teams context StandIn dispatched with, available BEFORE
        # the first audio frame, so the persona can name the caller.
        who = call.caller_name or "the caller"
        super().__init__(
            instructions=(
                f"You are a receptionist on a Microsoft Teams call with {who}. "
                "Keep replies short, warm and spoken-friendly: no markdown, no "
                "bullet lists, no URLs unless asked. If interrupted, stop and "
                "answer the newest question."
            )
        )


server = AgentServer()


@server.rtc_session(agent_name="standin-msteams")
async def entrypoint(ctx: JobContext) -> None:
    # Guard on is_teams_call when one worker also serves web or SIP rooms: a job
    # dispatched by anything else has no Microsoft Teams context to attach.
    info = standin.CallInfo.from_job(ctx)
    if not info.is_teams_call:
        raise RuntimeError("this example only serves Microsoft Teams calls")

    session = AgentSession(
        vad=silero.VAD.load(),
        llm=openai.realtime.RealtimeModel(voice="marin"),
    )

    # The one StandIn line. It binds the Microsoft Teams-only surface onto the session:
    # the caller identity, plus the two data topics carrying call context and
    # the goodbye StandIn wants spoken before it hangs up. The caller's AUDIO
    # needs nothing here - the plugin publishes it into the room, and
    # session.start(room=...) picks it up like any other participant.
    call = await standin.TeamsCall().start(session, ctx=ctx)

    await session.start(agent=Receptionist(call), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
