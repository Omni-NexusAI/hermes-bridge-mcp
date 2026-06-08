#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

ENV_FILE="${HERMES_BRIDGE_ENV_FILE:-$REPO_ROOT/config/android-peer.env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi

export HERMES_BRIDGE_TRANSPORT="${HERMES_BRIDGE_TRANSPORT:-streamable-http}"
export HERMES_BRIDGE_HOST="${HERMES_BRIDGE_HOST:-0.0.0.0}"
export HERMES_BRIDGE_PORT="${HERMES_BRIDGE_PORT:-18084}"
export HERMES_BRIDGE_PEERS_CONFIG="${HERMES_BRIDGE_PEERS_CONFIG:-$HOME/.hermes/bridge-state/peers.json}"

if [ -z "${HERMES_BRIDGE_PAIR_KEY:-}" ] && [ -z "${HERMES_BRIDGE_AUTH_TOKEN:-}" ]; then
  echo "HERMES_BRIDGE_PAIR_KEY is required for LAN-facing Android peer bridge. HERMES_BRIDGE_AUTH_TOKEN is still accepted for legacy configs." >&2
  exit 2
fi

exec "${PYTHON:-python}" "$REPO_ROOT/bin/windows-hermes-proxy-mcp.py" \
  --transport "$HERMES_BRIDGE_TRANSPORT" \
  --host "$HERMES_BRIDGE_HOST" \
  --port "$HERMES_BRIDGE_PORT"
