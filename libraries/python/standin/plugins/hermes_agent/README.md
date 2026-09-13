# standin.plugins.hermes_agent

Answer Microsoft Teams calls with your [Hermes Agent](https://github.com/NousResearch/hermes-agent)
agent, via [StandIn](https://standin.komaa.com).

One package holds the SDK and every plugin, so this is an extra rather than
a separate install:

```bash
pip install "standin-sdk[hermes-agent]"
```

Install it into the Python environment that runs Hermes. Hermes Agent ships its
own host, so that is all the extra has to add: it installs the adapter, and
Hermes loads it through the entry point below. Nothing here imports the host
while loading, so `import standin` and `import standin.plugins.hermes_agent` both
work on a machine that has never seen Hermes.

See the root-level [Hermes Microsoft Teams agent example](https://github.com/komaa-com/standin/tree/main/examples/hermes-msteams-connector) for complete setup.

StandIn answers the Microsoft Teams call and dials your worker. This plugin answers that
dial, connects a realtime speech-to-speech model for the conversation, and gives
that model one door into Hermes, so the caller talks to the same assistant they
know from chat, with their own tools, files and skills behind it.

Hermes loads this plugin **in-process**, through the `hermes_agent.plugins` entry
point. There is no HTTP hop to Hermes, no session header, and no second service
to run.

## Who is the brain

The realtime model is. It hears the caller and answers them, at conversational
latency. Hermes is reached only when the model calls `hermes_agent_consult`, and
it never sees audio.

```
Microsoft Teams call
        │
        ▼
StandIn service              joins the call, owns the Microsoft side
        │  one HMAC-authenticated WebSocket per call
        ▼
standin.plugins.hermes_agent  answers the dial, runs the realtime session
        │
        ├─► realtime model   the conversation: hears, answers, interrupts
        │
        └─► Hermes agent     the work: lookups, files, web, your skills
                             (in-process, via hermes_agent_consult)
```

The model is good at conversation, Hermes is good at work. That split is the
whole design.

## Setup

```yaml
# <hermes home>/config.yaml
plugins:
  enabled: [msteams_bridge]
  entries:
    msteams_bridge:
      config:
        # Deny by default. Put the AAD object ids that may call.
        allowlist: ["00000000-0000-0000-0000-000000000000"]
        session_scope: per-aad
        realtime:
          voice: alloy
```

```bash
export STANDIN_SECRET=...      # from the StandIn portal
export OPENAI_API_KEY=...      # the realtime model
hermes msteams-bridge serve
```

Then expose the port and register the public URL as your StandIn identity's
agent voice URL:

```bash
tailscale funnel --bg --set-path /msteams/calling http://127.0.0.1:9442/msteams/calling
```

Call your StandIn number.

`hermes msteams-bridge status` answers "would a call work right now?" without
placing one: it checks the secret, the realtime key and every Hermes surface the
call needs, and names what is missing.

## Running without Hermes

```bash
STANDIN_SECRET=... OPENAI_API_KEY=... python -m standin.plugins.hermes_agent
```

The same listener with no host. The call is answered and the model talks;
`hermes_agent_consult` replies, in words, that it cannot reach its tools. Useful
for checking a secret, a tunnel and a StandIn identity before standing up Hermes.

## Configuration

The `plugins.entries.msteams_bridge.config` block, with a `MSTEAMS_BRIDGE_*`
environment variable as the fallback for each key. Both are supported because
both already exist in the field.

| key | env | default | meaning |
|---|---|---|---|
| `allowlist` | `MSTEAMS_BRIDGE_ALLOWLIST` | *(empty)* | AAD object ids that may call. **Empty denies everyone** unless `allow_all`. |
| `allow_all` | `MSTEAMS_BRIDGE_ALLOW_ALL` | `false` | Accept any caller. Explicit opt-in. |
| `allowlist_allow_names` | `MSTEAMS_BRIDGE_ALLOWLIST_ALLOW_NAMES` | `false` | Match display names too. Spoofable; off by default. |
| `require_recording` | `MSTEAMS_BRIDGE_REQUIRE_RECORDING` | `true` | Wait for the Microsoft Teams recording banner before speaking or listening. |
| `meeting_recap` | `MSTEAMS_BRIDGE_MEETING_RECAP` | `false` | After the call ends, post minutes to the Microsoft Teams chat. Needs the StandIn chat lane (managed bot) and a summarization consult. Hang-up does not await the post. Meeting recaps are best-effort. They may be lost if the worker exits during processing. If the host gave no `respond`, the plugin opens a listen-only chat lane that posts and never answers. Restart-recoverable local spool of customer meeting data (`STANDIN_RECAP_DIR`). |
| `session_scope` | `MSTEAMS_BRIDGE_SESSION_SCOPE` | `per-call` | Memory of the agent session used for consults and the minutes: `per-call`, `per-thread`, `per-aad`. |
| `wake_phrases` | `MSTEAMS_BRIDGE_WAKE_PHRASES` | `assistant, hermes` | What addresses the assistant in a meeting. |
| `require_address` | `MSTEAMS_BRIDGE_REQUIRE_ADDRESS` | `true` | Stay silent in a meeting until addressed. |
| `follow_up_window_ms` | `MSTEAMS_BRIDGE_FOLLOW_UP_WINDOW_MS` | `12000` | How long an addressed turn keeps the floor. |
| `consult_timeout_s` | `MSTEAMS_BRIDGE_CONSULT_TIMEOUT_S` | `45` | How long one agent consult may take. |
| `consult_model` | `MSTEAMS_BRIDGE_CONSULT_MODEL` | *(host's `model:`)* | Override the consult's model. |

The `realtime:` sub-block configures the provider:

| key | env | default |
|---|---|---|
| `backend` | `MSTEAMS_BRIDGE_REALTIME_BACKEND` | OpenAI; set `azure` for Azure OpenAI |
| `api_key` | `MSTEAMS_BRIDGE_REALTIME_API_KEY` | `OPENAI_API_KEY`, or `AZURE_OPENAI_API_KEY` / `AZURE_FOUNDRY_API_KEY` |
| `model` | `MSTEAMS_BRIDGE_REALTIME_MODEL` | `gpt-realtime` |
| `voice` | `MSTEAMS_BRIDGE_REALTIME_VOICE` | `alloy` |
| `azure_endpoint` / `azure_deployment` | `MSTEAMS_BRIDGE_AZURE_ENDPOINT` / `_DEPLOYMENT` | none |
| `languages` | `MSTEAMS_BRIDGE_LANGUAGES` | detect and mirror the caller |
| `input_transcribe_model` | `MSTEAMS_BRIDGE_INPUT_TRANSCRIBE_MODEL` | `whisper-1`; `off` disables it |

The listener itself belongs to the SDK: `STANDIN_SECRET`, `STANDIN_PORT` (9442),
`STANDIN_HOST` (0.0.0.0), `STANDIN_WS_PATH` (`/msteams/calling`).

> Turning `input_transcribe_model` off disables the group gate and verbal
> interrupts as well: both read the caller's transcript. The plugin logs a
> warning when it starts a call that way.

## What the model can call

| tool | does |
|---|---|
| `hermes_agent_consult` | Runs the caller's real Hermes agent: lookups, files, web, tools, installed skills. Returns a short spoken result. |
| `set_call_language` | Pins the call to one language, applied to the session in flight. |

## In a meeting

The assistant stays silent until somebody says one of the wake phrases, then
keeps answering for `follow_up_window_ms` without needing the name again. Saying
"stop", "wait", "توقف" or "arrête" as a whole utterance cuts playback in code,
whether or not the model would have stopped.

## Two details worth knowing

**The goodbye interrupts.** StandIn sends its closing line when it is about to
end the call, so it almost always arrives while the model is mid-answer. A plain
say would be dropped: realtime response creation is guarded on "is a response
already active", and at that moment one always is. This plugin cancels the
active response first and then speaks, through `interrupt_and_say`, so the
caller hears the line before the call goes down.

**The group gate reads the thread id, not the participant count.** Microsoft Teams gives a
meeting or channel conversation a thread id beginning `19:`, and it is present
in `session.start` on every call, including the meeting-join path where no
participant count arrives at all. So the thread id is the primary signal for "am
I in a meeting", with the participant count kept as a second, corroborating one:
it can add certainty, never remove it.

## Scope

This plugin runs the conversation on a realtime speech-to-speech model, and
delegates work to Hermes through `hermes_agent_consult`. The tool set is the two
tools above, matching the call seam it speaks: audio in, audio out, plus call
context and the goodbye.

A streaming mode, speech to text into Hermes and back out through text to speech,
is on the roadmap for operators who would rather not use a realtime provider. It
lands beside the realtime handler: the config, the gate, the echo guard and the
Hermes boundary are already shared.

## Links

- [Documentation](https://docs.komaa.com/hermes/installation)
- [StandIn](https://standin.komaa.com)

[MIT](../../../LICENSE).
