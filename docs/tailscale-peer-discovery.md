# Tailscale Peer Discovery

Tailscale can carry Hermes Bridge traffic between devices on different local
networks. It supplies reachability only: Hermes Bridge still requires explicit
fingerprint approval, pins each peer certificate, and creates separate
per-peer bearer credentials.

## Tailnet Policy

Install Tailscale and join each intended bridge device to the same tailnet.
Assign `tag:hermes-bridge` only to devices that should be probed for the secure
bridge listener. A current Tailscale grants policy can express the minimum
reachability as:

```json
{
  "tagOwners": {
    "tag:hermes-bridge": ["autogroup:admin"]
  },
  "grants": [
    {
      "src": ["tag:hermes-bridge"],
      "dst": ["tag:hermes-bridge"],
      "ip": ["tcp:18443"]
    }
  ]
}
```

Merge these entries into the existing tailnet policy rather than replacing it.
Tailnets using legacy ACL syntax should grant the same tagged source-to-tagged
destination TCP port and retain their existing rules.

## Discovery Providers

The backend supports two inventory providers. Both providers feed the same
Hermes identity validation and approval flow:

- `cli` reads `tailscale status --json` from a local Tailscale daemon.
- `api` reads tailnet device inventory from the Tailscale HTTP API.
- `auto` uses the CLI when available and falls back to the API when the CLI is
  unavailable and API credentials are configured.

```text
HERMES_BRIDGE_AUTO_DISCOVERY=1
HERMES_BRIDGE_DISCOVERY_BACKEND=tailscale
HERMES_BRIDGE_TAILSCALE_PROVIDER=auto
HERMES_BRIDGE_TAILSCALE_TAG=tag:hermes-bridge
HERMES_BRIDGE_TAILSCALE_SCAN_INTERVAL=30
```

`HERMES_BRIDGE_TAILSCALE_CLI` may point to a non-default executable. Scan
intervals are clamped to 10-300 seconds. The backend uses only online,
unexpired, tagged peers and probes their official Tailscale IPv4 or IPv6
address on the configured secure bridge port, normally `18443`.

For API inventory, configure:

```text
HERMES_BRIDGE_TAILSCALE_PROVIDER=api
HERMES_BRIDGE_TAILSCALE_API_TOKEN=tskey-...
HERMES_BRIDGE_TAILNET=example.com
HERMES_BRIDGE_TAILSCALE_API_BASE=https://api.tailscale.com/api/v2
```

The API token is sent only to Tailscale's API endpoint and is never returned
through MCP status. Prefer the least privileged and shortest lived credential
that can list devices. Rotate it according to Tailscale policy, and do not
place it in shared bug reports or committed config.

The backend never reports the tailnet inventory through MCP. Public discovery
health contains only status, timestamps, counts, interval, and a sanitized
error category.

## Android and Passive Devices

A desktop bridge can discover and initiate pairing with a tagged Android node.
The Android Tailscale app does not need to expose its CLI to Termux. Configure
the Android bridge with the Tailscale backend so pairing requests from official
Tailscale source ranges are accepted; an unavailable local CLI leaves outbound
discovery degraded but does not stop the secure bridge listener.

To let Android or another passive environment initiate discovery itself, use
the `api` provider. The device must already be joined to the tailnet through
the normal Tailscale app or admin flow. If the bridge cannot infer its own
Tailscale address from an API device record matching the local hostname or
bridge display name, set `HERMES_BRIDGE_ADVERTISE_ADDRESS` to that device's
Tailscale IP or MagicDNS name before approving peers.

Approval is bilateral: after one desktop discovers the Android bridge and the
operator approves its full fingerprint, both peers receive pinned identities
and credentials. The Android node can then use the normal `bridge_peer_*`
tools without independently discovering the desktop first.

This PR does not enroll devices into Tailscale, create auth keys, or authorize
devices automatically. Those are administrative actions and should remain a
separate explicit-admin workflow using short-lived, single-use, tagged auth
keys if they are added later.

## Approval and Diagnostics

1. Call `bridge_network_status` on both devices through their local MCP
   connection.
2. Compare the complete SHA-256 identity fingerprint out of band.
3. Approve the candidate with `bridge_peer_pair(action="approve", ...)`.
4. Confirm `certificate_pinned` and `credential_configured` are true.
5. Exercise `bridge_peer_status` in both directions before delegating work.

Missing CLI, logged-out Tailscale, missing or expired API credentials, malformed
inventory JSON, unreachable peers, and probe timeouts degrade discovery health
but do not stop MCP. Do not place auth keys, OAuth credentials, private keys,
or tailnet inventory in bug reports.

## Isolated Validation

Automated tests mock `tailscale status --json`, Tailscale API responses, and
identity probes. They must never invoke the real Tailscale CLI, contact the
real Tailscale API, or inspect a real tailnet. Live device validation remains a
separate, explicitly authorized step using unpaired test devices.
