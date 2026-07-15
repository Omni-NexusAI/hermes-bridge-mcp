#!/usr/bin/env python3
"""Portable Hermes Bridge installer.

This deliberately manages only the bridge payload and its virtual environment.
It never installs Hermes, changes agent configuration, pairs devices, or touches
bridge-state.  It is safe to run from a checkout or a downloaded source archive.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import venv
from pathlib import Path

REPO = "Omni-NexusAI/hermes-bridge-mcp"
PAYLOAD = ("bin", "config", "docs", "scripts", "requirements-bridge.txt", "README.md")


def hermes_home() -> Path:
    explicit = os.environ.get("HERMES_BRIDGE_HOME") or os.environ.get("HERMES_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"
    return Path.home() / ".hermes"


def bridge_root() -> Path:
    return Path(os.environ.get("HERMES_BRIDGE_INSTALL_ROOT", hermes_home() / "bridge-runtime")).expanduser()


def version(source: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(source), "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:
        return time.strftime("source-%Y%m%d%H%M%S", time.gmtime())


def python_in(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def current_release(root: Path) -> Path | None:
    marker = root / "current.json"
    try:
        return Path(json.loads(marker.read_text(encoding="utf-8"))["release"])
    except Exception:
        return None


def install(source: Path, start: bool, dependencies: bool) -> dict:
    source = source.resolve()
    missing = [item for item in PAYLOAD if not (source / item).exists()]
    if missing:
        raise RuntimeError(f"source is missing bridge payload: {', '.join(missing)}")
    root = bridge_root()
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    release = releases / version(source)
    if not release.exists():
        stage = Path(tempfile.mkdtemp(prefix="hermes-bridge-", dir=releases))
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
    venv_dir = root / "venv"
    if dependencies and not python_in(venv_dir).exists():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    if dependencies:
        subprocess.check_call([str(python_in(venv_dir)), "-m", "pip", "install", "-r", str(release / "requirements-bridge.txt")])
    (root / "current.json").write_text(json.dumps({"release": str(release), "previous": str(prior) if prior else None}, indent=2), encoding="utf-8")
    result = {"status": "installed", "release": str(release), "bridge_root": str(root), "state_preserved": str(hermes_home() / "bridge-state")}
    if start:
        result["start"] = "Run the platform launcher from the installed release; automatic startup requires --enable-startup."
    return result


def doctor() -> dict:
    home, root = hermes_home(), bridge_root()
    hermes = os.environ.get("HERMES_EXE") or shutil.which("hermes")
    return {"platform": sys.platform, "python": sys.executable, "hermes_home": str(home), "bridge_root": str(root), "current_release": str(current_release(root) or ""), "hermes_executable": bool(hermes), "action": "Install Hermes separately or set HERMES_EXE." if not hermes else "ready"}


def rollback() -> dict:
    root = bridge_root()
    current = json.loads((root / "current.json").read_text(encoding="utf-8"))
    previous = current.get("previous")
    if not previous or not Path(previous).is_dir():
        raise RuntimeError("no previous bridge release is available")
    (root / "current.json").write_text(json.dumps({"release": previous, "previous": current.get("release")}, indent=2), encoding="utf-8")
    return {"status": "rolled_back", "release": previous}


def main() -> int:
    parser = argparse.ArgumentParser(description="Install or maintain the Hermes Bridge payload without touching pairing state.")
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
