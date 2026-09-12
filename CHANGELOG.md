# Changelog

Both packages share one version and ship together, so one file covers both.
`standin-sdk` on PyPI and `@komaa/standin-sdk` on npm always carry the same
number, and `make release-check` refuses a release where they do not.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Turn-taking as one object: `VoiceLane` runs segmentation, transcription, the
  agent and paced playback in order, so an agent that only reads and writes
  text can hold a call.
- The avatar surface: emotion cues, a viseme timeline estimated from the text
  and the audio actually sent, for Latin and Arabic, and your own video on the
  bot's tile.
- Group calls: a wake-phrase gate with a follow-up window, and verbal
  interrupts that stop playback in code rather than waiting for the model.
- Meeting recap: section parsing, an attributed transcript in the document, and
  a delivery target resolved once and pinned.
- Reaching people: speak into a call that is already up, or ring back and park
  the line until they answer.
- An install check: `run_smoke` rings this worker's own handler on loopback and
  reports what worked, with no provider bill, no tunnel and no Microsoft
  tenant.
- Chat attachments in, pictures out, and `MEDIA:` markers taken out of a reply
  before anybody reads one aloud.
- A web page on the tile through a renderer you supply, with the SDK's own
  public-address guard in front of it.

### Changed

- The short-utterance floor now measures the voiced part alone. It counted the
  pre-roll and the trailing silence, which together are over a second, so the
  floor could never fire and every click reached the transcriber.
- The documentation site gained twelve pages and a check that fails the build
  on a dead link, an unresolvable import, a public name with no prose, or a
  sentence that reveals how the service is built.

### Fixed

- A superseded turn no longer speaks its own failure over the turn that
  replaced it.
- A line handed to the voice lane after teardown is refused rather than
  synthesized onto a call that has ended.
- Parking an outbound message no longer raises out of a delivery that promises
  not to raise. A failed park is reported as what it is: the call rang, and
  nobody will hear the line.

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
