<div align="center">

# StandIn

### Your AI teammate in Microsoft Teams calls

Put your own AI agent into a Microsoft Teams call as a real participant, in your own tenant. It answers and places calls, sees the caller's camera and screen-share, converses in real time, and appears as a lip-synced avatar. Free sandbox, no Azure bot, no card.

[**Try it free**](https://standin.komaa.com) &nbsp;·&nbsp; [**Documentation**](https://docs.komaa.com) &nbsp;·&nbsp; [**Quickstart**](https://docs.komaa.com/quickstart)

</div>

---

## Demo

[![StandIn: set up on OpenClaw, then a live Teams call](assets/standin-demo.gif)](https://github.com/komaa-com/standin/blob/main/assets/standin-demo.mp4)

A 90-second walkthrough: set up StandIn on OpenClaw, then a real Microsoft Teams call where the agent answers, sees the shared screen, speaks when addressed, and appears as a lip-synced avatar on its tile. [Watch the full video with audio](https://github.com/komaa-com/standin/blob/main/assets/standin-demo.mp4).

## What it does

- **Listens and talks back** in real time, inside the ~200 ms window a live conversation needs, and stops the moment it is interrupted.
- **Sees the meeting**: continuous vision over screen-share and camera, with per-participant attribution, so "what do you make of this?" just works.
- **Speaks only when addressed**: stays silent in a group call until called by name, then steps in and steps back.
- **Governed by default**: recording-consent gating, a closed-by-default allowlist, and signed, encrypted media.

It joins under your own Microsoft identity, and no meeting recordings are stored.

## Bring your own agent

StandIn is the hosted bridge that joins the Teams call and handles the media and avatar rendering. You bring the brain. A small open-source bridge connects your existing agent, whatever stack it runs on, so it answers Teams calls with no Teams SDK, no Microsoft Graph, and no media infrastructure to run.

Seven backends, published on npm and PyPI:

| Backend | What it connects | Package |
|---|---|---|
| **OpenClaw** | The OpenClaw framework, as a plugin | [![npm](https://img.shields.io/npm/v/@komaa/openclaw-msteams-bridge?label=%40komaa%2Fopenclaw-msteams-bridge&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/openclaw-msteams-bridge) |
| **Hermes** | The Hermes Agent, as a plugin | [![PyPI](https://img.shields.io/pypi/v/hermes-msteams-bridge?label=hermes-msteams-bridge&color=3775a9&logo=pypi&logoColor=white&cacheSeconds=86400)](https://pypi.org/project/hermes-msteams-bridge/) |
| **ElevenLabs** | A hosted ElevenLabs Agent | [![npm](https://img.shields.io/npm/v/@komaa/elevenlabs-msteams-bridge?label=npm&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/elevenlabs-msteams-bridge) [![PyPI](https://img.shields.io/pypi/v/elevenlabs-msteams-bridge?label=PyPI&color=3775a9&logo=pypi&logoColor=white&cacheSeconds=86400)](https://pypi.org/project/elevenlabs-msteams-bridge/) |
| **LiveKit** | Any LiveKit Agent, including avatar agents | [![npm](https://img.shields.io/npm/v/@komaa/livekit-msteams-bridge?label=npm&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/livekit-msteams-bridge) [![PyPI](https://img.shields.io/pypi/v/livekit-msteams-bridge?label=PyPI&color=3775a9&logo=pypi&logoColor=white&cacheSeconds=86400)](https://pypi.org/project/livekit-msteams-bridge/) |
| **OpenAI** | OpenAI Realtime (`gpt-realtime`) | [![npm](https://img.shields.io/npm/v/@komaa/openai-msteams-bridge?label=%40komaa%2Fopenai-msteams-bridge&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/openai-msteams-bridge) |
| **Deepgram** | A Deepgram Voice Agent | [![npm](https://img.shields.io/npm/v/@komaa/deepgram-msteams-bridge?label=npm&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/deepgram-msteams-bridge) [![PyPI](https://img.shields.io/pypi/v/deepgram-msteams-bridge?label=PyPI&color=3775a9&logo=pypi&logoColor=white&cacheSeconds=86400)](https://pypi.org/project/deepgram-msteams-bridge/) |
| **Cartesia** | A Cartesia Line voice agent | [![npm](https://img.shields.io/npm/v/@komaa/cartesia-msteams-bridge?label=%40komaa%2Fcartesia-msteams-bridge&color=cb3837&logo=npm&cacheSeconds=86400)](https://www.npmjs.com/package/@komaa/cartesia-msteams-bridge) |

The Node and Python packages are interchangeable behind one wire contract, so you can switch backends without rewriting your integration.

## Quickstart

1. Get a free identity at [standin.komaa.com](https://standin.komaa.com). No Azure bot, no card.
2. Connect your own Azure bot and pick the backend that matches your stack from the table above.
3. Follow its guide on [docs.komaa.com](https://docs.komaa.com), point your StandIn identity at the bridge, and place a call.

## How it works

```mermaid
flowchart LR
  T["Microsoft Teams call"] --> S["StandIn bridge<br/>(hosted, joins the call)"]
  S -- "HMAC WebSocket, PCM 16 kHz" --> B["your bridge<br/>(open source, you run it)"]
  B --> A["your AI agent<br/>(any stack)"]
  A --> B --> S --> T
```

The hosted bridge joins the meeting, captures media, and renders the avatar. Your bridge is a small, dependency-light service that terminates an HMAC-authenticated WebSocket on one side and your agent platform on the other. Audio is 16 kHz PCM; the transport is replay-proof and recording-gated.

## The three CVI pillars

A Teams call becomes a true two-way video conversation:

- **Perception**: the agent sees the caller's camera and screen-share, on demand or continuously.
- **Dialogue**: realtime speech-to-speech or streaming STT to agent to TTS, with barge-in and group-call etiquette.
- **Rendering**: a lip-synced animated avatar tile, with expression cues and picture-in-picture image sharing.

## Links

- Website: [standin.komaa.com](https://standin.komaa.com)
- Docs: [docs.komaa.com](https://docs.komaa.com)

---

<div align="center">

StandIn is independent software. It is not affiliated with, endorsed by, or sponsored by Microsoft. Microsoft and Microsoft Teams are trademarks of the Microsoft group of companies.

</div>
