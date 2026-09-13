"""Small dependency-free installation and local readiness helpers.

These helpers never discover agents or modify pairing state. HTTPS readiness is
pinned to the host's existing certificate; it does not weaken peer trust.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import ssl
import tempfile
import time
import urllib.request
from pathlib import Path


def bridge_home() -> Path:
    explicit = os.environ.get("AGENT_BRIDGE_HOME") or os.environ.get("HERMES_BRIDGE_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        canonical, legacy = base / "agent-bridge", base / "hermes"
    else:
        canonical, legacy = Path.home() / ".agent-bridge", Path.home() / ".hermes"
    return legacy if (legacy / "bridge-state").exists() else canonical


def state_dir() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_STATE_DIR") or os.environ.get("HERMES_BRIDGE_STATE_DIR") or bridge_home() / "bridge-state")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def runtime_lock(path: Path, timeout: float = 120):
    """OS-released lock, including when an installer or watchdog is interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("another bridge installation is active")
                time.sleep(0.1)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def probe(port: int, secure: bool = False, expected_revision: str = "", expected_pid: int = 0) -> dict:
    """Return only non-secret readiness fields, never response or exception text."""
    try:
        handlers = [urllib.request.ProxyHandler({})]
        if secure:
            context = ssl.create_default_context(cafile=str(state_dir() / "network/identity-cert.pem"))
            context.check_hostname = False
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(f"{'https' if secure else 'http'}://127.0.0.1:{port}/readyz", timeout=2) as response:
            data = json.loads(response.read(16384))
        ready = data.get("status") == "ready"
        revision_matches = not expected_revision or data.get("build_revision") == expected_revision
        pid_matches = not expected_pid or data.get("process_id") == expected_pid
        return {"ready": ready and revision_matches and pid_matches,
                "status": data.get("status"), "bridge_version": data.get("bridge_version"),
                "build_revision": data.get("build_revision"), "process_id": data.get("process_id"),
                "revision_matches": revision_matches, "pid_matches": pid_matches}
    except Exception as exc:
        return {"ready": False, "error": type(exc).__name__}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secure", action="store_true")
    parser.add_argument("--revision", default="")
    parser.add_argument("--pid", type=int, default=0)
    args = parser.parse_args()
    result = probe(args.port, args.secure, args.revision, args.pid)
    print(json.dumps(result))
    raise SystemExit(0 if result["ready"] else 1)
