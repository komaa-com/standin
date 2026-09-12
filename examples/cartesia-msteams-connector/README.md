# Cartesia Microsoft Teams connector

Your [Cartesia Line agent](https://docs.cartesia.ai/line), answering Microsoft Teams calls,
via [StandIn](https://standin.komaa.com).

Two languages, same behaviour. Pick the one your worker already runs in.

**Python**

```bash
pip install standin-sdk
cp .env.example .env
set -a; source .env; set +a
python -m standin.plugins.cartesia
```

**TypeScript**

```bash
npm install @komaa/standin-sdk
cp .env.example .env
set -a; source .env; set +a
npx standin-cartesia
```

Then expose the port and register the public URL as your StandIn identity's agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

## Your agent stays your agent

Cartesia runs the agent, so this connector is transport and nothing else. There are no call
tools to declare here, because what the agent can do it does in its own code on Cartesia's
platform.

What reaches that code:

- **Who is calling**, as stream metadata: `callId`, `callerName`, `tenantId` and `direction`.
- **Call context**, as `custom` events: participant counts, recording changes, and the
  closing line when Microsoft Teams ends the call.
- **Key presses**, as real `dtmf` events.

`CARTESIA_SYSTEM_PROMPT` is deliberately optional. Leave it unset and the agent keeps exactly
the prompt you wrote on Cartesia's side; set it and the caller's details are appended to
yours. Nothing here ever silently rewrites a prompt you deployed.

Audio is pinned to `pcm_16000` in both directions, which is what a Microsoft Teams call
carries, so nothing resamples anything.

## Credentials

Your API key mints a short-lived token over HTTPS and that token authenticates the call
socket, so the long-lived key never rides a per-call connection.

See [the documentation](https://docs.komaa.com) for the full setup.
