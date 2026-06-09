# Android / Quest Hermes Peer Bridge Setup

Target Hermes Bridge MCP version: `v1.2.7`.

This setup is for Android-compatible Hermes peers, with Quest 3 as the first
test device. It runs the bridge directly with the Python MCP SDK and does not
require Node.js or `supergateway`.

## Install

```sh
pkg install python git
git clone https://github.com/Omni-NexusAI/hermes-bridge-mcp.git
cd hermes-bridge-mcp
git checkout development
python -m pip install -r requirements-android.txt
```

## Configure

```sh
mkdir -p ~/.hermes/bridge-state
cp config/android-peer.env.example config/android-peer.env
cp config/peers.example.json ~/.hermes/bridge-state/peers.json
```

Edit `config/android-peer.env` and `~/.hermes/bridge-state/peers.json`:

- set `HERMES_BRIDGE_PAIR_KEY` to the same strong secret on Windows and Android
- replace `WINDOWS_LAN_IP` with the Windows machine LAN IP
- replace `QUEST_LAN_IP` with the Quest LAN IP when copying the config back to
  Windows

For more than one paired agent, keep multiple peer records in `peers.json`.
Each record can use the shared `HERMES_BRIDGE_PAIR_KEY`, or a distinct
`pair_key_env` if you want separate secrets per peer later.

## Run

```sh
sh bin/start-hermes-bridge-peer.sh
```

The Android peer bridge listens on:

```text
http://QUEST_LAN_IP:18084/mcp
```

## First Smoke Tests

From the Windows peer, call:

```text
bridge_peer_status(peer_id="quest3")
```

The returned status should include `bridge_version: "v1.2.7"` and a generic
Android platform value. The unique device identity should come from `peer_id`,
not from a device-specific tool surface.

Then start a remote task:

```text
bridge_peer_delegate_start(
  peer_id="quest3",
  prompt="Reply exactly QUEST_PEER_OK.",
  conversation_key="first-bidirectional-smoke"
)
```

From the Quest peer, call the reverse direction:

```text
bridge_peer_status(peer_id="windows")
```

The returned status should include `bridge_version: "v1.2.7"` and a generic
Windows platform value.

Then:

```text
bridge_peer_delegate_start(
  peer_id="windows",
  prompt="Reply exactly WINDOWS_PEER_OK.",
  conversation_key="first-bidirectional-smoke"
)
```
