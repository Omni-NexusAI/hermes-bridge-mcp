# Codex integration: unfinished and disabled

Existing Codex Desktop conversation attachment is **unfinished**, not a supported
bridge feature. Use native Codex Desktop connections for Codex-to-Codex work.
The separate CLI adapter produced genuine Codex replies and retained follow-up
context, but those bridge-managed sessions never proved Desktop attachment.

Both integrations are disabled by default. This does not remove Codex as an MCP
client: keep its Agent Bridge server entry to communicate with Hermes or other
enabled frameworks. The eleven core and four universal tool schemas are unchanged.
Lossless result retrieval stays available and retained histories are not deleted.

## Retire an existing installation

- Disable the receiving `codex` adapter in host-owned `agents.json` using
  `enabled: false` and `replace_builtin: true`; retain the manifest's adapter
  kind and command. Disable any separately named Codex test adapters too.
- In Codex's bridge MCP entry, add the eight `bridge_agent_` / `bridge_peer_`
  tools ending in `projects_list`, `conversations_list`, `conversation_read`,
  or `routed_delegate_start` to `disabled_tools`. Preserve other existing filters.
  Keep the server, universal tools, and `complete_result` tools enabled.
- Preserve owner mappings and task history as recovery data. Do not edit Codex
  session databases, remove Codex itself, or change bridge pairings/credentials.
- Existing clients may need an MCP reconnect to refresh tool visibility. Let
  active work finish before any required reload.

For an installation without an `agents.json`, the following host-owned manifest
disables only the built-in Codex recipient. If the file already exists, merge the
record into its `agents` array and preserve every other adapter and setting.

```json
{
  "schema_version": 1,
  "agents": [
    {
      "id": "codex",
      "kind": "cli",
      "enabled": false,
      "replace_builtin": true,
      "command": ["codex", "exec", "{prompt}"]
    }
  ]
}
```

Verify retirement against the running bridge: `bridge_agent_universal_list`
must show Codex disabled and the intended remaining frameworks enabled. A
Codex-targeted universal start must return `agent_unavailable` without a task ID.
Check Codex's effective MCP configuration for the eight disabled tools as well;
a server-side tool listing does not reflect the client's filter. This configuration
procedure also works on older payloads; installing this source revision additionally
hides the owner tools from the server's default tool catalogue.

## Retained experimental implementation

The following protocol notes describe unaccepted development code. Source-level
experiments must explicitly opt in with `AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING=1`.
The historical built-in CLI adapter separately requires
`AGENT_BRIDGE_ENABLE_CODEX_ADAPTER=1` (or an explicit host replacement manifest).
These flags do not establish that an existing Desktop owner is attachable.

`conversation_routing_v1` is an optional extension. The eleven core tools and four
`universal_agent_v1` tools preserve their exact schemas and established adapter paths.
The retained Codex adapter remains separate from owner routing.

Configure `bridge-state/codex-owner.json` on each host, or set
`AGENT_BRIDGE_CODEX_OWNER_CONFIG` to a host-owned file. Start from
[`codex-owner.example.json`](../config/codex-owner.example.json). The endpoint must
belong to the existing desktop owner. Never launch another app-server against an
active desktop conversation, edit Codex session databases, or set `desktop_owner`
true merely because an endpoint speaks the protocol.

The [official app-server protocol](https://learn.chatgpt.com/docs/app-server)
defines discovery, reading, starting/resuming conversations, and guarded steering.
It does not establish that every installed desktop version exposes an attachable
owner endpoint or an API to register newly created conversations in Desktop's
project sidebar. This candidate reports `owner_verification: host_operator_assertion`
and `desktop_project_registration: unverified` until separate live acceptance.
Its transport client supports a host-local WebSocket or Unix socket endpoint;
the protocol documentation classifies WebSocket transport as experimental.

The host maps repository identity to its own project ID and local cwd. Different
paths on different computers are expected. Optional `cwd_aliases` cover the host's
existing worktrees. Callers may select an ID or repository URL, but cannot supply
local paths, endpoints, tokens, model overrides, or permissions. Projectless work
uses the host's `projectless_cwd` and retains topic associations too.

1. Inspect `bridge_agent_status.public_tool_contract` and the paired peer's
   advertised extension before calling optional routing tools.
2. List projects and conversations; read selected context if needed. Routing reads
   actual recent context, rather than selecting by title alone. Ambiguous matches
   return candidate IDs and create nothing.
3. Call `bridge_peer_routed_delegate_start` with the peer, a stable `request_id`,
   `topic`, prompt, and optional project identity. Supply selected objectives,
   decisions, constraints, progress, requested contribution, or a subsequent `delta`
   in `context`. Whole histories and working directories are not copied automatically.
4. Select `queue` for ordinary work, `steer` when the contribution must affect an
   active turn, or `separate` with `new_reason` for independent work. Unsupported
   steering falls back to an explicit queue. The result records the selected
   conversation, reused/created status, chosen delivery mode and reason.
5. Poll the returned task ID through the existing status tools or `*_complete_result`.
   Follow `next_offset` to retrieve the full reply. A completed turn without an agent
   reply is `result_unavailable`, not verified success.

Use the same request ID and identical input for retries. Use a new request ID for
a follow-up while retaining its project and topic association. Lost acknowledgement
after a create or send becomes `delivery_uncertain`: inspect the owner before any
new delivery. An accepted turn is reconciled by its exact ID and is never resent.
Approval requests remain with the owning application; this bridge never approves
on the user's behalf or changes model, sandbox, or approval defaults.

Both local and peer processes share transactions and delivery locks. Polling and
busy waits do not hold the delivery lock, so a later explicit steer can reach the
observed active turn. Each selected conversation's project is checked again before
delivery. Stored completed replies remain readable if the adapter is unavailable.

Automated tests use synthetic owner RPC and temporary state. Bilateral live
acceptance additionally requires actual desktop-owner discovery and replies on
both updated machines, existing and newly created conversation cases, correct
project association, and a follow-up whose answer depends on retained context.
