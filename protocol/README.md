# Call protocol

`schema.yaml` is the wire contract between StandIn and your worker: one
WebSocket per active Microsoft Teams call, JSON text frames, camelCase keys,
discriminated on `type`. `schema.sha256` identifies the exact bytes both SDK
protocol modules were generated from, which is how a stale binding is caught
rather than shipped.

The generator writes the Python and TypeScript `protocol` modules. **Never
hand-edit those.** Change `schema.yaml`, regenerate, and let the drift check
confirm the two stay in step.

```bash
make protocol-generate   # regenerate both SDK modules from schema.yaml
make protocol-check      # verify the digest and both generated modules
make check               # everything, including the above
```

The generated modules own the call-context models, parser field mappings,
message discriminators, the audio sample rate, and the outbound wire builders.
The small runtime modules beside them own JSON and base64 validation, identity
normalisation, and context prose.

## Compatibility policy

`generate.py` encodes deliberate leniency, and it is more permissive than a
strict schema validator on purpose:

- an absent meeting thread becomes an empty string
- missing or malformed caller identity becomes empty identity
- absent or unknown call direction becomes `inbound`

Evolution is additive. Unknown fields are ignored and unknown message types are
safe to ignore, so an older worker and a newer service interoperate. That is why
a receiver must never reject a message type it does not recognise.

Defining a message here does not by itself give the SDK a handler for it. The
avatar messages are the clearest example: they belong to the wire contract, and
the SDK ignores them by design, because the avatar tile is StandIn's to render.

## Conformance vectors

`conformance.json` supplies shared wire, audio, chat and authentication cases
that **both** SDKs run, so "the Python and TypeScript SDKs agree" is something CI
proves rather than something a README claims.

Add a regression case there whenever you change observable behaviour. The right
order is: write the vector first, watch one language fail, then fix that
language.

## Authentication

Two lanes, neither a replacement for the other, plus a second version of the
body signature:

| Lane | Signs | Window |
|---|---|---|
| Handshake | `"{timestampMs}.{id}"`, the channel name or the call id | 60 s |
| Body | the exact transmitted bytes | 300 s |
| Request (v2) | method, path and a hash of the body | 300 s |

v2 exists because v1 signs a single value, leaving everything else in the
request unsigned. Binding the method and path means a validly signed body cannot
be replayed against a different endpoint.

The SDK security pages at [docs.komaa.com](https://docs.komaa.com) carry the
full contract and the replay guard.
