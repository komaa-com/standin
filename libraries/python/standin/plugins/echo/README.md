# standin.plugins.echo

The smallest [StandIn](https://standin.komaa.com) plugin that answers a real
Microsoft Teams call. **Copy this directory to start your own.**

It echoes the caller's voice back. That makes it the right thing to run before
you suspect your own agent: if the echo answers, your secret, your tunnel and
your StandIn identity are all correct.

It needs no extra. Echo is in the base install, because a base install that
cannot answer a call is not a base install.

```bash
pip install standin-sdk
STANDIN_SECRET=... python -m standin.plugins.echo
```

Then call your StandIn number and talk. You should hear yourself.

## The whole thing

```python
from standin import CallServer, CallSession


class EchoHandler:
    async def on_start(self, session: CallSession) -> None:
        self._call = session

    async def on_caller_audio(self, pcm: bytes) -> None:
        await self._call.send_audio(pcm)

    async def on_goodbye(self, text: str) -> None:
        print(f"goodbye: {text}")


server = CallServer(handler_factory=EchoHandler)
await server.start()
```

Replace `on_caller_audio` with your framework's agent loop and you have a real
plugin. Everything else stays.

## Writing your own

There is one package, so a new plugin is a directory inside it and nothing
else. No new wheel, no new manifest, no workspace member.

1. Copy this directory to `libraries/python/standin/plugins/<name>/`.
2. Add `"<name>"` to `_PLUGINS` in `libraries/python/standin/__init__.py`,
   so `standin.<name>` resolves lazily.
3. If it needs a framework, add one extra to `[project.optional-dependencies]`
   in `libraries/python/pyproject.toml`, so `pip install "standin-sdk[<name>]"`
   works, and add it to `all`.
4. Import that framework through `standin.plugins._lazy`, never at module
   load time. `import standin` must keep working with aiohttp alone.
5. `make check` and open a PR.

Full guide: [CONTRIBUTING.md](https://github.com/komaa-com/standin/blob/main/CONTRIBUTING.md).

[MIT](../../../LICENSE).
