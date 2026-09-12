# Hermes Agent Microsoft Teams connector

Your [Hermes Agent](https://github.com/NousResearch/hermes-agent), answering Microsoft Teams
calls, via [StandIn](https://standin.komaa.com).

There is ONE package to install, into the Python environment that runs Hermes:

```bash
pip install "standin-sdk[hermes-agent]"
cp .env.example .env
```

Fill in `.env`, and merge the plugin block from `config.yaml.example` into your
existing `~/.hermes/config.yaml`. Export the credentials and start the listener:

```bash
set -a
source .env
set +a
hermes msteams-bridge serve
```

Then expose the port and register the public URL as your StandIn identity's
agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## What is StandIn-specific here

Nothing you write. There is no agent file in this example, and that is the
point: Hermes already IS the agent. You enable one plugin entry, and the
assistant you talk to in chat picks up the phone.

```yaml
plugins:
  enabled: [msteams_bridge]
```

Hermes loads `standin.plugins.hermes_agent` in-process through its
`hermes_agent.plugins` entry point, so a call reaches your real agent, with your
tools, your files and your installed skills. No HTTP hop, no second service, no
copy of your configuration.

## How it fits together

```
Microsoft Teams call
        │
        ▼
StandIn service              joins the call, owns the Microsoft side
        │  one HMAC-authenticated WebSocket per call
        ▼
standin.plugins.hermes_agent  answers the dial, runs the realtime session
        │                    (loaded in-process by Hermes)
        │
        ├─► realtime model   the conversation: hears, answers, interrupts
        │
        └─► your Hermes Agent   the work: lookups, files, web, your skills
```

The realtime model is the brain of the conversation. Hermes is reached when the
model calls `hermes_agent_consult`, and never sees audio. The model is good at
talking; Hermes is good at doing.

## Before you call, check

```bash
hermes msteams-bridge status
```

It answers "would a call work right now?" without placing one: the StandIn
secret, the realtime key, and every Hermes surface the call needs. Anything
missing is named, with what it costs.

## Without Hermes

```bash
STANDIN_SECRET=... OPENAI_API_KEY=... python -m standin.plugins.hermes_agent
```

The same listener with no host. The call is answered and the model talks; ask it
to look something up and it says, out loud, that it cannot reach its tools. Run
this first if you are not sure whether the problem is your secret, your tunnel
or your agent.

## In a meeting

The assistant stays silent until somebody addresses it by name, then keeps
answering for twelve seconds without needing the name again. Saying "stop" as a
whole sentence cuts it off in code, whether or not the model would have stopped.

Both behaviours are configured in the `msteams_bridge` config block.
`require_address: false` turns the gate off entirely, which makes the assistant
answer every turn of every meeting - occasionally what you want, usually not.

## Who may call

Deny by default. An empty `allowlist` answers nobody:

```yaml
allowlist: ["00000000-0000-0000-0000-000000000000"]   # AAD object ids
```

Set `allow_all: true` to accept anyone, deliberately. Display-name matching
exists (`allowlist_allow_names`) and is off, because a display name is
caller-supplied and an AAD object id is not.

## Memory between calls

`session_scope` decides what a caller gets:

| value | the agent remembers |
|---|---|
| `per-call` | nothing from last time. The default. |
| `per-thread` | the conversation, per Microsoft Teams chat or meeting. |
| `per-aad` | the person, across every call they make. |

`per-aad` falls back to the call id for a guest or anonymous caller, who has no
AAD object id - so two guests never share one memory.

## Links

- [Plugin README](../../libraries/python/standin/plugins/hermes_agent/README.md)
- [Documentation](https://docs.komaa.com/hermes/installation)
- [StandIn](https://standin.komaa.com)
