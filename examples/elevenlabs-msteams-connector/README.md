# ElevenLabs Microsoft Teams connector

Your [ElevenLabs agent](https://elevenlabs.io/docs/agents-platform/overview), answering
Microsoft Teams calls, via [StandIn](https://standin.komaa.com).

Two languages, same behaviour. Pick the one your worker already runs in.

**Python**

```bash
pip install standin-sdk
cp .env.example .env
# fill in .env, then:
set -a; source .env; set +a
python -m standin.plugins.elevenlabs
```

**TypeScript**

```bash
npm install @komaa/standin-sdk
cp .env.example .env
# fill in .env, then:
set -a; source .env; set +a
npx standin-elevenlabs
```

Then expose the port and register the public URL as your StandIn identity's agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## Configure the agent for pcm_16000

In the ElevenLabs dashboard, set the agent's input AND output audio format to **pcm_16000**.
That is exactly what a Microsoft Teams call carries, so nothing resamples anything and the
latency you measure is the model's rather than the transport's.

An agent set to anything else is refused at the first frame, with one clear log line, rather
than producing a whole call of garbled audio.

## Give the agent the rest of the call

Declare these as **client tools** on the agent and it can do more than talk. Nothing to
implement: the connector answers them.

Rather than retyping the table, print the declarations and paste them in:

```bash
python -c "import json, standin.plugins.elevenlabs as e; print(json.dumps(e.client_tools(), indent=2))"
```

| Tool | Parameters | What it does |
|---|---|---|
| `end_call` | none | Hang up. |
| `express` | `emotion` | Set the avatar's expression. |
| `show_image` | `url`, or `dataBase64` + `mime`; plus `caption?`, `durationMs?` | Put a picture on the bot's video tile. |
| `look` | `source?`, `question?` | Look at the caller's screen share or camera. |
| `look_back` | `question?` | Look again at a screen the caller has moved past. |

`look` uploads the frame to the conversation, which stores the caller's screen with
ElevenLabs. It therefore works only while the Microsoft Teams call is being recorded: the
caller has been told the call is being kept, and this is part of what is kept.

A URL given to `show_image` is chosen by the model, which the caller steers, so it is fetched
through the SDK's guard: public hosts only, no private or link-local addresses, and the
address is re-checked at connect time.

## Writing the handler yourself

The connector is a `CallHandler` like any other, so you can build it inside your own worker:

```python
from standin import CallServer
from standin.plugins.elevenlabs import ElevenLabsHandler

server = CallServer(handler_factory=ElevenLabsHandler)
await server.start()
```

See [the documentation](https://docs.komaa.com) for the full setup.
