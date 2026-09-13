# DOX Framework

## Core Contract

- `AGENTS.md` files are binding work contracts for their subtrees.
- Read the full applicable chain before editing and update it after meaningful contract or structure changes.
- Preserve the stable v1.2.7 `bridge_agent_*` and `bridge_peer_*` core tool contract unless a breaking version is explicitly declared.
- Agent Bridge MCP v1.3.5 adds four `universal_agent_v1` extension tools;
  keep them separate from the exact 11-tool compatibility contract.
- `conversation_routing_v1` adds optional host-mapped owner conversation tools
  and lossless result retrieval; preserve all fifteen earlier schemas exactly.
- Pairing MCP Prompts are optional discovery conveniences; pairing tools remain
  authoritative and compatibility commands remain available for clients that
  do not expose MCP Prompts.

## Isolation Contract

- Repository development and automated tests must not read or write a real Hermes home, agent configuration, startup folder, container, paired-device state, Tailscale daemon state, or Tailscale API/tailnet state.
- Tests must use temporary `HOME`, `LOCALAPPDATA`, bridge state, identity, certificate, and log directories.
- Tests must not use ports `18082`, `18083`, `18084`, or `18443`, invoke a real Hermes executable, or broadcast the production mDNS service.
- Live installation, restart, container access, and device validation require separate explicit user authorization.

## Work Guidance

- Base feature work on `development` and keep legacy static peer configuration functional.
- Treat automatically discovered identities as untrusted until explicitly approved.
- Treat Tailscale as a reachability layer, never as proof that a Hermes identity is trusted.
- Never expose private keys, bearer credentials, or unredacted pairing records through tools or logs.
- Use native `bridge_peer_*` tools as the normal remote-agent path; raw HTTP helpers remain diagnostics.
- Keep portable installation state-preserving: bridge payloads may update atomically, but installers must not alter bridge-state, identities, pairings, legacy peers, agent configuration, or live services without explicit user authorization.

## Verification

- Run tests only with the repository's sandbox guard enabled.
- Confirm the original 11 core tool names and schemas remain stable.
- Verify host-owned CLI, MCP mapping, native MCP, and Codex session variants
  without invoking a real installed agent.
- Run `python -m pytest -q` from an isolated environment when dependencies are available.

## Rollout Boundaries

- Automated tests remain isolated. Live rollout needs explicit target authorization;
  an authorization to update identified hosts does not extend to other paired devices.
- Preserve disabled integrations and established repository checkouts. Do not copy
  repositories between hosts or publish implementation changes without authorization.
- A bridge task completion status is insufficient acceptance: retrieve actual replies,
  demonstrate retained context and project association, and record running versions.
- Existing Codex Desktop attachment requires its owning server. A new app-server or
  a host-configured owner assertion alone is not evidence of Desktop attachment.

## Child DOX Index

- `bin/AGENTS.md` — bridge runtime, discovery, authentication, and launcher contracts.
- `tests/AGENTS.md` — isolated test-environment and safety requirements.
