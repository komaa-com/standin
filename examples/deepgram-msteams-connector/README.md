# Deepgram Microsoft Teams connector

A [Deepgram Voice Agent](https://developers.deepgram.com/docs/voice-agent), answering
Microsoft Teams calls, via [StandIn](https://standin.komaa.com).

Two languages, same behaviour. Pick the one your worker already runs in.

**Python**

```bash
pip install standin-sdk
cp .env.example .env
set -a; source .env; set +a
python -m standin.plugins.deepgram
```

**TypeScript**

```bash
npm install @komaa/standin-sdk
cp .env.example .env
set -a; source .env; set +a
npx standin-deepgram
```

Then expose the port and register the public URL as your StandIn identity's agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## Nothing to configure on the Deepgram side

Speech to text, reasoning and speech are all set on the session when the call starts, so
there is no agent to build in a dashboard first. Audio is pinned to linear16 at 16 kHz in
both directions, which is exactly what a Microsoft Teams call carries.

Point the reasoning step at your own model with `DEEPGRAM_THINK_ENDPOINT_URL` and
`DEEPGRAM_THINK_ENDPOINT_HEADERS` if you would rather keep it in house.

## What the agent can do

Five call capabilities are declared automatically: `end_call`, `express`, `show_image`,
`look` and `look_back`.

`look_back` answers about a screen the caller has already moved past, and works only
while the Microsoft Teams call is being recorded, because that is the only time earlier
frames are kept at all.

A Voice Agent hears but does not see, so `look` needs a vision model. Set
`STANDIN_VISION_API_URL` and `STANDIN_VISION_MODEL` to any OpenAI-compatible endpoint that
accepts images, including one you run yourself. The frame is sent for inference and not
stored, and only the description comes back. Without it, the agent is told plainly that
looking is unavailable rather than being left with silence.

## Adding tools of your own

Your tool runs in your worker, so it can reach whatever your worker can reach:

```python
from standin import CallServer
from standin.plugins.deepgram import CustomTool, DeepgramHandler

async def open_ticket(params, ctx):
    return f"opened ticket for {params['summary']}"

tools = [CustomTool(
    name="open_ticket",
    description="Open a support ticket for the caller.",
    handler=open_ticket,
    parameters={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
)]

server = CallServer(handler_factory=lambda: DeepgramHandler(tools=tools))
await server.start()
```

The description is what the model reads to decide whether to call it, so write it for a
model rather than for a developer. Keep the handler fast: the caller is waiting in silence
while it runs.

See [the documentation](https://docs.komaa.com) for the full setup.
