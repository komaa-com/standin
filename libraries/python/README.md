# standin-sdk

One package. [StandIn](https://standin.komaa.com) in Python, core and every
plugin, in a single import.

StandIn is the hosted bridge that joins a Microsoft Teams call. It owns the
Microsoft side entirely, the bot registration, Graph, media negotiation, the
avatar tile, and talks to your worker over one authenticated socket per call.
This package is that socket's other end.

```python
from standin import CallServer, CallSession, ChatChannel, FrameAligner
from standin.plugins.livekit import TeamsCall
```

## Install

```bash
pip install standin-sdk
```

That one line is already enough for ElevenLabs, Deepgram and Cartesia: they are
reached over an ordinary WebSocket, so they need nothing beyond aiohttp, which
is the only thing the base install pulls.

```bash
STANDIN_SECRET=... ELEVENLABS_API_KEY=... ELEVENLABS_AGENT_ID=... \
  python -m standin.plugins.elevenlabs
```

Add an extra only for a framework that runs inside your process:

```bash
pip install "standin-sdk[livekit]"
pip install "standin-sdk[hermes-agent]"
```

Each plugin has a runnable example at the root of the repo:
[ElevenLabs](../../examples/elevenlabs-msteams-connector),
[Deepgram](../../examples/deepgram-msteams-connector),
[Cartesia](../../examples/cartesia-msteams-connector),
[LiveKit](../../examples/livekit-msteams-connector) and
[Hermes Agent](../../examples/hermes-msteams-connector). Start a custom
plugin from [echo](standin/plugins/echo). OpenAI and OpenClaw are
TypeScript, in [the other half of the repo](../typescript).

## One package, on purpose

A call surface is never done: screen share, call back, camera, chat, managed
chat, adaptive cards. Every one of them has to reach every framework StandIn
supports. Split across a wheel per framework, each surface costs N hand-threaded
releases and N version matrices; here it costs one directory under
`standin/plugins/` and one line in `standin/__init__.py`.

The base install stays small anyway, because that is what extras are for:

| Install | You get |
|---|---|
| `pip install standin-sdk` | The core, and every plugin reached over a socket: echo, ElevenLabs, Deepgram and Cartesia. aiohttp is the only dependency. |
| `pip install "standin-sdk[livekit]"` | The above, plus livekit-agents. |
| `pip install "standin-sdk[hermes-agent]"` | The above, plus the Hermes adapter. Hermes Agent itself ships the host and loads the adapter in-process. |
| `pip install "standin-sdk[all]"` | Everything. |

`import standin` never imports a framework. Plugins load the first time you
name one, so LiveKit code on disk costs a Hermes user nothing, and a missing
extra raises `PluginNotInstalled` with the install line in it, not a
`ModuleNotFoundError` from inside somebody else's package.

## What it gives you

| | |
|---|---|
| `CallServer` | Answers the socket StandIn dials. Owns the HMAC handshake and its replay guard, capacity and draining, the wire protocol, sequence numbers and the audio timeline, and the watchdogs that end a call nobody closed. |
| `CallHandler` | The five-method seam a plugin implements. Every method optional. |
| `VideoFrame` | One frame of what the caller is showing, on the vision lane. |
| `ChatChannel` | The Microsoft Teams messages lane. Dialed **out** from your worker, so chat needs no listener, no open port, and no bot credential of your own. |

## Writing a plugin

The whole contract is five methods, and you implement only the ones you need:

```python
from standin import CallServer, CallSession


class EchoHandler:
    async def on_start(self, session: CallSession) -> None:
        self._call = session

    async def on_caller_audio(self, pcm: bytes) -> None:
        await self._call.send_audio(pcm)  # PCM16, 16 kHz, mono


server = CallServer(handler_factory=EchoHandler)
await server.start()
```

This illustrates the handler contract. [echo](standin/plugins/echo) adds
the runnable entry point and keeps the listener alive:

```bash
STANDIN_SECRET=... python -m standin.plugins.echo
```

Call your number and you hear yourself. Run that before you suspect your own
agent: if the echo answers, your secret, your tunnel and your StandIn identity
are all correct.

Everything that is the same for every framework lives in `CallServer`, which is
why plugins stay small. Everything that differs, what runs the agent, is
yours.

## Configuration

Environment only, matching how the plugins read their keys.

| Variable | Default | Meaning |
|---|---|---|
| `STANDIN_SECRET` | *(required)* | Connection secret from the StandIn portal. Arms the listener. |
| `STANDIN_PORT` | `9442` | Port the call listener binds. |
| `STANDIN_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` when only a local tunnel should reach it. |
| `STANDIN_WS_PATH` | `/msteams/calling` | Path StandIn dials. |
| `STANDIN_CHAT_URL` | `wss://teams.standin.komaa.com/api/chat/channel` | Chat channel the worker dials out to. |

The listener authenticates WebSocket upgrades with HMAC. Terminate TLS at your
public ingress so StandIn can reach it over `wss://`.

## Signing control requests

Use `sign_request` for HTTP control requests. HMAC v2 binds the method, request
path and hash of the entire body, including `tenantId`:

```python
from standin import SIGNATURE_V2_HEADER, TIMESTAMP_HEADER, now_ms, sign_request

timestamp = str(now_ms())
headers = {
    TIMESTAMP_HEADER: timestamp,
    SIGNATURE_V2_HEADER: sign_request(secret, timestamp, "POST", "/api/calls", raw_body),
}
```

Serialize the body once and send those same `raw_body` bytes. These helpers
prepare signatures; they do not send HTTP requests. `sign_body` / `verify_body`
are for chat POST bodies, with a 300-second replay window. WebSocket call and
chat-channel handshakes keep `sign_handshake` / `verify_handshake` and their
separate 60-second window.

## Audio

PCM16, 16 kHz, mono, little-endian, both directions. The server owns the
outbound sequence number and timeline, so a handler that swaps or re-publishes
its audio source cannot make timestamps jump backwards.

## The layout

```
standin/
  __init__.py        the public API, and the lazy hook that keeps it cheap
  call_server.py  handler.py  chat.py  audio.py  protocol.py  ...
  vision.py          what the caller shows you, and what you show back
  avatar.py          the face the caller sees: expression and lip-sync
  fetch.py           fetching a URL a model chose, safely
  plugins/
    echo/            answers a call with the caller's own voice. No extra.
    elevenlabs/      an ElevenLabs agent takes the call. No extra.
    deepgram/        a Deepgram Voice Agent takes the call. No extra.
    cartesia/        a Cartesia Line agent takes the call. No extra.
    livekit/         a LiveKit Agent takes the call.
    hermes/          a Hermes agent takes the call, in the Hermes process.
```

## Links

- [Documentation](https://docs.komaa.com)
- [StandIn](https://standin.komaa.com)
- [Source](https://github.com/komaa-com/standin)

[MIT](LICENSE).
