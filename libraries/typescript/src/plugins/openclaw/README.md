# `@komaa/standin-sdk/openclaw`

Connect the realtime voice provider configured in [OpenClaw](https://openclaw.ai)
to Microsoft Teams calls through [StandIn](https://standin.komaa.com).

**This is an OpenClaw plugin, not a worker you run.** It installs into OpenClaw
and consumes the host's realtime speech-to-speech session, provider registry and
logger, all of which are in-process objects inside the gateway. OpenClaw's
external HTTP surface is text-only, so a standalone worker could only use
OpenClaw as a text brain and bring its own STT and TTS, which is the part this
plugin exists to avoid.

## Install

Install the package where your OpenClaw gateway can reach it:

```bash
npm install @komaa/standin-sdk
```

Needs OpenClaw 2026.6.10 or newer, and the Node version that gateway requires.

## How OpenClaw loads it

OpenClaw does not import a plugin by package specifier. It is handed a
**directory** through `plugins.load.paths` and discovers what is inside it. So
the plugin ships as a self-describing directory inside this package:

```
node_modules/@komaa/standin-sdk/dist/plugins/openclaw/
  index.js                the entry OpenClaw executes
  openclaw.plugin.json    the manifest OpenClaw discovers it by
  package.json            names the entry and the plugin API floor
```

`index.js` comes from `tsc`. The other two are placed beside it by
`scripts/emit-openclaw-plugin-dir.mjs`, because `tsc` emits only `.ts` and a
manifest one level up is a manifest the host never sees. All three are inside
`files: ["dist"]`, so they are in the published tarball.

Point the gateway at that directory:

```json
{ "plugins": { "load": { "paths": [
    "node_modules/@komaa/standin-sdk/dist/plugins/openclaw"
] } } }
```

## Configure it

Add the entry to `openclaw.json` and restart the gateway:

```json
{
  "plugins": {
    "entries": {
      "standin-msteams": {
        "enabled": true,
        "config": {
          "secret": "sk_standin_REPLACE_ME",
          "inboundPolicy": "allowlist",
          "allowFrom": [
            "00000000-0000-0000-0000-000000000000"
          ],
          "inboundGreeting": "Hi, this is your assistant. How can I help?",
          "callingPort": 9442,
          "bindAddress": "127.0.0.1",
          "path": "/msteams/calling",
          "maxConcurrentCalls": 4,
          "requireRecordingStatus": false,
          "realtime": {
            "provider": "openai",
            "providers": {
              "openai": {
                "apiKey": "sk-REPLACE_ME",
                "voice": "alloy"
              }
            },
            "instructions": "You are on a Microsoft Teams call. Keep answers short and spoken."
          }
        }
      }
    }
  }
}
```

Expose port 9442 and register the public `wss://` URL as your StandIn identity's
agent voice URL. Call your StandIn number.

The [OpenClaw example](https://github.com/komaa-com/standin/tree/main/examples/openclaw-msteams-connector) is the same
configuration with the tunnel and the checks around it.

## What it does

StandIn owns the Microsoft side entirely, the bot registration, Graph, media
negotiation, the avatar tile, and dials
`wss://<your-host>/msteams/calling/{callId}` once per call. The SDK answers that
dial; this plugin bridges the call to your configured realtime provider:

```
caller   16 kHz --> 24 kHz --> OpenClaw realtime session
model    24 kHz --> 16 kHz --> the caller
```

It carries the three things that make a call feel right rather than merely work:

- **Barge-in.** `cancelPlayback()` runs BEFORE the response is cancelled
  upstream, so the caller stops hearing the turn they interrupted. Two triggers:
  the model truncating its own turn, and a deterministic verbal interrupt
  ("stop", "hold on", `توقف`) matched in code because the model is mid-generation
  when one arrives.
- **An echo guard.** On a speakerphone the agent's own voice comes back up the
  caller leg loudly enough for a realtime model's VAD to answer itself. Caller
  input is dropped while our own audio is still playing out, unless it is loud
  enough to be a real interruption.
- **A recording gate.** With `requireRecordingStatus` on, no caller media reaches
  the model until Microsoft Teams reports recording active.

The call carries voice and the realtime provider's own instructions. Reaching the
OpenClaw agent's tools, skills and memory from inside a call is on the roadmap.

## Configuration

| key | meaning |
|---|---|
| `secret` | The StandIn connection secret. Without it nothing starts. |
| `callingPort` / `bindAddress` / `path` | Where the call listener binds. Defaults `9442`, `0.0.0.0`, `/msteams/calling`. |
| `maxConcurrentCalls` | The operator's cap. Default 4. Call 5 is refused with "busy". |
| `inboundPolicy` / `allowFrom` | Who may call. **Defaults to `disabled`, which refuses everyone.** |
| `inboundGreeting` | Spoken on pickup. Omit and the agent waits for the caller. |
| `requireRecordingStatus` | Hold caller media until Microsoft Teams reports recording active. |
| `realtime.*` | Provider selection, instructions, and the echo guard's tunables. |

## Working on the plugin

`openclaw` is an **optional peer** dependency: optional because this lives in
the same package as the SDK core, and most installs want the core alone.
Development pins `openclaw@2026.6.10` from the registry so `tsc` resolves the
real plugin SDK types on every machine, never a `link:` to a sibling checkout,
which would put a path from one developer's disk into the committed lockfile and
break `pnpm install` for everyone else. The plugin runs inside the separately
installed OpenClaw gateway.

The files here import `openclaw` at module scope, which is safe only because
nothing in the SDK core reaches them. Keep it that way.

```bash
pnpm install          # in libraries/typescript
pnpm build
pnpm typecheck
pnpm test
```

`package.test.ts` checks the **built** directory against the validators of the
installed OpenClaw host, so it needs `pnpm build` to have run first. That is
deliberate: an unbuilt load path is exactly the failure an operator would
otherwise meet at gateway boot.

[MIT](../../../LICENSE).
