# Changelog

Both packages share one version and ship together, so one file covers both.
`standin-sdk` on PyPI and `@komaa/standin-sdk` on npm always carry the same
number, and `make release-check` refuses a release where they do not.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## 0.1.1 - 2026-09-13

### Added

- Hang-up meeting recap on the OpenClaw and Hermes plugins (`meetingRecap` /
  `meeting_recap`), with a local spool so unfinished minutes can send after a
  restart.
- `listen_only` / `listenOnly` on `ChatChannel`, so recap can post without
  answering chat.
- Agent Skills: `npx skills add komaa-com/skills`.

### Changed

- Docs for recap, connection modes, and expose stay on the public contract.

## 0.1.0 - 2026-09-12

First release. Everything below is what the SDK contains rather than a delta,
since there is nothing before it.

The version is `0.1.0` and the Python package is classified Beta on purpose.
The surface is tested and in use, and it is young enough that a name or a
default may still move. A `1.0.0` is a promise about stability that is easier
to make later than to walk back.

### The call

- `CallServer` answers the socket StandIn dials: the signed handshake and its
  single-use replay guard, capacity and draining, the wire protocol, outbound
  sequence numbers and the audio timeline, five bounds on a call nobody
  closed, and idempotent teardown.
- A handler implements up to seven callbacks and inherits from nothing. A
  missing one is a no-op, and an exception from any of them ends that call
  alone.
- `cancel_playback()` is the only lever that un-sends audio the service
  already holds, which is what makes a barge-in actually stop the bot.

### Speech

- `VoiceLane` runs segmentation, transcription, the agent and paced playback
  as one turn, so an agent that only reads and writes text can hold a call.
- `UtteranceSegmenter`, `PacedPlayback`, WAV decoding, resampling and a frame
  aligner, for a plugin that wants the pieces rather than the assembly.
- `StartupBuffer` and an echo guard for the speech-to-speech path, which has
  its own turn-taking and its own two ways to go wrong.

### Seeing, and being seen

- The caller's camera and screen share, a vision budget, a recording-gated
  keyframe history, and ambient vision that is off until a plugin turns it on.
- Pictures, documents and web pages on the bot's tile, a slideshow, and the
  model's choice of fullscreen or overlay.
- Emotion cues and a viseme timeline estimated from the text and the audio
  actually sent, covering Latin and Arabic, plus your own video on the tile.

### Around the call

- Call tools declared once and rendered into each provider's JSON, consulting
  and durable background work, and a meeting recap with a Word document.
- A chat lane dialled out from your worker, with attachments in and pictures
  out, so nothing listens and no Bot Framework credential lives in your agent.
- Outbound calling that speaks into a call already up before it rings anybody
  a second time.
- An install check that rings your own handler on loopback and reports what
  worked, with no provider bill, no tunnel and no Microsoft tenant.

### Plugins

- ElevenLabs, Deepgram, Cartesia and an echo in both languages. LiveKit in
  both. Hermes Agent in Python. OpenAI Realtime and OpenClaw in TypeScript.

<!--
Each release adds a section here, newest first:

## 0.2.0 - 2026-01-01

### Added
### Changed
### Deprecated
### Removed
### Fixed
### Security
-->
