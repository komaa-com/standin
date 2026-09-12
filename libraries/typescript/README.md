# @komaa/standin-sdk

[StandIn](https://standin.komaa.com) for TypeScript: the SDK core and every
framework plugin, in one package.

StandIn is the hosted bridge that joins a Microsoft Teams call. It owns the
Microsoft side entirely, the bot registration, Graph, media negotiation, the
avatar tile, and talks to your worker over one authenticated socket per call.
This package is that socket's other end.

## Install

```bash
npm install @komaa/standin-sdk
```

Node 20 or newer. Nothing else is required to answer a call.

## One package, every entry point

| Import | What it is | Needs installed |
|---|---|---|
| `@komaa/standin-sdk` | The core: `CallServer`, `CallHandler`, `ChatChannel`, the audio and HMAC helpers | nothing but `ws` |
| `@komaa/standin-sdk/echo` | The smallest plugin that answers a real call. Copy it to start your own | nothing but `ws` |
| `@komaa/standin-sdk/elevenlabs` | An ElevenLabs agent takes the call | nothing but `ws` |
| `@komaa/standin-sdk/deepgram` | A Deepgram Voice Agent takes the call | nothing but `ws` |
| `@komaa/standin-sdk/cartesia` | A Cartesia Line agent takes the call | nothing but `ws` |
| `@komaa/standin-sdk/openai` | An OpenAI Realtime model takes the call | nothing but `ws` |
| `@komaa/standin-sdk/livekit` | A LiveKit agent takes the call | `@livekit/rtc-node`, `livekit-server-sdk` |
| `@komaa/standin-sdk/openclaw` | The OpenClaw gateway plugin | `openclaw` |

The core imports **nothing** from `src/plugins/`, so installing this package
never drags a framework in behind you, and `import { CallServer } from
"@komaa/standin-sdk"` works on a machine with no OpenClaw at all. `openclaw` is
an optional peer dependency: you install it because you run a gateway, not
because you installed this.

One package is what keeps the plugins honest with each other. A new
capability lands in the core once and every plugin has it, instead of being
threaded into one package per framework by hand.

## Run the echo agent

```bash
npm install @komaa/standin-sdk
STANDIN_SECRET=... npx standin-echo
```

Call your StandIn number and talk. You should hear yourself. Run this before you
suspect your own agent: if the echo answers, your secret, your tunnel and your
StandIn identity are all correct.

From there, the [OpenClaw example](../../examples/openclaw-msteams-connector) is a
complete deployment, and the [echo plugin](src/plugins/echo) is the
file to copy for a custom one.

The [Python SDK](../python) uses the same call handler contract and wire
protocol. Shared conformance vectors check the behaviour of both SDKs. The
framework plugins are OpenClaw in TypeScript, Hermes in Python, and LiveKit
in both.

## What it gives you

| | |
|---|---|
| `CallServer` | Answers the socket StandIn dials. Owns the HMAC handshake and its replay guard, capacity and draining, the wire protocol, sequence numbers and the audio timeline, and the watchdogs that end a call nobody closed. |
| `CallHandler` | The five-method seam a plugin implements. Every method optional. |
| `VideoFrame` | One frame of what the caller is showing, on the vision lane. |

## Writing a plugin

```ts
import { CallServer, type CallSession } from "@komaa/standin-sdk";

class EchoHandler {
  #call!: CallSession;

  async onStart(session: CallSession) {
    this.#call = session;
  }

  async onCallerAudio(pcm: Buffer) {
    await this.#call.sendAudio(pcm);   // PCM16, 16 kHz, mono
  }
}

const server = new CallServer({ handlerFactory: () => new EchoHandler() });
await server.start();
```

That is a working Microsoft Teams agent. `@komaa/standin-sdk/echo` is this file plus a
signal handler, and `npx standin-echo` runs it.

## Configuration

Environment only.

| Variable | Default | Meaning |
|---|---|---|
| `STANDIN_SECRET` | *(required)* | Connection secret from the StandIn portal. |
| `STANDIN_PORT` | `9442` | Port the call listener binds. |
| `STANDIN_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` when only a local tunnel should reach it. |
| `STANDIN_WS_PATH` | `/msteams/calling` | Path StandIn dials. |

The listener authenticates WebSocket upgrades with HMAC. Terminate TLS at your
public ingress so StandIn can reach it over `wss://`.

## Signing control requests

Use `signRequest` for HTTP control requests. HMAC v2 binds the method, request
path and hash of the entire body, including `tenantId`:

```ts
import { SIGNATURE_V2_HEADER, TIMESTAMP_HEADER, nowMs, signRequest } from "@komaa/standin-sdk";

const timestamp = String(nowMs());
const headers = {
  [TIMESTAMP_HEADER]: timestamp,
  [SIGNATURE_V2_HEADER]: signRequest(secret, timestamp, "POST", "/api/calls", rawBody),
};
```

Serialize the body once and send those same `rawBody` bytes. These helpers
prepare signatures; they do not send HTTP requests. `signBody` / `verifyBody`
are for chat POST bodies, with a 300-second replay window. WebSocket call and
chat-channel handshakes keep `signHandshake` / `verifyHandshake` and their
separate 60-second window.

## Audio

PCM16, 16 kHz, mono, little-endian, both directions. The server owns the
outbound sequence number and timeline, so a handler that swaps or re-publishes
its audio source cannot make timestamps jump backwards.

## Contributing

Building the package from a clone, the layout rules for a new plugin and
the checks CI runs are in
[CONTRIBUTING.md](https://github.com/komaa-com/standin/blob/main/CONTRIBUTING.md).

[MIT](LICENSE).
