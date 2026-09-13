#!/usr/bin/env python3
"""Portable Agent Bridge installer.

This deliberately manages only the bridge payload and its virtual environment.
It never installs Hermes, changes agent configuration, pairs devices, or touches
bridge-state.  It is safe to run from a checkout or a downloaded source archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
from agent_bridge_runtime import atomic_json, bridge_home, state_dir, runtime_lock

REPO = "Omni-NexusAI/hermes-bridge-mcp"
PAYLOAD = ("bin", "config", "docs", "scripts", "requirements-bridge.txt", "README.md")


def agent_bridge_home() -> Path:
    return bridge_home()


def hermes_home() -> Path:
    """Compatibility alias retained for callers of the Hermes-era bootstrap API."""
    return agent_bridge_home()


def bridge_root() -> Path:
    return Path(
        os.environ.get(
            "AGENT_BRIDGE_INSTALL_ROOT",
            os.environ.get("HERMES_BRIDGE_INSTALL_ROOT", agent_bridge_home() / "bridge-runtime"),
        )
    ).expanduser()


def version(source: Path) -> str:
    # A dirty checkout must never silently reuse an older payload at the same SHA.
    digest = hashlib.sha256()
    for item in sorted(PAYLOAD):
        path = source / item
        for file in sorted(path.rglob("*") if path.is_dir() else [path]):
            if file.is_file() and "__pycache__" not in file.parts and file.suffix != ".pyc":
                digest.update(file.relative_to(source).as_posix().encode())
                digest.update(b"\0")
                digest.update(file.read_bytes())
    return "payload-" + digest.hexdigest()[:24]


def python_in(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def current_release(root: Path) -> Path | None:
    marker = root / "current.json"
    try:
        return Path(json.loads(marker.read_text(encoding="utf-8"))["release"])
    except Exception:
        return None


def install(source: Path, start: bool, dependencies: bool) -> dict:
    with runtime_lock(bridge_root() / "install.lock"):
        return _install(source, start, dependencies)


def _install(source: Path, start: bool, dependencies: bool) -> dict:
    source = source.resolve()
    missing = [item for item in PAYLOAD if not (source / item).exists()]
    if missing:
        raise RuntimeError(f"source is missing bridge payload: {', '.join(missing)}")
    root = bridge_root()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / version(source)
    if not release.exists():
        stage = Path(tempfile.mkdtemp(prefix="agent-bridge-", dir=releases))
        try:
            for item in PAYLOAD:
                src, dst = source / item, stage / item
                if src.is_dir():
                    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(src, dst)
            stage.replace(release)
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
    prior = current_release(root)
    # Dependency changes belong to the candidate. Rollback retains the old
    # interpreter and packages, rather than only reverting Python source.
    venv_dir = root / "environments" / release.name
    if dependencies and not python_in(venv_dir).exists():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    if dependencies:
        subprocess.check_call([str(python_in(venv_dir)), "-m", "pip", "install", "-r", str(release / "requirements-bridge.txt")])
    marker_path = root / "current.json"
    previous_marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else {}
    runtime_python = str(python_in(venv_dir)) if dependencies else None
    atomic_json(release / "runtime.json", {"python": runtime_python, "build_revision": release.name})
    if prior != release:
        atomic_json(marker_path, {"release": str(release), "python": runtime_python,
                    "previous": str(prior) if prior else None,
                    "previous_python": previous_marker.get("python") or str(python_in(root / "venv"))})
    elif dependencies:
        previous_marker["python"] = runtime_python
        atomic_json(marker_path, previous_marker)
    result = {
        "status": "installed",
        "product": "Agent Bridge MCP",
        "release": str(release),
        "bridge_root": str(root),
        "state_preserved": str(agent_bridge_home() / "bridge-state"),
    }
    if start:
        if os.name == "nt":
            launcher = release / "bin" / "start-agent-bridge.ps1"
            peer_launcher = release / "bin" / "start-agent-bridge-peer.ps1"
            subprocess.check_call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher)])
            subprocess.check_call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(peer_launcher)])
            result["start"] = "native local and secure peer listeners started"
        else:
            result["start"] = "Install complete; run bin/start-agent-bridge-peer.sh for the peer listener."
    return result


def doctor() -> dict:
    home, root = agent_bridge_home(), bridge_root()
    hermes = os.environ.get("HERMES_EXE") or shutil.which("hermes")
    release = current_release(root)
    marker = json.loads((root / "current.json").read_text(encoding="utf-8")) if release else {}
    runtime_python = Path(marker.get("python") or python_in(root / "venv"))
    try:
        from agent_bridge_universal import UniversalAgentRegistry
        adapters = UniversalAgentRegistry(state_dir(), Path(hermes or "__missing_hermes__")).public_agents()
        adapter_error = None
    except Exception as exc:
        adapters, adapter_error = [], getattr(exc, "code", type(exc).__name__)
    usable = any(item["available"] and item["enabled"] for item in adapters)
    return {
        "product": "Agent Bridge MCP",
        "platform": sys.platform,
        "python": sys.executable,
        "agent_bridge_home": str(home),
        "hermes_home": str(home),
        "bridge_root": str(root),
        "current_release": str(current_release(root) or ""),
        "hermes_executable": bool(hermes),
        "runtime_python": str(runtime_python),
        "runtime_installed": runtime_python.is_file(),
        "adapters": adapters,
        "adapter_error": adapter_error,
        "authentication": "not_probed",
        "approval": "host_owned_not_changed",
        "listener_readiness": "not_probed",
        "action": "Fix the adapter configuration." if adapter_error else (
            "Run bootstrap.py install to install runtime dependencies." if not runtime_python.is_file() else (
                "Install an agent adapter or configure agents.json." if not usable else "configuration_ready; verify listeners and adapter authentication")),
    }


def rollback() -> dict:
    with runtime_lock(bridge_root() / "install.lock"):
        return _rollback()


def _rollback() -> dict:
    root = bridge_root()
    current = json.loads((root / "current.json").read_text(encoding="utf-8"))
    previous = current.get("previous")
    if not previous or not Path(previous).is_dir():
        raise RuntimeError("no previous bridge release is available")
    atomic_json(root / "current.json", {"release": previous, "python": current.get("previous_python"),
                "previous": current.get("release"), "previous_python": current.get("python")})
    return {"status": "rolled_back", "release": previous}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or maintain Agent Bridge MCP without touching pairing state.")
    parser.add_argument("command", choices=("install", "update", "doctor", "rollback"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--no-deps", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.command in {"install", "update"}:
        result = install(args.source, args.start, not args.no_deps)
    elif args.command == "doctor":
        result = doctor()
    else:
        result = rollback()
    print(json.dumps(result, indent=2) if args.json else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
