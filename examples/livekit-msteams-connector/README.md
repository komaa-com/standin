# LiveKit Microsoft Teams connector

A [LiveKit Agent](https://docs.livekit.io/agents/) that answers Microsoft Teams
calls, via [StandIn](https://standin.komaa.com).

There is ONE package to install, and LiveKit is an extra on it. Use Python 3.10
or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install "standin-sdk[livekit]" "livekit-agents[openai,silero]"
cp .env.example .env
```

`agent.py` in this directory is the Python worker, and the rest of this page
walks through it. The plugin also ships in TypeScript with the same behaviour
and the same wire protocol, as a worker you run rather than a file you write:

```bash
npm install @komaa/standin-sdk @livekit/rtc-node

export STANDIN_SECRET="your-StandIn-connection-secret"
export LIVEKIT_URL="wss://your-project.livekit.cloud"
export LIVEKIT_API_KEY="..."
export LIVEKIT_API_SECRET="..."
npx standin-livekit
```

It creates one room per call and dispatches your own LiveKit agent into it, so
the agent you already run needs no change.

Fill in `.env` with your StandIn secret, LiveKit project credentials, and OpenAI
key, then export it before starting the worker:

```bash
set -a
source .env
set +a
python agent.py download-files
python agent.py dev
```

Then expose the port and register the public URL as your StandIn identity's
agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## What is StandIn-specific here

Two lines out of the whole file:

```python
from standin.plugins import livekit as standin
...
call = await standin.TeamsCall().start(session, ctx=ctx)
```

Everything else is the shape every LiveKit agent example already has. Importing
the plugin arms it; `STANDIN_SECRET` starts it. **A worker without that
variable behaves exactly as if the plugin were not there**, so the same file
can serve your web and SIP rooms unchanged.

## How it fits together

```
Microsoft Teams call
        │
        ▼
StandIn service            joins the call, owns the Microsoft side
        │  one HMAC-authenticated WebSocket per call
        ▼
standin.plugins       answers the dial, creates one room per call,
  .livekit                 dispatches YOUR agent into it, relays audio
        │
        ▼
your LiveKit Agent         an ordinary room participant
```

By the time `entrypoint` runs, the call is an ordinary LiveKit room. The
caller's voice is a room track like any other participant's, so the session
needs no special audio wiring.

## What you get in the entrypoint

`TeamsCall().start()` returns `CallInfo` and wires two data topics:

| topic | carries |
|---|---|
| `msteams.context` | participant counts and group-call etiquette, DTMF digits, recording status. Logged by default; pass `on_context=` to handle it. |
| `msteams.goodbye` | the line StandIn wants spoken before it ends the call. The default handler interrupts and says it, teardown follows within seconds. |

`CallInfo` carries `caller_name`, `tenant_id`, `call_id`, `thread_id`,
`user_id`, `direction` and `is_teams_call`.

> `user_id` is the caller's AAD object id and is **empty for guest and anonymous
> callers**. Never use it as a bare key for per-caller memory without checking
> it first, or two anonymous callers share one identity.

## Dispatch

With `agent_name=` set the plugin dispatches explicitly, which is
recommended. Without it, creating the room is itself what assigns the job. It
logs which mode it is in at startup.
