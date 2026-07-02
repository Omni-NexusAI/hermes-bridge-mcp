#!/usr/bin/env python3
"""Register bridge pairing slash commands as Hermes quick_commands.

This script adds quick_commands aliases to config.yaml so that
/manual-pair, /discovery-pair, /tailscale-pair, /pair-status, and /repair
are real registered slash commands that redirect to the
agent-bridge-pairing skill.

Works on any platform (Windows, Linux, macOS, Android/Termux) — wherever
Hermes is installed. Uses PyYAML if available, falls back to manual YAML
manipulation.

Usage:
    python scripts/register-commands.py          # register commands
    python scripts/register-commands.py --remove  # remove commands
    python scripts/register-commands.py --status   # check registration

The script is idempotent — running it multiple times produces the same
result. It preserves all existing quick_commands entries.
"""

import argparse
import os
import sys
from pathlib import Path


# The commands to register: { command_name: { "type": "alias", "target": "..." } }
COMMANDS = {
    "manual-pair": {"type": "alias", "target": "/agent-bridge-pairing"},
    "discovery-pair": {"type": "alias", "target": "/agent-bridge-pairing"},
    "tailscale-pair": {"type": "alias", "target": "/agent-bridge-pairing"},
    "pair-status": {"type": "alias", "target": "/agent-bridge-pairing"},
    "repair": {"type": "alias", "target": "/agent-bridge-pairing"},
}


def find_config_path() -> Path:
    """Find the Hermes config.yaml path."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]) / "config.yaml"
    if os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "config.yaml"
        if candidate.exists():
            return candidate
    return Path.home() / ".hermes" / "config.yaml"


def load_yaml(path: Path) -> dict:
    """Load YAML file, return empty dict if missing or invalid."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # No PyYAML — try Hermes's own loader
        try:
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from utils import fast_safe_load
            with open(path, "r", encoding="utf-8") as f:
                return fast_safe_load(f) or {}
        except Exception:
            return {}
    except FileNotFoundError:
        return {}


def save_yaml(path: Path, data: dict) -> None:
    """Save YAML file."""
    try:
        import yaml
    except ImportError:
        print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def register(config_path: Path) -> None:
    """Register the quick_commands aliases in config.yaml."""
    config = load_yaml(config_path)
    if not config:
        config = {}
    if "quick_commands" not in config:
        config["quick_commands"] = {}
    qc = config["quick_commands"]
    if not isinstance(qc, dict):
        qc = {}
        config["quick_commands"] = qc

    added = []
    updated = []
    for cmd, spec in COMMANDS.items():
        if cmd in qc:
            if qc[cmd] != spec:
                qc[cmd] = spec
                updated.append(cmd)
        else:
            qc[cmd] = spec
            added.append(cmd)

    save_yaml(config_path, config)

    if added:
        print(f"  Added: {', '.join(added)}")
    if updated:
        print(f"  Updated: {', '.join(updated)}")
    if not added and not updated:
        print("  All commands already registered (no changes needed).")
    print(f"Config: {config_path}")
    print("")
    print("The following slash commands are now available:")
    for cmd in COMMANDS:
        print(f"  /{cmd}")


def remove(config_path: Path) -> None:
    """Remove the quick_commands aliases from config.yaml."""
    config = load_yaml(config_path)
    qc = config.get("quick_commands", {})
    if not isinstance(qc, dict):
        return

    removed = []
    for cmd in COMMANDS:
        if cmd in qc:
            del qc[cmd]
            removed.append(cmd)

    if removed:
        save_yaml(config_path, config)
        print(f"  Removed: {', '.join(removed)}")
    else:
        print("  No bridge commands found (nothing to remove).")
    print(f"Config: {config_path}")


def status(config_path: Path) -> None:
    """Check registration status."""
    config = load_yaml(config_path)
    qc = config.get("quick_commands", {})
    if not isinstance(qc, dict):
        qc = {}

    print(f"Config: {config_path}")
    print("")
    all_registered = True
    for cmd, spec in COMMANDS.items():
        if cmd in qc:
            if qc[cmd] == spec:
                print(f"  ✓ /{cmd} → {spec['target']}")
            else:
                print(f"  ⚠ /{cmd} exists but has different config: {qc[cmd]}")
                all_registered = False
        else:
            print(f"  ✗ /{cmd} — not registered")
            all_registered = False

    if all_registered:
        print("\nAll bridge commands registered.")
    else:
        print("\nSome commands not registered. Run without --status to register.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register bridge pairing slash commands in Hermes config."
    )
    parser.add_argument(
        "--remove",
        action="store_true",
        help="Remove the commands instead of registering them.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check registration status without making changes.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (auto-detected if not specified).",
    )
    args = parser.parse_args()

    config_path = Path(args.config) if args.config else find_config_path()

    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        print("Make sure Hermes is installed on this device.", file=sys.stderr)
        return 1

    if args.status:
        status(config_path)
    elif args.remove:
        remove(config_path)
    else:
        register(config_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
