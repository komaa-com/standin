# standin.plugins.livekit

Answer Microsoft Teams calls with a [LiveKit Agent](https://docs.livekit.io/agents/),
via [StandIn](https://standin.komaa.com).

One package holds the SDK and every plugin, so this is an extra rather than
a separate install:

```bash
pip install "standin-sdk[livekit]"
```

See the root-level [LiveKit Microsoft Teams agent example](https://github.com/komaa-com/standin/tree/main/examples/livekit-msteams-connector) for complete setup.

StandIn answers the Microsoft Teams call and dials your worker. This plugin answers that
dial, creates one LiveKit room per call, dispatches your own agent into it, and
relays the audio both ways. By the time your entrypoint runs, the call is an
ordinary LiveKit room, the caller's voice is a room track like any other
participant's.

## Usage

Your file is shaped like every other agent example. Nothing starts except
through `cli.run_app(server)`.

```python
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import openai
from standin.plugins import livekit as standin


class MyAgent(Agent):
    def __init__(self, call: standin.CallInfo) -> None:
        super().__init__(
            instructions=f"You are on a Microsoft Teams call with {call.caller_name}.",
        )


server = AgentServer()


@server.rtc_session(agent_name="standin-msteams")
async def entrypoint(ctx: JobContext):
    session = AgentSession(llm=openai.realtime.RealtimeModel())
    call = await standin.TeamsCall().start(session, ctx=ctx)
    await session.start(agent=MyAgent(call), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
```

Importing `standin.plugins.livekit` arms it. Setting `STANDIN_SECRET`
starts it. A worker without that variable behaves exactly as if the plugin
were not there.

That import is also the only place in the SDK where LiveKit is loaded. Without
the `[livekit]` extra it raises `standin.PluginNotInstalled` naming the
install line above; `import standin` on its own never touches LiveKit at all.

## Configuration

Environment only.

| Variable | Default | Meaning |
|---|---|---|
| `STANDIN_SECRET` | *(required)* | Connection secret from the StandIn portal. Arms the listener. |
| `STANDIN_PORT` | `9442` | Port the call listener binds. |
| `STANDIN_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` when only a local tunnel should reach it. |
| `STANDIN_WS_PATH` | `/msteams/calling` | Path StandIn dials. |
| `LIVEKIT_URL` / `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | *(required)* | Your LiveKit project; the worker already has these. |

Expose the port and register the public `wss://` URL as your StandIn identity's
agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

## What you get in the entrypoint

`TeamsCall().start(session, ctx=ctx)` returns a `CallInfo` and wires two data
topics onto your session:

| | |
|---|---|
| `msteams.context` | Non-interrupting context: participant counts and group-call etiquette, DTMF digits, recording status. Logged by default; pass `on_context=` to handle it. |
| `msteams.goodbye` | StandIn is ending the call and wants this line spoken first. The default handler interrupts the current turn and says it, which is what you want, teardown follows within seconds. |

`CallInfo` carries `caller_name`, `tenant_id`, `call_id`, `thread_id`,
`user_id`, `direction`, and `is_teams_call`. Guard on `is_teams_call` when one
worker serves Microsoft Teams rooms alongside your web or SIP rooms:

```python
info = standin.CallInfo.from_job(ctx)
if info.is_teams_call:
    call = await standin.TeamsCall().start(session, ctx=ctx)
```

`user_id` is the caller's AAD object id and is **empty for guest and anonymous
callers**, never use it as a bare key for per-caller memory without checking
it first, or two anonymous callers share one identity.

## Dispatch

With `agent_name=` set, this dispatches explicitly, recommended. Without
it, it relies on automatic dispatch, where creating the room is itself
what assigns the job. It logs which mode it is in at startup.

## Links

- [Documentation](https://docs.komaa.com/livekit/installation)
- [StandIn](https://standin.komaa.com)

[MIT](../../../LICENSE).
