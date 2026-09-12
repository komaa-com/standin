# `@komaa/standin-sdk/echo`

The smallest [StandIn](https://standin.komaa.com) plugin that answers a real
Microsoft Teams call. **Copy this directory to start your own.**

It echoes the caller's voice back. That makes it the right thing to run before
you suspect your own agent: if the echo answers, your secret, your tunnel and
your StandIn identity are all correct.

```bash
npm install @komaa/standin-sdk
STANDIN_SECRET=... npx standin-echo
```

Then call your StandIn number and talk. You should hear yourself.

## The whole plugin

```ts
import { CallServer, type CallSession } from "@komaa/standin-sdk";

class EchoHandler {
  #call!: CallSession;
  async onStart(session: CallSession) { this.#call = session; }
  async onCallerAudio(pcm: Buffer) { await this.#call.sendAudio(pcm); }
  async onGoodbye(text: string) { console.info(`goodbye: ${text}`); }
}

const server = new CallServer({ handlerFactory: () => new EchoHandler() });
await server.start();
```

Replace `onCallerAudio` with your framework's agent loop and you have a real
plugin. Everything else stays.

## Writing your own

A new plugin is a directory beside this one. Clone the repository, then:

1. Copy this directory to `libraries/typescript/src/plugins/<name>/`.
2. Add a `"./<name>"` entry to `exports` in `libraries/typescript/package.json`.
3. Import your framework **inside** the function or method that needs it, never
   at module scope, unless nothing outside the plugin can reach that file.
   The core must keep importing on a machine that has no framework installed.
4. Add any heavy vendor dependency as an optional peer, not a dependency.
5. `pnpm install` at `libraries/typescript/`, then `make ts-check`.
6. Open a PR.

The Python twin of this directory is `standin.plugins.echo`. Full guide:
[CONTRIBUTING.md](https://github.com/komaa-com/standin/blob/main/CONTRIBUTING.md).

[MIT](../../../LICENSE).
