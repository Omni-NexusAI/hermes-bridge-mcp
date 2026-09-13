# Bridge Runtime

## Purpose

Runtime code and launchers for local delegation, peer networking, discovery, pairing, authentication, and recovery.

## Local Contracts

- Keep the 11 v1.2.7 core MCP tools backward compatible; new network-management tools are optional extensions.
- Legacy HTTP peer mode remains on port `18084`; secure automatically paired peers default to HTTPS port `18443`.
- Local MCP ports `18082`/`18083` use the native stateless Streamable HTTP server with managed bearer authentication; `supergateway` is not a runtime dependency.
- Production mDNS discovery is enabled unless `AGENT_BRIDGE_AUTO_DISCOVERY=0`
  (legacy `HERMES_BRIDGE_*` remains accepted). Advertise and browse both
  `_agent-bridge._tcp.local.` and `_hermes-bridge._tcp.local.`, deduplicated
  by peer identity and certificate fingerprint.
- Canonical product/runtime names use Agent Bridge MCP, `agent-bridge`,
  `AGENT_BRIDGE_*`, and `mcp-agent-bridge`; canonical environment values take
  precedence over legacy aliases.
- New installs use Agent Bridge state paths. Existing Hermes-era state is
  retained in place when detected, with no automatic identity migration.
- Universal routing is host-configured through `universal_agent_v1`. Remote
  callers may select only enabled agent IDs and may not supply executables,
  credentials, sandbox escalation, or unrestricted execution policy.
- Peer universal tools require managed, authenticated, certificate-pinned
  peers. Capabilities must never be exposed in public discovery metadata.
- A new device identity always requires user approval. Known pinned identities may reconnect or rekey automatically.
- Runtime state writes must be atomic and safe across the local and peer bridge processes.
- `agent_bridge_storage.py` owns transactional task/session stores and OS locks.
  Preserve complete replies; bound public previews separately. Positively dead
  owners require reconciliation; never silently rerun an uncertain delivery.
- `agent_bridge_identity.py` owns persistent identity creation and signing.
  Missing identity components, mismatched keys, and corrupt trust/revocation state
  fail closed without replacing user identity or forgetting trust.
- `agent_bridge_codex.py` owns explicit owner RPC and host-owned project mapping;
  `agent_bridge_conversations.py` owns optional tools and queued job orchestration.
  Caller paths, executables, credentials, models and permission overrides are forbidden.
  Reuse caller/agent/project/topic associations and actual conversation context.
  Queue ordinary contributions; guarded steering must not wait behind polling locks.
  Validate project association before delivery and retrieve the acknowledged turn.
- Routed queue inputs are private bridge state. Startup and result polling may
  resume known unsent queues with the same request ID; uncertain or acknowledged
  deliveries only reconcile. Cancellation preserves already accepted owner work.
- Conversation extensions accept local callers or authenticated managed peers;
  legacy unauthenticated/shared-token LAN access does not grant owner access.
  Apply the same gate when core status, result, or cancel tools address routed tasks.
- Installers activate content-addressed payloads and their separate Python environments
  atomically. Rollback retains both previous source and previous dependencies.
  Windows launchers verify executable/script ownership and matching readiness,
  process ID and build revision. Restart only after checkpointing affected tasks;
  watchdog aliases share one owner lock and never kill an unhealthy process implicitly.
- Windows venv PID records identify the launcher; readiness identifies the actual
  listener within its verified process tree. Redirector children must match the
  venv's declared base interpreter, exact bridge script, ancestry and creation time.
  Explicit restart stops the verified interpreter before its launcher, refuses
  active or unknown descendants and reused process IDs, and leaves direct system
  console-host cleanup to Windows. Inspection failures must prevent new launches.
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
