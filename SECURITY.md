# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Use GitHub's **"Report a vulnerability"**
button under this repository's **Security** tab rather than opening a public issue.

Include a clear description, reproduction steps, and the affected component. We aim to acknowledge
reports within a few business days and will keep you informed through remediation and coordinated
disclosure.

## Scope

This repository holds the StandIn SDKs, their plugins, and the wire protocol they speak. In
scope:

- the Python and TypeScript SDKs under `libraries/`
- the plugins shipped inside them
- the wire protocol and conformance vectors under `protocol/`
- the examples under `examples/`

Reports about the hosted service at [standin.komaa.com](https://standin.komaa.com), including
anything you reach over the call or chat lanes, are also welcome through the same private channel.

## What the SDK defends, and what it does not

The SDK authenticates the **connection**, not the person on the call.

- Every socket is authenticated with HMAC-SHA256 over your connection secret, with a replay window
  and a single-use handshake guard. Missing or malformed inputs fail closed.
- A `session.start` naming a different call than the one the signature covered is refused.
- Deciding **which callers may be answered** is your deployment's policy, which is why it lives in
  a plugin and not in the SDK. An allowlist that admits an absent caller id admits every
  anonymous caller.

Terminate TLS in front of your worker. The signature proves origin and freshness; it does not
encrypt anything.

## Handling your connection secret

Keep it in the environment or your platform's secret store. `STANDIN_SECRET` is the only place the
SDK reads it from by default. Never commit it, never log it, and never put it in a URL or query
string. Rotate it from the StandIn portal; live calls finish on the old value.

If a configuration layer can hand you an unresolved reference, coerce a non-string to the empty
string rather than stringifying it. `String({})` yields a non-empty, guessable value that a naive
presence check would accept.
