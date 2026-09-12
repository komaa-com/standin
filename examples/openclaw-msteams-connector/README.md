# OpenClaw Microsoft Teams connector

The realtime voice provider configured in [OpenClaw](https://openclaw.ai),
answering Microsoft Teams calls via [StandIn](https://standin.komaa.com).

## Set it up

Install the SDK where your OpenClaw gateway can reach it:

```bash
npm install @komaa/standin-sdk
```

Merge `openclaw.json.example` from this directory into your existing
`~/.openclaw/openclaw.json`. Fill in the StandIn secret, the realtime provider
key, and the allowed caller AAD object ids, then start or restart the gateway:

```bash
openclaw gateway
```

Expose the call port and register the public URL as your StandIn identity's
agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## This is a configuration example, not an app

There is no `agent.ts` here, and there is not supposed to be. The plugin runs
**inside** the OpenClaw gateway process: it consumes the host's realtime
speech-to-speech session, provider registry and logger, which are in-process
objects. So the whole of this example is the config block below plus a gateway
restart.

OpenClaw loads a plugin from a **directory**, not a package name, which is why
`plugins.load.paths` points inside `node_modules` rather than naming
`@komaa/standin-sdk`. That directory ships in the package with the plugin
manifest beside its entry, so there is nothing to copy out.

That is also why this directory has an `openclaw.json.example` where the LiveKit
example has an `agent.py`.

The `.env.example` beside it is not read by anything. It is a list of the two
values you would rather not paste into a config file, for wherever your gateway
lets you reference the environment instead.

## The whole config

```json
{
  "plugins": {
    "load": {
      "paths": [
        "node_modules/@komaa/standin-sdk/dist/plugins/openclaw"
      ]
    },
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

`allowFrom` takes the caller's **AAD object id**, not their email. It is empty
for guest and anonymous callers, so an anonymous caller can never be
allowlisted, which is the intended behaviour.

## How it fits together

```
Microsoft Teams call
        │
        ▼
StandIn service               joins the call, owns the Microsoft side
        │  one HMAC-authenticated WebSocket per call
        ▼
@komaa/standin-sdk            answers the dial, speaks the wire protocol
        │
        ▼
.../plugins/openclaw     inside your gateway: resamples both legs,
        │                     guards against echo, handles barge-in
        ▼
OpenClaw realtime session     your voice provider and instructions
```

## Checking it works

The gateway logs one line at boot when the listener is up:

```
standin-msteams: listening on 127.0.0.1:9442/msteams/calling (max 4 concurrent)
```

If instead it says nothing started, the `secret` is missing or unresolved. If it
warns that no realtime voice provider resolved, every call will be refused with
`realtime-unavailable`: set the provider's API key.
