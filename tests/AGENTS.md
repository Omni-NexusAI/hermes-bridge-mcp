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
- `stable_tool_schemas.json` freezes complete schemas for the eleven core and four
  universal tools from baseline 7101286; compare exact schemas on every change.
- Register tools before schema collection and assert independent expected names
  and counts so an empty snapshot or unregistered server cannot pass vacuously.
- Cover cross-process writes, conversation serialization, uncertain delivery recovery,
  complete reply paging, owner authentication, partial identities and corrupt revocations.
- Owner routing tests inject synthetic RPC clients. Cover differing host project paths,
  context-based reuse, ambiguity, justified new conversations, busy queues, unsupported
  steering, concurrent steering, request retries, and project drift before delivery.
- Use a fresh workspace-local `--basetemp .pytest-tmp/<run>` on Windows to avoid
  inherited temporary-directory ACL failures. Never reuse a live state directory.
- Cover the four universal extension tools separately, including native MCP,
  declarative MCP and CLI adapters, unknown/disabled agents, malformed
  manifests, injection resistance, secret redaction, cancellation, timeouts,
  Codex `threadId`/`sessionId`, and caller-scoped conversation persistence.
- Verify default Codex retirement without probing installed agents, preserve cached
  replies and pending work, and explicitly opt into synthetic legacy Codex tests.
- Verify framework switching and return-to-framework context with isolated adapters;
  unknown or disabled framework IDs must fail without falling back to Hermes.
- Verify canonical environment precedence, compatibility-aware state paths,
  dual discovery registration, and legacy-peer rejection for universal calls.
- Cover approval, rejection, replay, expiry, certificate mismatch, revocation, restart, endpoint change, and identity-preserving rekey.
- Cover coordinated unpair, dry-run, forced local forget, fingerprint mismatch, retry/idempotency, and cleanup of stale pairing artifacts.
- Exercise signed introductions and confirm introduced unknown identities remain candidates until approval.
- Cover tagged Tailscale filtering, official IPv4/IPv6 ranges, malformed status/API output, unavailable CLI, unavailable/unauthorized API, unreachable peers, and sanitized discovery health.

## Child DOX Index

- No child DOX scopes.
