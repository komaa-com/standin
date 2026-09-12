# OpenAI Microsoft Teams connector

An [OpenAI Realtime](https://platform.openai.com/docs/guides/realtime) model, answering
Microsoft Teams calls, via [StandIn](https://standin.komaa.com).

Speech to speech: the model hears the caller's voice rather than a transcript of it, so there
is no recognition step adding latency in the middle.

```bash
npm install @komaa/standin-sdk
cp .env.example .env
set -a; source .env; set +a
npx standin-openai
```

Then expose the port and register the public URL as your StandIn identity's agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## About the sample rate

The Realtime API speaks PCM at 24 kHz and a Microsoft Teams call speaks 16 kHz. The connector
owns that conversion in both directions, using the same resampler the Python SDK uses, driven
by the same shared conformance vectors. You never see it.

## What the model can do

Five call capabilities are declared automatically: `end_call`, `express`, `show_image`,
`look` and `look_back`.

`look_back` answers about a screen the caller has already moved past, and works only
while the Microsoft Teams call is being recorded, because that is the only time earlier
frames are kept at all.

The Realtime model hears but does not see, so `look` needs a vision model. Set
`STANDIN_VISION_API_URL` and `STANDIN_VISION_MODEL` to any OpenAI-compatible endpoint that
accepts images. The frame is sent for inference and not stored, and only the description
comes back.

`OPENAI_VAD_TYPE` decides when the model thinks you have finished: `semantic_vad` listens for
a finished thought, `server_vad` for silence. Semantic interrupts less often mid-sentence.

## Adding tools, or a remote MCP server

```ts
import { CallServer } from "@komaa/standin-sdk";
import { OpenAIHandler, mcpTool } from "@komaa/standin-sdk/openai";

const server = new CallServer({
  handlerFactory: () => new OpenAIHandler({
    tools: [{
      name: "open_ticket",
      description: "Open a support ticket for the caller.",
      parameters: { type: "object", properties: { summary: { type: "string" } }, required: ["summary"] },
      handler: async (params) => `opened ticket for ${String(params.summary)}`,
    }],
    mcpTools: [mcpTool({ server_label: "docs", server_url: "https://mcp.example.com" })],
  }),
});
await server.start();
```

A `tools` entry runs in your worker. An `mcpTools` entry is dialled by OpenAI itself, so
nothing runs here at call time. Approval defaults to never, because a live voice call has no
approval interface and a pending approval is a caller listening to silence.

See [the documentation](https://docs.komaa.com) for the full setup.
