# Contributing to StandIn

Thank you for contributing. This guide covers setting up your environment, deciding where your change
belongs, and getting it merged.

StandIn puts your AI agent into a Microsoft Teams call. This repository is the whole open-source side:
both SDKs, every plugin, and the wire protocol they speak. One place to open an issue, one place to
send a pull request.

No contribution is too small.

---

## Contribution priorities

We value contributions in this order:

1. **Bug fixes.** Dropped calls, audio glitches, leaked connection slots, anything that loses a caller.
   Always top priority.
2. **Cross-language parity.** A capability that exists in one SDK and not the other is a bug. Change
   both halves in the same pull request: snake_case in Python, camelCase in TypeScript.
3. **Security hardening.** Signature handling, replay windows, caller policy, anything that fails open.
   See [Security](#security-considerations).
4. **New plugins.** A framework we do not support yet. This is the most useful new work.
5. **Robustness.** Reconnection, backpressure, graceful degradation when a provider misbehaves.
6. **Documentation.** Fixes, clarifications, and new examples.

---

## Before you start: search first

A minute of searching saves a wasted afternoon and keeps the queue clean.

```bash
gh search issues --repo komaa-com/standin "<your terms>"
gh search prs --repo komaa-com/standin --state all "<your terms>"
```

- Search **merged** pull requests too, not only open ones.
- The issue tracker lags the code. Grep the source before proposing a capability; it may already exist.
- If an open pull request already covers it, review or improve that one rather than opening a rival.
- For anything large, comment on the issue first so nobody duplicates your work.

---

## Where does your change belong?

```
protocol/                 The wire schema, its generator, and the conformance
                          vectors both SDKs run. It sits ABOVE the languages
                          because it belongs to neither.

libraries/
  python/                 ONE package -> standin-sdk, imported as `standin`
    standin/
      call_server.py handler.py chat.py audio.py _hmac.py protocol.py
      vision.py avatar.py fetch.py
      plugins/       echo/ elevenlabs/ deepgram/ cartesia/
                          livekit/ hermes/
    tests/
  typescript/             ONE package -> @komaa/standin-sdk
    src/
      callServer.ts handler.ts chat.ts audio.ts hmac.ts protocol.ts
      vision.ts avatar.ts fetch.ts
      plugins/       echo/ elevenlabs/ deepgram/ cartesia/
                          openai/ livekit/ openclaw/

examples/                 Root-level examples named by plugin
docs/                     The documentation site
```

The two halves are the **same shape**. Learn one, you know the other.

### One package per language, plugins inside

This is deliberate, and it is the opposite of what some other projects do.

StandIn ships capabilities quickly: screen share, call-back, camera, chat, adaptive cards, and
speech-to-speech next. A capability must land **once** rather than being threaded by hand into a separate
package per framework. So every plugin lives inside the one package, and there is exactly one
manifest per language.

Size is controlled by extras, not by splitting packages. The base install is dependency-light, and
`pip install "standin-sdk[livekit]"` pulls a framework only when asked.

In practice most plugins cost nothing at all. A provider reached over a WebSocket
(ElevenLabs, Deepgram, Cartesia, OpenAI) needs only what the SDK already depends on, so it
ships in the base install with no extra. An extra is for a plugin that runs a framework
INSIDE your process, which so far means LiveKit.

> If you have contributed to a project that asks you to publish plugins as standalone plugin repos,
> note that we ask the opposite. That model protects a fast-moving core from third-party churn. Ours is a
> stable transport, and our cost is fan-out, not churn.

**The rule that makes one package safe:** no module under `plugins/` may import its framework at
module load time. `import standin` must succeed for someone who has installed none of them. Verified by
`tests/test_one_package.py`.

Directory names are short on purpose. The repository is already called `standin`, so a directory named
`standin-plugins-livekit` says it a third time before you reach any code. Published names are declared in
the single `pyproject.toml` or `package.json`.

---

## Development setup

Requires Python 3.10 or newer, Node.js 22.19 or newer,
[uv](https://docs.astral.sh/uv/) and [pnpm](https://pnpm.io/installation).

```bash
make install       # the Python package with all extras, plus pnpm install
make hooks         # point git at .githooks, so a push runs make check first
make check         # protocol drift, both test suites, both linters, and the docs site
make conformance   # just the shared protocol vectors, in both languages
make fix           # autofix Python formatting and lint
```

`make hooks` is worth running once. It sets `core.hooksPath`, so `git push` runs the same `make
check` CI runs and blocks a push that would go red. `git push --no-verify` skips it when you mean
to. Optionally, `pip install pre-commit && pre-commit install` adds the per-commit hygiene in
`.pre-commit-config.yaml`: formatting, line endings, large files, and a credential scan.

`make install` puts the clone itself into the environment, so the code you edit is the code the
tests run. That is the one place a source install belongs: everywhere else, install the published
packages with `pip install standin-sdk` or `npm install @komaa/standin-sdk`.

`make check` needs **no API keys, no Microsoft tenant, and no StandIn account**. The test suite stands up
a real `CallServer`, signs a handshake the way StandIn does, and drives a whole call over a local socket,
so a pull request from a fork passes CI without any secrets. Tests needing a live service are marked and
skipped by default.

---

## The call handler

The whole contract is seven callbacks, and you implement only the ones you need. Five are on
`CallHandler` in both languages. The other two, `on_video_frame` and `on_speaker_change`, are
declared on `CallHandler` in TypeScript and on the separate `VideoHandler` and `SpeakerHandler`
protocols in Python: adding a member to a `@runtime_checkable` Protocol breaks `isinstance` for
every handler that already exists, so Python splits them and TypeScript, whose optional members
cost nothing, does not.

**Python**

```python
from standin import CallServer, CallSession


class MyHandler:
    async def on_start(self, session: CallSession) -> None:
        self._call = session

    async def on_caller_audio(self, pcm: bytes) -> None:
        reply = await my_framework.respond(pcm)      # your agent
        await self._call.send_audio(reply)


server = CallServer(handler_factory=MyHandler)
await server.start()
```

**TypeScript**

```ts
import { CallServer, type CallSession } from "@komaa/standin-sdk";

class MyHandler {
  #call!: CallSession;

  async onStart(session: CallSession) {
    this.#call = session;
  }

  async onCallerAudio(pcm: Buffer) {
    await this.#call.sendAudio(await myFramework.respond(pcm));
  }
}

const server = new CallServer({ handlerFactory: () => new MyHandler() });
await server.start();
```

Same seam, same seven callbacks, each language's casing. The two video and speaker callbacks sit on
their own protocols in Python, for the reason above; the server calls whichever of the seven your
handler happens to have.

## Adding a plugin

### Python

1. Copy `libraries/python/standin/plugins/echo/` to
   `libraries/python/standin/plugins/<name>/`. It is under 100 lines and answers a real call.
2. If it needs a framework, add an extra to `[project.optional-dependencies]` in
   `libraries/python/pyproject.toml`, so `pip install "standin-sdk[<name>]"` works. A provider
   you reach over a WebSocket needs none: aiohttp is already there.
3. Import that framework **lazily**, inside the function that needs it, and raise
   `PluginNotInstalled` when it is missing. See `standin/plugins/_lazy.py`.
4. Add a runnable example at `examples/<name>-msteams-connector/`.
5. `make check`, then open a pull request.

### TypeScript

1. Copy `libraries/typescript/src/plugins/echo/` to
   `libraries/typescript/src/plugins/<name>/`.
2. Add a subpath entry to `exports` in `libraries/typescript/package.json`.
3. Keep the framework out of the core: `import { CallServer } from "@komaa/standin-sdk"` must work with
   your framework absent.
4. Add a runnable example at `examples/<name>-msteams-connector/`.
5. `make check`, then open a pull request.

There is exactly **one manifest per language**. Do not create a second `pyproject.toml` or
`package.json`. If your plugin seems to need one, say so in the pull request rather than adding it.

---

## Changing the wire protocol

Both SDKs use generated protocol modules. Do not edit them by hand.

1. Change `protocol/schema.yaml`, then regenerate with `make protocol-generate`.
2. Add a behaviour regression to `protocol/conformance.json` and run `make conformance`.
3. Run `make check`, which also checks the snapshot and generated files for drift.

Write the conformance vector **first**. One language will fail, and that tells you which to fix.

See [protocol/README.md](protocol/README.md) for the full contract and the compatibility policy.

---

## Code style

- Docstrings and comments explain **why**, not what. A comment earns its place by recording a decision or
  a trap, never by restating the line below it.
- Document every public class and method. The docstrings are the API docs.
- **No em dashes** anywhere: code, comments, documentation or user-facing text. Use a hyphen, comma or
  colon.
- MIT headers matching [LICENSE](LICENSE).
- Formatting and linting are enforced by `make check`. Run `make fix` first.

---

## Security considerations

The SDK authenticates the **connection**, not the person on the call.

- Signature handling must fail closed. Missing secret, timestamp or signature returns false, never true.
- Comparison is constant time, with a length check first.
- Caller policy belongs to a plugin, not the SDK. Remember that `caller.aad_id` is **empty** for
  guest and anonymous callers, so `if caller.aad_id and caller.aad_id not in allowed` fails open for
  exactly the callers you least want to admit.
- Never log a secret, a signature, or a raw caller identifier.

Report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

---

## Pull requests

### Branch naming

```
fix/description        # Bug fixes
feat/description       # New features
docs/description       # Documentation
test/description       # Tests
refactor/description   # Code restructuring
```

### Before submitting

1. Run `make check`.
2. If you changed behaviour in one language, change it in the other, in the same pull request.
3. Keep the pull request focused. One logical change. Do not mix a fix with a refactor.

### Description

Include what changed and why, how to test it, and a reference to any related issue.

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <description>
```

| Type | Use for |
|---|---|
| `fix` | Bug fixes |
| `feat` | New features |
| `docs` | Documentation |
| `test` | Tests |
| `refactor` | Restructuring with no behaviour change |
| `chore` | Build, CI, dependency updates |

Scopes: `sdk`, `protocol`, `audio`, `video`, `chat`, `security`, `elevenlabs`, `deepgram`,
`cartesia`, `openai`, `livekit`, `hermes`, `openclaw`, `echo`, `docs`.

```
fix(audio): keep the frame aligner residual across a barge-in
feat(chat): add the messages lane to the TypeScript SDK
fix(security): reject a non-ASCII signature instead of raising
```

Add an entry to `CHANGELOG.md` under `## Unreleased` for anything a user would notice. A refactor,
a test or a docs-only change does not need one; say so in the pull request instead.

---

## Releasing

Both packages carry one version and go out together, so there is one number to change and one tag
to push.

```bash
make release-check   # builds both, and refuses a release whose versions disagree
```

It checks the three places the version is written: `libraries/python/standin/version.py`,
`libraries/typescript/package.json` and `libraries/typescript/src/version.ts`. The last is what
`VERSION` reports at runtime, and nothing in the build ties it to the others, so it drifts silently
and the package tells the truth about itself only by luck. The check also builds both artifacts and
runs `twine check`, which catches a readme PyPI will reject before the version is burned rather than
at upload.

To release: bump all three, update `CHANGELOG.md`, merge, then tag `vX.Y.Z` on the merge commit. The
`Release` workflow verifies everything again and then waits. Each upload job names a GitHub
Environment, and with required reviewers set on `pypi` and `npm` a pushed tag pauses for a human in
the Actions tab rather than uploading. You can also run the workflow by hand: it is a dry run that
builds both packages and uploads nothing unless you type `publish`.

PyPI uses trusted publishing, so there is no API token in the repository. npm uses `NPM_TOKEN` and
publishes with provenance, which records on the package page which commit and which workflow built
the tarball.

<!-- Neither package has been published yet. The first release is a deliberate decision, not a
     consequence of merging something. -->

---

## Reporting issues

Use [GitHub Issues](https://github.com/komaa-com/standin/issues). Include your operating system, your
Python or Node version, which plugin you are using, and the full error. Steps to reproduce matter
more than anything else.

Issues labelled [good first issue](https://github.com/komaa-com/standin/labels/good%20first%20issue) are
scoped to be a first contribution.

For security vulnerabilities, report privately instead. See [SECURITY.md](SECURITY.md).

---

## Questions

Hosted service and account questions: [standin.komaa.com](https://standin.komaa.com) or the docs at
[docs.komaa.com](https://docs.komaa.com). Anything about an SDK or a plugin: open an issue here.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
