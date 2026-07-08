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
export HERMES_BRIDGE_SECURE_PORT="${HERMES_BRIDGE_SECURE_PORT:-18443}"
export HERMES_BRIDGE_PEERS_CONFIG="${HERMES_BRIDGE_PEERS_CONFIG:-$HOME/.hermes/bridge-state/peers.json}"

if [ -z "${HERMES_BRIDGE_PAIR_KEY:-}" ] && [ -z "${HERMES_BRIDGE_AUTH_TOKEN:-}" ] && [ "${HERMES_BRIDGE_AUTO_DISCOVERY:-0}" != "1" ]; then
  echo "Set HERMES_BRIDGE_PAIR_KEY for legacy peers or HERMES_BRIDGE_AUTO_DISCOVERY=1 for automatic pairing." >&2
  exit 2
fi

legacy_pid=""
secure_pid=""

cleanup() {
  [ -z "$legacy_pid" ] || kill "$legacy_pid" 2>/dev/null || true
  [ -z "$secure_pid" ] || kill "$secure_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if [ -n "${HERMES_BRIDGE_PAIR_KEY:-}" ] || [ -n "${HERMES_BRIDGE_AUTH_TOKEN:-}" ]; then
  "${PYTHON:-python}" "$REPO_ROOT/bin/windows-hermes-proxy-mcp.py" \
    --transport "$HERMES_BRIDGE_TRANSPORT" --host "$HERMES_BRIDGE_HOST" --port "$HERMES_BRIDGE_PORT" &
  legacy_pid=$!
fi

if [ "${HERMES_BRIDGE_AUTO_DISCOVERY:-1}" = "1" ]; then
  "${PYTHON:-python}" "$REPO_ROOT/bin/windows-hermes-proxy-mcp.py" \
    --transport "$HERMES_BRIDGE_TRANSPORT" --host "$HERMES_BRIDGE_HOST" \
    --port "$HERMES_BRIDGE_SECURE_PORT" --secure-network &
  secure_pid=$!
  wait "$secure_pid"
else
  if [ -n "$legacy_pid" ]; then
    wait "$legacy_pid"
  fi
fi
