# Hermes Bridge MCP

Current bridge version: `v1.2.7` (stable) · `v1.3.0` (development branch).

Universal MCP bridge for cross-device agent delegation and peer-to-peer mesh
networking. Connect any MCP-compatible agent — Hermes, Agent Zero, Claude Code,
or any other — to another agent across the network.

The bridge exposes direct delegation tools so any agent can delegate work to a
local agent on the same machine, or to agents on remote devices over a trusted
network. Delegated work runs through the local agent's own tools, session
persistence, memory, and approval policy.

It runs as a native Streamable HTTP peer bridge for agent-to-agent
communication across Windows, Linux, macOS, and Android/Termux.

## What It Provides

- `bridge_agent_status`
- `bridge_agent_delegate`
- `bridge_agent_delegate_start`
- `bridge_agent_delegate_status`
- `bridge_agent_delegate_result`
- `bridge_agent_delegate_cancel`
- `bridge_peer_*` tools for authenticated agent-to-agent delegation
- `bridge_network_status` for discovered, paired, revoked, and recovering peers (v1.3.0+)
- `bridge_peer_pair` for explicit approval, rejection, revocation, and reconnection (v1.3.0+)

## Which Tool Family To Use

Use `bridge_agent_*` only for the local agent running on the same bridge
endpoint. Do not use `bridge_agent_*` to reach a different machine, headset,
phone, or LAN device.

Use `bridge_peer_*` for another configured peer on the network.
These tools require `peer_id`; call `bridge_agent_status` first and inspect
`configured_peers`, `peers`, and `tool_routing` if the peer ID is unknown.

## Peer Bridge

Peer mode runs the same MCP server directly over Streamable HTTP and does not
require `supergateway`. The legacy shared-key listener remains HTTP on `18084`:

**Windows:**

```powershell
$env:HERMES_BRIDGE_PAIR_KEY = "same-secret-on-each-paired-agent"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\hermes\bin\start-hermes-bridge-peer.ps1
```

**Linux / macOS / Android (Termux):**

```sh
python -m pip install -r requirements-android.txt
cp config/android-peer.env.example config/android-peer.env
sh bin/start-hermes-bridge-peer.sh
```

Peer endpoints default to `http://<LAN-IP>:18084/mcp`.

### Automatic Discovery (v1.3.0+, development branch)

Automatic pairing is opt-in. When enabled, the peer launcher also starts a
certificate-pinned HTTPS listener on `18443` and advertises it with mDNS:

```powershell
$env:HERMES_BRIDGE_AUTO_DISCOVERY = "1"
```

Call `bridge_network_status` to inspect candidates. After comparing the complete
fingerprint, approve once from either agent:

```text
bridge_peer_pair(action="approve", peer_id="candidate-id", expected_fingerprint="full-sha256-fingerprint")
```

For peers on different networks, use the Tailscale backend instead of mDNS:

```powershell
$env:HERMES_BRIDGE_AUTO_DISCOVERY = "1"
$env:HERMES_BRIDGE_DISCOVERY_BACKEND = "tailscale"
```

See [`docs/tailscale-peer-discovery.md`](docs/tailscale-peer-discovery.md) for details.

### Static Peer Config

Static peer config lives at
`$HERMES_BRIDGE_PEERS_CONFIG` or `~/.hermes/bridge-state/peers.json`:

```json
{
  "peers": [
    {
      "peer_id": "quest3",
      "url": "http://QUEST_LAN_IP:18084/mcp",
      "platform": "android",
      "pair_key_env": "HERMES_BRIDGE_PAIR_KEY"
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

LAN-facing peer bridge startup requires `HERMES_BRIDGE_PAIR_KEY` unless
explicitly run with the unsafe development override. Both paired agents should
use the same pair key for the simplest setup.

## Requirements

- An MCP-compatible agent (Hermes, Agent Zero, Claude Code, or any MCP client)
- Python 3.10+ with `mcp` and `httpx` packages
- Node.js with `supergateway` for stdio bridge mode (optional for HTTP-only)

## Install

**Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

**Android / Termux:**

```sh
sh bin/start-android-hermes-peer-bridge.sh
```

**Linux / macOS:**

```sh
python bin/windows-hermes-proxy-mcp.py --transport streamable-http --host 0.0.0.0 --port 18084
```

## Agent Configuration

Any MCP client can connect to the bridge:

```yaml
# Hermes / Agent Zero config
mcp_servers:
  hermes-bridge:
    url: http://localhost:18082/mcp
    timeout: 120
    connect_timeout: 30
```

## Verify

From any MCP client connected to the bridge:

```text
bridge_agent_status
```

The stable tool contract is exactly these 11 tools:

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

v1.3.0 adds the optional `automatic_pairing_v1` extension tools
`bridge_network_status` and `bridge_peer_pair`. The core list and extension list
are reported separately in `public_tool_contract`.

## Compatibility Policy

Starting with `v1.2.7`, bridge versions must remain backward compatible with the
default `bridge_agent_*` and `bridge_peer_*` tool contract unless a future
release explicitly declares a breaking bridge version.

`bridge_agent_status` exposes `min_compatible_bridge_version`,
`compatibility_policy`, and `public_tool_contract` so agents can verify whether
a peer is compatible before delegation.

## Files

- `bin/windows-hermes-proxy-mcp.py` — universal companion MCP server (filename retained for compatibility)
- `bin/hermes_bridge_network.py` — device identity, managed pairing, discovery, TLS pinning, recovery (v1.3.0+)
- `bin/bridge_pairing_tools.py` — optional MCP pairing management tools
- `bin/start-hermes-bridge-peer.ps1` — Windows peer launcher
- `bin/start-hermes-bridge-peer.sh` — POSIX/Android peer launcher
- `bin/start-android-hermes-peer-bridge.sh` — Android/Termux peer launcher
- `scripts/configure-a0-mcp.py` — A0 MCP settings helper
- `scripts/validate-network-runtime.py` — no-write runtime preview
- `config/*.example.*` — peer config and env templates
- `docs/` — setup and validation guides

## License

MIT
