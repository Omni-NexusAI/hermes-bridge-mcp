#!/usr/bin/env sh
set -eu
if [ -n "${AGENT_BRIDGE_HOME:-}" ]; then
  export HERMES_BRIDGE_HOME="$AGENT_BRIDGE_HOME"
fi
for name in PEER_ID HOST PORT SECURE_PORT AUTO_DISCOVERY DISCOVERY_BACKEND \
  ADVERTISE_ADDRESS PAIR_KEY PEERS_CONFIG VERSION; do
  eval "canonical=\${AGENT_BRIDGE_${name}:-}"
  if [ -n "$canonical" ]; then
    eval "export HERMES_BRIDGE_${name}=\$canonical"
  fi
done
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$SCRIPT_DIR/start-hermes-bridge-peer.sh" "$@"
