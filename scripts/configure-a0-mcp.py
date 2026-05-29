#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


DEFAULT_URL = "http://host.docker.internal:18082/mcp"


def load_settings(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
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


def configure(settings: dict, name: str, url: str) -> tuple[dict, bool]:
    parsed = parse_mcp_servers(settings)
    servers = parsed["mcpServers"]
    desired = {
        "description": "Hermes Bridge MCP: delegate container agent tasks to native Windows Hermes",
        "type": "streamable-http",
        "url": url,
        "disabled": False,
        "init_timeout": 30,
        "tool_timeout": 900,
    }
    changed = servers.get(name) != desired
    servers[name] = desired
    settings["mcp_servers"] = json.dumps(parsed, indent=2, ensure_ascii=False)
    return settings, changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Add or update Hermes Bridge MCP in A0 settings.json.")
    parser.add_argument("--settings", default="/a0/usr/settings.json", help="Path to A0 settings.json")
    parser.add_argument("--name", default="hermes-bridge", help="A0 MCP server name")
    parser.add_argument("--url", default=DEFAULT_URL, help="Hermes Bridge MCP URL")
    parser.add_argument("--dry-run", action="store_true", help="Print the resulting settings without writing")
    args = parser.parse_args()

    settings_path = Path(args.settings)
    settings = load_settings(settings_path)
    updated, changed = configure(settings, args.name, args.url)

    if args.dry_run:
        parsed = json.loads(updated["mcp_servers"])
        servers = parsed.get("mcpServers", {})
        print(json.dumps({
            "settings": str(settings_path),
            "server": args.name,
            "url": args.url,
            "changed": changed,
            "dry_run": True,
            "server_count": len(servers),
            "entry": servers.get(args.name),
            "restart_a0_required": True,
        }, indent=2, ensure_ascii=False))
        return 0

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = settings_path.with_name(f"{settings_path.name}.bak.{timestamp}")
    shutil.copy2(settings_path, backup_path)
    with settings_path.open("w", encoding="utf-8") as handle:
        json.dump(updated, handle, indent=4, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps({
        "settings": str(settings_path),
        "backup": str(backup_path),
        "server": args.name,
        "url": args.url,
        "changed": changed,
        "restart_a0_required": True,
        "verify_in_ui": "Settings > MCP/A2A > External MCP Servers > Open; look for hermes-bridge in the JSON and hermes_bridge in server status.",
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
