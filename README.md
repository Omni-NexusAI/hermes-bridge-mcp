# Agent Bridge MCP

Current bridge version: `v1.3.5` (release candidate).

Agent Bridge MCP is a universal, secure MCP delegation bridge. An MCP-compatible
caller can select Hermes, Codex, A0, or another host-enabled adapter locally or
on a paired device. `peer_id` selects the device and `agent` selects the agent
architecture on that device.

The canonical MCP identifier is `agent-bridge`, the environment prefix is
`AGENT_BRIDGE_*`, and delegated Hermes work uses the source label
`mcp-agent-bridge`. The former Hermes Bridge names, `HERMES_BRIDGE_*`,
`hermes-bridge`, and `windows-hermes` remain compatibility aliases.

This bridge is not a messaging gateway proxy. It does not expose Telegram,
Discord, Slack, WhatsApp, or `messages_send`/`conversations_list` tools.
Messaging gateway launchers and watchdogs are intentionally not shipped by this
bridge anymore; install Hermes messaging integrations separately if needed.

## What It Provides

- `bridge_agent_status`
- `bridge_agent_delegate`
- `bridge_agent_delegate_start`
- `bridge_agent_delegate_status`
- `bridge_agent_delegate_result`
- `bridge_agent_delegate_cancel`
- `bridge_peer_*` tools for authenticated Hermes-to-Hermes delegation
- `bridge_network_status` for discovered, paired, revoked, and recovering peers
- `bridge_peer_pair` for explicit approval, rejection, revocation, and reconnection
- `bridge_peer_unpair` for fingerprint-checked, coordinated removal or forced local forget
- `bridge_agent_universal_list`
- `bridge_agent_universal_delegate_start`
- `bridge_peer_universal_list`
- `bridge_peer_universal_delegate_start`

Legacy `windows_agent_*` aliases are hidden by default. Set
`AGENT_BRIDGE_ENABLE_LEGACY_WINDOWS_TOOLS=1` only for older bridge clients that
still call those names.

## Which Tool Family To Use

The original 11 `bridge_agent_*` and `bridge_peer_*` tools retain their exact
schemas and Hermes-compatible behavior.

Use `bridge_agent_universal_list` to inspect sanitized capabilities of locally
enabled adapters. Use `bridge_agent_universal_delegate_start(agent, ...)` to
start a bridge-managed task on one of those adapters. Poll or cancel the
returned task ID with the existing `bridge_agent_delegate_status`,
`bridge_agent_delegate_result`, and `bridge_agent_delegate_cancel` tools.

Use `bridge_peer_universal_list(peer_id)` and
`bridge_peer_universal_delegate_start(peer_id, agent, ...)` for another device.
These tools require an authenticated, certificate-pinned managed pairing.
Agent capabilities are returned only after authentication and are never
included in public discovery advertisements.

`bridge_agent_delegate` runs the task through the local Hermes Bridge agent:

```text
hermes chat --query ... --quiet --source mcp-agent-bridge --accept-hooks
```

The delegate tool now resumes a persistent Hermes session per A0 thread key
when `a0_thread_key` is provided. Long-running work should use
`bridge_agent_delegate_start`, then poll with `bridge_agent_delegate_status`
or `bridge_agent_delegate_result`.

Adaptive timeout behavior keeps long delegated tasks alive after the initiating
MCP call returns. For `bridge_agent_delegate`,
`timeout_seconds` is the inline wait before a pollable `task_id` is returned;
the background task has a separate longer `hard_timeout_seconds`. Set
`kill_on_timeout=true` only when the caller explicitly wants the old destructive
timeout behavior.

It does not expose a raw PowerShell or CMD proxy.

## Universal Agent Adapters

`universal_agent_v1` supports three host-owned adapter mechanisms:

- A native MCP implementation exposing capability, start, status, result, and
  cancellation tools.
- A declarative MCP stdio or HTTP tool/field mapping.
- A declarative argument-array CLI adapter.

Hermes and Codex manifests are auto-detected. Codex supports current
`codex mcp-server`/`threadId` and legacy `codex mcp`/`sessionId` variants.
Codex work creates or continues bridge-managed Codex tasks; v1.3.5 does not
attach to arbitrary already-open Codex Desktop tasks.

Additional adapters are configured by the host in `agents.json`; see
[`config/agents.example.json`](config/agents.example.json). Remote requests can
select an enabled `agent`, but cannot supply executables, credentials, model
overrides, sandbox escalation, or unrestricted execution settings. Persistent
sessions are keyed by caller peer, agent identifier, and conversation key.
Unknown or disabled agents return `agent_unavailable`; older peers without the
extension return `extension_unsupported`.

## Secure Peer Bridge

Peer mode runs the same MCP server directly over Streamable HTTP and does not
require `supergateway`. The legacy shared-key listener remains HTTP on `18084`:

```powershell
$env:AGENT_BRIDGE_PAIR_KEY = "same-secret-on-each-paired-agent"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\agent-bridge\bin\start-agent-bridge-peer.ps1
```

Android/Termux:

```sh
python -m pip install -r requirements-android.txt
cp config/agent-peer.env.example config/agent-peer.env
sh bin/start-agent-bridge-peer.sh
```

Peer endpoints default to `http://<LAN-IP>:18084/mcp`. Keep `18082` and `18083`
for the existing A0 live/staging bridge.

Automatic pairing is opt-in for upgrades. When enabled, the peer launcher also
starts a certificate-pinned HTTPS listener on `18443` and advertises it with
mDNS:

```powershell
$env:AGENT_BRIDGE_AUTO_DISCOVERY = "1"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\agent-bridge\bin\start-agent-bridge-peer.ps1
```

Call `bridge_network_status` to inspect candidates. After comparing the complete
fingerprint shown by the tool, approve once from either agent:

To remove a stale secure relationship from both machines, call
`bridge_peer_unpair` with the peer ID and its pinned fingerprint. The operation
uses prepare/commit phases and is safe to retry. Use `scope="local"` only when
the remote machine is permanently unavailable; the result explicitly reports
that remote cleanup is still required. `dry_run=true` previews every local
record that would be removed.

```text
bridge_peer_pair(action="approve", peer_id="candidate-id", expected_fingerprint="full-sha256-fingerprint")
```

For peers on different networks, use the opt-in Tailscale backend instead of
mDNS. It reads Tailscale inventory from the local CLI or, on passive/mobile
environments, the Tailscale HTTP API. The backend probes only tagged nodes and
presents validated bridge identities as the same untrusted candidates:

```powershell
$env:AGENT_BRIDGE_AUTO_DISCOVERY = "1"
$env:AGENT_BRIDGE_DISCOVERY_BACKEND = "tailscale"
$env:AGENT_BRIDGE_TAILSCALE_PROVIDER = "auto"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\agent-bridge\bin\start-agent-bridge-peer.ps1
```

For Android or other environments without a local Tailscale CLI, set
`AGENT_BRIDGE_TAILSCALE_PROVIDER=api`, `AGENT_BRIDGE_TAILSCALE_API_TOKEN`,
and `AGENT_BRIDGE_TAILNET`. If the bridge cannot infer its own Tailscale
address from API inventory, set `AGENT_BRIDGE_ADVERTISE_ADDRESS` to its
Tailscale IP or MagicDNS name.

Tailnet membership never approves a Hermes identity. Verify the fingerprint
and use `bridge_peer_pair` exactly as with mDNS. See
[`docs/tailscale-peer-discovery.md`](docs/tailscale-peer-discovery.md) for tag,
access-policy, platform, and failure-mode setup.

The bridge advertises and browses both `_agent-bridge._tcp.local.` and the
legacy `_hermes-bridge._tcp.local.` service, deduplicated by peer ID and
certificate fingerprint.

Approval exchanges device-bound, per-peer bearer credentials over pinned HTTPS.
Unknown identities cannot call MCP tools. Trusted identities can recover from an
IP change or rotate credentials with `action="reconnect"`; a changed identity
requires approval again.

Static peer config lives at
`$AGENT_BRIDGE_PEERS_CONFIG` or `~/.agent-bridge/bridge-state/peers.json`
(an existing Hermes-era path remains valid):

```json
{
  "peers": [
    {
      "peer_id": "quest3",
      "url": "http://QUEST_LAN_IP:18084/mcp",
      "platform": "android",
      "pair_key_env": "AGENT_BRIDGE_PAIR_KEY"
    }
  ]
}
```

Peer tools:

- `bridge_peer_status(peer_id)`
- `bridge_peer_delegate_start(peer_id, prompt, cwd?, timeout_seconds?, max_turns?, conversation_key?, hard_timeout_seconds?)`
- `bridge_peer_delegate_status(peer_id, task_id)`
- `bridge_peer_delegate_result(peer_id, task_id)`
- `bridge_peer_delegate_cancel(peer_id, task_id)`

The peer tools internally call the remote peer's `bridge_agent_*` tools over
Streamable HTTP using the URL and bearer token from `peers.json`. Agents should
use the peer tools rather than hand-writing JSON-RPC unless diagnosing a broken
MCP client session.

LAN-facing peer bridge startup requires `AGENT_BRIDGE_PAIR_KEY` unless
explicitly run with the unsafe development override. Both paired agents should
use the same pair key for the simplest setup. For multi-peer setups, each peer
entry can use a distinct `pair_key_env`. Legacy `HERMES_BRIDGE_AUTH_TOKEN`,
`token`, and `token_env` values are still accepted for existing installs.

Static shared-key peers remain supported. Automatically paired peers use a
persistent P-256 device identity, certificate pinning, directional credentials,
signed pairing receipts, replay protection, and signed trusted-peer
introductions. Introduced unknown identities remain candidates until approved.

## Requirements

- A local Hermes install available to the bridge runtime
- Docker Hermes configured with access to `host.docker.internal`
- Python dependencies pinned in `requirements-bridge.txt`; Node.js and
  `supergateway` are not required.

## Install

From this repository on any supported platform, install the complete bridge
payload without altering existing pairing or agent configuration:

```text
python bootstrap.py install --start
python bootstrap.py doctor --json
```

New installations use the Agent Bridge home (`%LOCALAPPDATA%\agent-bridge` on
Windows or `~/.agent-bridge`). Existing installations continue using a detected
Hermes-era state directory so identities and pairings remain intact; state is
not migrated automatically. Explicit `AGENT_BRIDGE_HOME`,
`AGENT_BRIDGE_STATE_DIR`, or `AGENT_BRIDGE_STATE_FILE` values override detection.
Canonical variables take precedence over their `HERMES_BRIDGE_*` aliases.

The bootstrap preserves `bridge-state` and `peers.json`, and supports `rollback`. It installs bridge
dependencies in its own virtual environment but intentionally does not install
or update Hermes itself. Use `--no-deps` for an offline staging pass.

From this repository on Windows, the legacy convenience wrapper remains:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer copies scripts into the compatibility-aware bridge home, creates hidden
Startup launchers, installs the network dependencies into Hermes' virtual
environment, and can restart the bridge. Automatic discovery remains disabled
until `AGENT_BRIDGE_AUTO_DISCOVERY=1` is explicitly set.

Preview paths, ports, discovery settings, and isolation without starting or
writing runtime state:

```powershell
python scripts/validate-network-runtime.py
```

If the A0 `settings.json` file is available from Windows, the installer can add
or update the `agent-bridge` MCP entry:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -A0SettingsPath C:\path\to\settings.json
```

## Docker Hermes Config

In Docker Hermes `config.yaml`:

```yaml
mcp_servers:
  agent-bridge:
    url: http://host.docker.internal:18082/mcp
    headers:
      Authorization: Bearer <contents-of-%LOCALAPPDATA%\hermes\bridge-state\local-mcp-token>
    timeout: 120
    connect_timeout: 30
```

Do not add `transport: sse` for this bridge. It uses native stateless
Streamable HTTP with JSON responses at `/mcp`.
The Windows launcher creates the local bearer token once and preserves it across
upgrades. The A0 configuration helper reads that token automatically when run
on Windows and never prints it in its result.

## Verify

From the Docker Hermes container:

```bash
hermes mcp test agent-bridge
```

Expected result includes direct delegation tools such as:

```text
bridge_agent_status
bridge_agent_delegate
bridge_agent_delegate_start
bridge_peer_status
```

The backward-compatible core remains exactly these 11 tools:

```text
bridge_agent_status
bridge_agent_delegate
bridge_agent_delegate_start
bridge_agent_delegate_status
bridge_agent_delegate_result
bridge_agent_delegate_cancel
bridge_peer_status
bridge_peer_delegate_start
bridge_peer_delegate_status
bridge_peer_delegate_result
bridge_peer_delegate_cancel
```

v1.3.x adds the optional `automatic_pairing_v1` extension tools
`bridge_network_status`, `bridge_peer_pair`, and `bridge_peer_unpair`. The core list and extension list
are reported separately in `public_tool_contract`.

v1.3.5 adds the four `universal_agent_v1` tools listed above. Status, result,
and cancellation continue through the existing task tools.

`bridge_agent_status` should report `bridge_version` as `v1.3.5` on every
platform. It reports the local Hermes runtime separately as `hermes_version`.
It also reports sanitized peer routing diagnostics without exposing token values.

## Compatibility Policy

Starting with `v1.2.7`, Hermes Bridge MCP versions must remain backward
compatible with the default `bridge_agent_*` and `bridge_peer_*` tool contract
unless a future release explicitly declares a breaking bridge version. New
versions may add optional fields or tools, but they should not remove or rename
the 11 default tools, change their core argument meanings, or require all peers
to update at once for normal local and peer delegation.

`bridge_agent_status` exposes `min_compatible_bridge_version`,
`compatibility_policy`, and `public_tool_contract` so agents can verify whether a
peer is compatible before delegation.

You can also test the proxy directly:

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://host.docker.internal:18082/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("bridge_agent_delegate", {
                "prompt": "Reply exactly HERMES_BRIDGE_OK and do not use tools.",
                "timeout_seconds": 180,
                "max_turns": 10,
            })
            print(result.content[0].text)

asyncio.run(main())
```

## Files

- `bin/agent-bridge-mcp.py` - canonical Agent Bridge MCP entrypoint
- `bin/agent_bridge_universal.py` - host-owned universal adapter registry and execution
- `bin/windows-hermes-proxy-mcp.py` - compatibility implementation filename
- `bin/hermes_bridge_network.py` - device identity, managed pairing, discovery, TLS pinning, and recovery
- `bin/hermes-bridge-mcp-serve.cmd` - universal stdio entrypoint wrapper for supergateway on Windows
- `bin/start-hermes-bridge-peer.ps1` - universal Windows peer launcher wrapper
- `bin/start-hermes-bridge-peer.sh` - universal POSIX/Android peer launcher wrapper
- `bin/start-windows-hermes-bridge*.ps1` - Windows-specific A0 bridge launchers
- `bin/start-android-hermes-peer-bridge.sh` - Android/Termux-specific peer launcher implementation
- `bin/windows-hermes-bridge-background-watchdog.ps1` - Windows-specific bridge watchdog
- `scripts/configure-a0-mcp.py` - backup-first A0 MCP settings helper
- `scripts/validate-network-runtime.py` - no-write runtime and isolation preview
- `config/*.example.*` - peer config and env templates
- `docs/quest-android-setup.md` - Android/Quest setup checklist
- `docs/unpaired-device-validation.md` - opt-in release-candidate validation and rollback
- `startup/*.vbs` - hidden Startup-folder launchers

## Notes

- Existing Docker Hermes sessions may cache MCP tools. Restart Docker Hermes or
  start a new session after installing.
- New installs should use the MCP server name `agent-bridge`. Existing
  `hermes-bridge` and `windows-hermes` endpoint names remain aliases.
- The GitHub repository keeps its existing name for this release candidate; it
  will be renamed only after v1.3.5 is merged and validated live.
- Delegated tasks use the local Hermes agent's normal approval policy.
- `scripts/smoke_peer_bridge.py` is a diagnostic probe for a remote `/mcp`
  endpoint. It is not the normal agent workflow; prefer `bridge_peer_*`.
