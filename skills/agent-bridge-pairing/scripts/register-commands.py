#!/usr/bin/env python3
"""Compatibility installer for clients that do not expose MCP Prompts.

Native MCP Prompts are the preferred interface. This script preserves the
v1.3.0 command shortcuts for Hermes, Claude Code, and generic shells.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


COMMANDS = {
    "manual-pair": {
        "description": "Pair with a device by secure URL and verified fingerprint.",
        "tool": "bridge_manual_pair",
    },
    "discovery-pair": {
        "description": "Find and approve a device through bridge discovery.",
        "tool": "bridge_discovery_scan",
    },
    "tailscale-pair": {
        "description": "Find and approve a device through configured Tailscale discovery.",
        "tool": "bridge_discovery_scan",
    },
    "pair-status": {
        "description": "Show paired devices, discovery state, and connectivity.",
        "tool": "bridge_pair_status",
    },
    "repair": {
        "description": "Diagnose and repair one identified peer connection.",
        "tool": "bridge_repair_peer",
    },
}


def hermes_config() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser() / "config.yaml"
    if os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "config.yaml"
        if local.exists():
            return local
    return Path.home() / ".hermes" / "config.yaml"


def detect_agents() -> list[str]:
    agents = []
    if hermes_config().exists() or shutil.which("hermes"):
        agents.append("hermes")
    if (Path.home() / ".claude").exists():
        agents.append("claude-code")
    agents.append("shell")
    return agents


def register_hermes(remove: bool) -> list[str]:
    try:
        import yaml
    except ImportError:
        return ["hermes: skipped (PyYAML not installed)"]

    path = hermes_config()
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        config = {}
    commands = config.setdefault("quick_commands", {})
    if not isinstance(commands, dict):
        commands = {}
        config["quick_commands"] = commands

    changed = []
    for name in COMMANDS:
        if remove:
            if commands.pop(name, None) is not None:
                changed.append(f"hermes: removed /{name}")
        else:
            desired = {"type": "alias", "target": "/agent-bridge-pairing"}
            if commands.get(name) != desired:
                commands[name] = desired
                changed.append(f"hermes: registered /{name}")
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return changed or ["hermes: no changes"]


def register_claude_code(remove: bool) -> list[str]:
    directory = Path.home() / ".claude" / "commands"
    results = []
    for name, spec in COMMANDS.items():
        path = directory / f"{name}.md"
        if remove:
            if path.exists():
                path.unlink()
                results.append(f"claude-code: removed /{name}")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {name}\n\n{spec['description']}\n\n"
            f"Use MCP tool `{spec['tool']}` or the native `{name.replace('-', '_')}` "
            f"MCP Prompt. Never approve an unverified fingerprint.\n\n"
            "Arguments: $ARGUMENTS\n",
            encoding="utf-8",
        )
        results.append(f"claude-code: registered /{name}")
    return results or ["claude-code: no changes"]


def register_shell(remove: bool) -> list[str]:
    directory = Path.home() / ".local" / "bin"
    results = []
    for name, spec in COMMANDS.items():
        path = directory / name
        if remove:
            if path.exists():
                path.unlink()
                results.append(f"shell: removed {name}")
            continue
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "#!/usr/bin/env sh\n"
            f"echo '{spec['description']}'\n"
            f"echo 'MCP tool: {spec['tool']}'\n"
            f"echo 'MCP Prompt: {name.replace('-', '_')}'\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o755)
        except OSError:
            pass
        results.append(f"shell: registered {name}")
    return results or ["shell: no changes"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install compatibility commands for clients without MCP Prompts."
    )
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--agent", choices=("hermes", "claude-code", "shell"))
    args = parser.parse_args()

    agents = [args.agent] if args.agent else detect_agents()
    if args.status:
        print(f"Detected agents: {', '.join(agents)}")
        print("Native MCP Prompts: " + ", ".join(name.replace("-", "_") for name in COMMANDS))
        return 0

    handlers = {
        "hermes": register_hermes,
        "claude-code": register_claude_code,
        "shell": register_shell,
    }
    print(f"Detected agents: {', '.join(agents)}")
    for agent in agents:
        for result in handlers[agent](args.remove):
            print(f"  {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
