# Bridge Runtime

## Purpose

Runtime code and launchers for local delegation, peer networking, discovery, pairing, authentication, and recovery.

## Local Contracts

- Keep the 11 v1.2.7 core MCP tools backward compatible; new network-management tools are optional extensions.
- Legacy HTTP peer mode remains on port `18084`; secure automatically paired peers default to HTTPS port `18443`.
- Local MCP ports `18082`/`18083` use the native stateless Streamable HTTP server with managed bearer authentication; `supergateway` is not a runtime dependency.
- Production mDNS discovery is enabled unless `HERMES_BRIDGE_AUTO_DISCOVERY=0`; the mDNS backend uses `_hermes-bridge._tcp.local.`. Tailscale remains opt-in and probes only explicitly tagged tailnet nodes.
- A new device identity always requires user approval. Known pinned identities may reconnect or rekey automatically.
- Runtime state writes must be atomic and safe across the local and peer bridge processes.
- `hermes_bridge_network.py` owns device identity, managed peer state, durable revocations, mDNS, pairing HTTP routes, pinned TLS, signed introductions, and recovery.
- Managed secrets live only in restricted `bridge-state/network` files; public MCP status must expose booleans and fingerprints, never credentials.
- Pairing and rekey routes accept only bounded requests from loopback, private, link-local, or official Tailscale source ranges; Tailscale ranges require the Tailscale backend.
- Managed unpair uses signed, fingerprint-bound prepare/commit transactions. Forced local forget must report that remote cleanup remains outstanding and remove matching managed, legacy, candidate, approval, and session artifacts without deleting task history.

## Work Guidance

- Separate static `peers.json` compatibility data from managed pairing state.
- Bind credentials to persistent identity fingerprints and verify pinned certificates before MCP calls.
- Keep discovery metadata non-secret and strictly validate all untrusted network input.
- Start production mDNS and secure HTTPS unless `HERMES_BRIDGE_AUTO_DISCOVERY=0`; keep legacy static peers authoritative until an explicitly approved managed pairing migrates the same ID, and keep their listener independent from secure discovery failures.
- Keep Tailscale CLI execution and API responses injectable, report only sanitized health, and never expose tailnet names, node names, IPs, tags, API tokens, or raw Tailscale output through MCP.
- Tailscale API inventory may refresh candidate endpoints for pinned peers, but automatic tailnet enrollment, auth-key creation, and device authorization are outside this runtime contract.

## Verification

- Runtime tests must use loopback, dynamic non-production ports, generated identities, and an in-memory discovery backend.

## Child DOX Index

- No child DOX scopes.
