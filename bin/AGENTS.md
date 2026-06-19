# Bridge Runtime

## Purpose

Runtime code and launchers for local delegation, peer networking, discovery, pairing, authentication, and recovery.

## Local Contracts

- Keep the 11 v1.2.7 core MCP tools backward compatible; new network-management tools are optional extensions.
- Legacy HTTP peer mode remains on port `18084`; secure automatically paired peers default to HTTPS port `18443`.
- Production discovery is opt-in for upgrades and uses `_hermes-bridge._tcp.local.` only when explicitly enabled.
- A new device identity always requires user approval. Known pinned identities may reconnect or rekey automatically.
- Runtime state writes must be atomic and safe across the local and peer bridge processes.

## Work Guidance

- Separate static `peers.json` compatibility data from managed pairing state.
- Bind credentials to persistent identity fingerprints and verify pinned certificates before MCP calls.
- Keep discovery metadata non-secret and strictly validate all untrusted network input.

## Verification

- Runtime tests must use loopback, dynamic non-production ports, generated identities, and an in-memory discovery backend.

## Child DOX Index

- No child DOX scopes.

