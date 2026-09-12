# StandIn documentation

Source for **[docs.komaa.com](https://docs.komaa.com)**: the documentation for
[StandIn](https://standin.komaa.com), the hosted service that puts your AI agent into a Microsoft
Microsoft Teams call, and for the Python and TypeScript SDKs that connect to it.

This folder lives inside the [StandIn monorepo](https://github.com/komaa-com/standin), alongside the
code it documents. Built with [Mintlify](https://mintlify.com).

## Structure

| Path | Contents |
|---|---|
| `docs.json` | Site config and navigation. Three products: Home, Python SDK, TypeScript SDK. |
| `index.mdx`, `quickstart.mdx` | The landing page and the first working call. |
| `concepts/` | Architecture, dialogue modes, what the SDK gives you. |
| `teams/` | The Microsoft side: connection modes, Azure bot, app package, publishing. |
| `expose.mdx`, `troubleshooting.mdx`, `community.mdx` | Reachability, failures, the sandbox tier. |
| `python-sdk/`, `typescript-sdk/` | The two SDK products, mirrored page for page. |
| `legacy/` | The earlier standalone bridges. Still published, still accurate, not extended. |
| `legacy/_hidden/` | Bridges that were never launched. Out of navigation and in `.mintignore`, which is what keeps them off the site. |
| `logo/`, `favicon.svg`, `images/`, `assets/` | Brand and screenshots. |

## Local preview

```bash
npm i -g mint
mint dev          # http://localhost:3000, run where docs.json lives
```

## Scope

This site documents the SDKs and the hosted evaluation flow. Nothing about how the service itself
is built or operated belongs here.
