#!/usr/bin/env python3
"""Register bridge pairing slash commands for multiple agent architectures.

Detects which agent(s) are installed on this device and registers the
pairing slash commands using each architecture's native mechanism:

  - Hermes:        quick_commands aliases in config.yaml
  - Claude Code:   .claude/commands/*.md files
  - Generic shell: scripts in ~/.local/bin/ that call the MCP tool

This ensures /manual-pair, /discovery-pair, /tailscale-pair, /pair-status,
and /repair work as real invocable commands regardless of which agent
architecture the user runs.

The MCP tools (bridge_manual_pair, bridge_pair_status, etc.) are the
universal layer — they work with any MCP client without registration.
These slash commands are a convenience shortcut for interactive use.

Usage:
    python register-commands.py          # register for all detected agents
    python register-commands.py --remove  # remove from all agents
    python register-commands.py --status   # check registration
"""

import argparse
import os
import sys
from pathlib import Path


COMMANDS = {
    "manual-pair": {
        "description": "Pair with a device by IP address. Usage: /manual-pair <name> <ip> [port]",
        "hermes_target": "/agent-bridge-pairing",
        "mcp_tool": "bridge_manual_pair",
    },
    "discovery-pair": {
        "description": "Find and pair with a device via mDNS discovery. Usage: /discovery-pair [name]",
        "hermes_target": "/agent-bridge-pairing",
        "mcp_tool": "bridge_discovery_scan",
    },
    "tailscale-pair": {
        "description": "Find and pair with a device via Tailscale. Usage: /tailscale-pair [name]",
        "hermes_target": "/agent-bridge-pairing",
        "mcp_tool": "bridge_discovery_scan",
    },
    "pair-status": {
        "description": "Show status of all paired devices and discovery state.",
        "hermes_target": "/agent-bridge-pairing",
        "mcp_tool": "bridge_pair_status",
    },
    "repair": {
        "description": "Diagnose and repair a broken peer connection. Usage: /repair <peer-id>",
        "hermes_target": "/agent-bridge-pairing",
        "mcp_tool": "bridge_repair_peer",
    },
}


# ---------------------------------------------------------------------------
# Agent detection
# ---------------------------------------------------------------------------

def detect_agents() -> list[str]:
    """Detect which agent architectures are installed."""
    agents = []

    # Hermes — look for config.yaml or hermes binary
    hermes_home = (
        os.environ.get("HERMES_HOME")
        or (os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "hermes"))
        or os.path.expanduser("~/.hermes")
    )
    if Path(hermes_home).exists() or _which("hermes"):
        agents.append("hermes")

    # Claude Code — look for .claude directory in home or project
    if Path.home().joinpath(".claude").exists():
        agents.append("claude-code")

    # Generic shell — always available as fallback
    agents.append("shell")

    return agents


def _which(cmd: str) -> str | None:
    """Check if a command is on PATH."""
    import shutil
    return shutil.which(cmd)


# ---------------------------------------------------------------------------
# Hermes registration
# ---------------------------------------------------------------------------

def find_hermes_config() -> Path:
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]) / "config.yaml"
    if os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "config.yaml"
        if candidate.exists():
            return candidate
    return Path.home() / ".hermes" / "config.yaml"


def register_hermes(config_path: Path, remove: bool = False) -> list[str]:
    """Register quick_commands aliases in Hermes config.yaml."""
    try:
        import yaml
    except ImportError:
        return ["hermes: skipped (PyYAML not installed)"]

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = {}

    if "quick_commands" not in config:
        config["quick_commands"] = {}
    qc = config["quick_commands"]
    if not isinstance(qc, dict):
        qc = {}
        config["quick_commands"] = qc

    results = []
    for cmd, spec in COMMANDS.items():
        alias = {"type": "alias", "target": spec["hermes_target"]}
        if remove:
            if cmd in qc:
                del qc[cmd]
                results.append(f"hermes: removed /{cmd}")
        else:
            if qc.get(cmd) != alias:
                qc[cmd] = alias
                results.append(f"hermes: registered /{cmd}")

    if results or remove:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    return results if results else ["hermes: all commands already registered"]


# ---------------------------------------------------------------------------
# Claude Code registration
# ---------------------------------------------------------------------------

def register_claude_code(remove: bool = False) -> list[str]:
    """Register slash commands as .claude/commands/*.md files."""
    commands_dir = Path.home() / ".claude" / "commands"
    results = []

    if remove:
        for cmd in COMMANDS:
            cmd_file = commands_dir / f"{cmd}.md"
            if cmd_file.exists():
                cmd_file.unlink()
                results.append(f"claude-code: removed /{cmd}")
        return results if results else ["claude-code: no commands found to remove"]

    commands_dir.mkdir(parents=True, exist_ok=True)
    for cmd, spec in COMMANDS.items():
        cmd_file = commands_dir / f"{cmd}.md"
        content = (
            f"# {cmd}\n\n"
            f"{spec['description']}\n\n"
            f"This command loads the agent-bridge-pairing skill. "
            f"If the MCP tool `{spec['mcp_tool']}` is available, call it directly. "
            f"Otherwise follow the skill procedure.\n\n"
            f"Arguments: $ARGUMENTS\n"
        )
        cmd_file.write_text(content, encoding="utf-8")
        results.append(f"claude-code: registered /{cmd}")

    return results


# ---------------------------------------------------------------------------
# Generic shell registration
# ---------------------------------------------------------------------------

def register_shell(remove: bool = False) -> list[str]:
    """Create shell scripts that output guidance for the agent."""
    bin_dir = Path.home() / ".local" / "bin"
    results = []

    if remove:
        for cmd in COMMANDS:
            script = bin_dir / cmd
            if script.exists():
                script.unlink()
                results.append(f"shell: removed {cmd}")
        return results if results else ["shell: no commands found to remove"]

    bin_dir.mkdir(parents=True, exist_ok=True)
    for cmd, spec in COMMANDS.items():
        script = bin_dir / cmd
        content = (
            f"#!/usr/bin/env sh\n"
            f"# {cmd} — {spec['description']}\n"
            f"# This is a bridge pairing command. Ask your agent to:\n"
            f"# 1. Call the MCP tool {spec['mcp_tool']}" 
            + (" with the provided arguments" if cmd not in ("pair-status", "discovery-pair", "tailscale-pair") else "")
            + f"\n# 2. Or load the agent-bridge-pairing skill and follow the {cmd} procedure\n"
            f"echo '{spec['description']}'\n"
            f"echo 'MCP tool: {spec['mcp_tool']}'\n"
            f"echo 'Load skill: agent-bridge-pairing'\n"
        )
        script.write_text(content, encoding="utf-8")
        try:
            script.chmod(0o755)
        except Exception:
            pass
        results.append(f"shell: installed {cmd} to {bin_dir}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register bridge pairing slash commands for all detected agents."
    )
    parser.add_argument("--remove", action="store_true", help="Remove commands.")
    parser.add_argument("--status", action="store_true", help="Check status.")
    parser.add_argument("--agent", type=str, default=None, help="Target specific agent (hermes, claude-code, shell).")
    args = parser.parse_args()

    agents = [args.agent] if args.agent else detect_agents()
    all_results = []

    for agent in agents:
        if agent == "hermes":
            config_path = find_hermes_config()
            if config_path.exists():
                all_results.extend(register_hermes(config_path, remove=args.remove))
            else:
                all_results.append(f"hermes: config not found at {config_path}")

        elif agent == "claude-code":
            claude_dir = Path.home() / ".claude"
            if claude_dir.exists():
                all_results.extend(register_claude_code(remove=args.remove))
            else:
                all_results.append("claude-code: not detected (~/.claude not found)")

        elif agent == "shell":
            all_results.extend(register_shell(remove=args.remove))

    if args.status:
        # Status mode — just check what's registered
        print("Registration status:")
        if "hermes" in agents:
            config = find_hermes_config()
            try:
                import yaml
                with open(config, "r") as f:
                    qc = (yaml.safe_load(f) or {}).get("quick_commands", {})
                for cmd in COMMANDS:
                    status = "✓" if cmd in qc else "✗"
                    print(f"  {status} hermes: /{cmd}")
            except Exception:
                print(f"  ? hermes: unable to read {config}")
        if "claude-code" in agents:
            commands_dir = Path.home() / ".claude" / "commands"
            for cmd in COMMANDS:
                status = "✓" if commands_dir.joinpath(f"{cmd}.md").exists() else "✗"
                print(f"  {status} claude-code: /{cmd}")
        print(f"\nDetected agents: {', '.join(agents)}")
    else:
        print(f"Detected agents: {', '.join(agents)}")
        for result in all_results:
            print(f"  {result}")
        print("\nSlash commands available:")
        for cmd in COMMANDS:
            print(f"  /{cmd} — {COMMANDS[cmd]['description']}")
        print("\nNote: MCP tools (bridge_manual_pair, bridge_pair_status, etc.)")
        print("are always available to any MCP client without registration.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
