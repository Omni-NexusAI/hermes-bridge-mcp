# DOX Framework

## Core Contract

- `AGENTS.md` files are binding work contracts for their subtrees.
- Read the full applicable chain before editing and update it after meaningful contract or structure changes.
- Preserve the stable v1.2.7 `bridge_agent_*` and `bridge_peer_*` core tool contract unless a breaking version is explicitly declared.

## Isolation Contract

- Repository development and automated tests must not read or write a real Hermes home, agent configuration, startup folder, container, or paired-device state.
- Tests must use temporary `HOME`, `LOCALAPPDATA`, bridge state, identity, certificate, and log directories.
- Tests must not use ports `18082`, `18083`, `18084`, or `18443`, invoke a real Hermes executable, or broadcast the production mDNS service.
- Live installation, restart, container access, and device validation require separate explicit user authorization.

## Work Guidance

- Base feature work on `development` and keep legacy static peer configuration functional.
- Treat automatically discovered identities as untrusted until explicitly approved.
- Never expose private keys, bearer credentials, or unredacted pairing records through tools or logs.
- Use native `bridge_peer_*` tools as the normal remote-agent path; raw HTTP helpers remain diagnostics.

## Verification

- Run tests only with the repository's sandbox guard enabled.
- Confirm the original 11 core tool names and schemas remain stable.
- Run `python -m pytest -q` from an isolated environment when dependencies are available.

## User Preferences

- Do not update or test against any existing agent or currently paired device during automatic-discovery development.
- Deliver an uninstalled release candidate for later testing on unpaired devices.

## Child DOX Index

- `bin/AGENTS.md` — bridge runtime, discovery, authentication, and launcher contracts.
- `tests/AGENTS.md` — isolated test-environment and safety requirements.

