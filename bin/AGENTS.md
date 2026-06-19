# Bridge Runtime

## Purpose

Runtime code and launchers for local delegation, peer networking, discovery, pairing, authentication, and recovery.

## Local Contracts

- Keep the 11 v1.2.7 core MCP tools backward compatible; new network-management tools are optional extensions.
- Legacy HTTP peer mode remains on port `18084`; secure automatically paired peers default to HTTPS port `18443`.
- Production discovery is opt-in for upgrades and uses `_hermes-bridge._tcp.local.` only when explicitly enabled.
- A new device identity always requires user approval. Known pinned identities may reconnect or rekey automatically.
- Runtime state writes must be atomic and safe across the local and peer bridge processes.
- `hermes_bridge_network.py` owns device identity, managed peer state, durable revocations, mDNS, pairing HTTP routes, pinned TLS, signed introductions, and recovery.
- Managed secrets live only in restricted `bridge-state/network` files; public MCP status must expose booleans and fingerprints, never credentials.
- Pairing and rekey routes accept only bounded requests from loopback, private, or link-local source addresses.

## Work Guidance

- Separate static `peers.json` compatibility data from managed pairing state.
- Bind credentials to persistent identity fingerprints and verify pinned certificates before MCP calls.
- Keep discovery metadata non-secret and strictly validate all untrusted network input.
- Keep production mDNS and secure HTTPS startup behind `HERMES_BRIDGE_AUTO_DISCOVERY=1`; legacy static peers remain authoritative on ID collisions.

## Verification

- Runtime tests must use loopback, dynamic non-production ports, generated identities, and an in-memory discovery backend.

## Child DOX Index

- No child DOX scopes.
