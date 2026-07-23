# Tests

## Purpose

Automated verification for the bridge's compatibility, security, discovery, pairing, and recovery behavior.

## Local Contracts

- Tests must fail closed if a real Hermes path, production port, production discovery namespace, or non-loopback network interface is selected.
- Never invoke a real Hermes or Tailscale binary or contact an existing bridge, agent, container, tailnet, Tailscale API, or device.
- Use temporary roots, generated credentials, stub delegate runners, and injectable discovery.
- Production mDNS broadcast is forbidden in automated tests.
- Tailscale status, API inventory, peer inventory, and identity probes must be injected mocks; a test must fail closed before invoking the real CLI or HTTP API.
- The integration harness may start only temporary loopback HTTPS bridge processes with dynamic ports and `HERMES_BRIDGE_STUB_DELEGATE=1`.

## Verification

- Assert the 11 stable core tool names and schemas separately from optional extension tools.
- Cover the four universal extension tools separately, including native MCP,
  declarative MCP and CLI adapters, unknown/disabled agents, malformed
  manifests, injection resistance, secret redaction, cancellation, timeouts,
  Codex `threadId`/`sessionId`, and caller-scoped conversation persistence.
- Verify canonical environment precedence, compatibility-aware state paths,
  dual discovery registration, and legacy-peer rejection for universal calls.
- Cover approval, rejection, replay, expiry, certificate mismatch, revocation, restart, endpoint change, and identity-preserving rekey.
- Cover coordinated unpair, dry-run, forced local forget, fingerprint mismatch, retry/idempotency, and cleanup of stale pairing artifacts.
- Exercise signed introductions and confirm introduced unknown identities remain candidates until approval.
- Cover tagged Tailscale filtering, official IPv4/IPv6 ranges, malformed status/API output, unavailable CLI, unavailable/unauthorized API, unreachable peers, and sanitized discovery health.

## Child DOX Index

- No child DOX scopes.
