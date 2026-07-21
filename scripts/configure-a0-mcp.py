#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path


DEFAULT_URL = "http://host.docker.internal:18082/mcp"
DEFAULT_HEALTH_URL = "http://host.docker.internal:18082/healthz"
DEFAULT_INIT_TIMEOUT = 30
DEFAULT_TOOL_TIMEOUT = 900


def load_settings(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def parse_mcp_servers(settings: dict) -> dict:
    raw = settings.get("mcp_servers")
    if raw in (None, ""):
        return {"mcpServers": {}}
    if isinstance(raw, str):
        parsed = json.loads(raw)
    elif isinstance(raw, dict):
        parsed = raw
    else:
        raise TypeError("settings.mcp_servers must be a JSON string or object")
    if not isinstance(parsed, dict):
        raise TypeError("settings.mcp_servers must parse to an object")
    servers = parsed.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise TypeError("settings.mcp_servers.mcpServers must be an object")
    return parsed


def configure(settings: dict, name: str, url: str, bearer_token: str = "") -> tuple[dict, bool]:
    parsed = parse_mcp_servers(settings)
    servers = parsed["mcpServers"]
    desired = {
        "description": (
            "Hermes Bridge MCP: use bridge_agent_* and bridge_peer_* tools through "
            "the normal MCP interface. Do not use messenger gateway tools or raw "
            "HTTP clients except for diagnostics."
        ),
        "type": "streamable-http",
        "url": url,
        "disabled": False,
        "init_timeout": DEFAULT_INIT_TIMEOUT,
        "connect_timeout": DEFAULT_INIT_TIMEOUT,
        "tool_timeout": DEFAULT_TOOL_TIMEOUT,
        "timeout": DEFAULT_TOOL_TIMEOUT,
    }
    if bearer_token:
        desired["headers"] = {"Authorization": f"Bearer {bearer_token}"}
    changed = servers.get(name) != desired
    servers[name] = desired
    settings["mcp_servers"] = json.dumps(parsed, indent=2, ensure_ascii=False)
    return settings, changed


def check_health(url: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            return {
                "url": url,
                "ok": response.status == 200 and (body == "ok" or json.loads(body).get("status") in {"ok", "ready"}),
                "status": response.status,
                "body": body,
            }
    except (OSError, urllib.error.URLError) as exc:
        return {
            "url": url,
            "ok": False,
            "error": str(exc),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Add or update Hermes Bridge MCP in A0 settings.json.")
    parser.add_argument("--settings", default="/a0/usr/settings.json", help="Path to A0 settings.json")
    parser.add_argument("--name", default="hermes-bridge", help="A0 MCP server name")
    parser.add_argument("--url", default=DEFAULT_URL, help="Hermes Bridge MCP URL")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL, help="Hermes Bridge health endpoint")
    parser.add_argument("--check-health", action="store_true", help="Check the bridge health endpoint after configuring")
    parser.add_argument("--token-file", help="File containing the local bridge bearer token")
    parser.add_argument("--dry-run", action="store_true", help="Print the resulting settings without writing")
    args = parser.parse_args()

    settings_path = Path(args.settings)
    settings = load_settings(settings_path)
    token_path = Path(args.token_file) if args.token_file else Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / "bridge-state" / "local-mcp-token"
    bearer_token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else ""
    updated, changed = configure(settings, args.name, args.url, bearer_token)

    parsed = json.loads(updated["mcp_servers"])
    servers = parsed.get("mcpServers", {})
    result = {
        "settings": str(settings_path),
        "server": args.name,
        "url": args.url,
        "changed": changed,
        "server_count": len(servers),
        "entry": {key: value for key, value in servers.get(args.name, {}).items() if key != "headers"},
        "bearer_token_configured": bool(bearer_token),
        "restart_a0_required": True,
    }
    if args.check_health:
        result["health"] = check_health(args.health_url)

    if args.dry_run:
        result["dry_run"] = True
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = settings_path.with_name(f"{settings_path.name}.bak.{timestamp}")
    shutil.copy2(settings_path, backup_path)
    with settings_path.open("w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=4, ensure_ascii=False)
        handle.write("\n")

    result["backup"] = str(backup_path)
    result["verify_in_ui"] = (
        "Restart A0, then verify Settings > MCP/A2A > External MCP Servers > "
        "Open; look for hermes-bridge in JSON and hermes_bridge in server status."
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
