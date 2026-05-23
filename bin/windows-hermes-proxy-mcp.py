from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

HERMES_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
HERMES_AGENT = HERMES_HOME / "hermes-agent"
HERMES_EXE = HERMES_AGENT / "venv" / "Scripts" / "hermes.exe"
DEFAULT_CWD = Path.home()

if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

from mcp_serve import EventBridge, create_mcp_server  # noqa: E402


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _tail(text: str, max_chars: int = 8000) -> str:
    if not text:
        return ""
    return text[-max_chars:]


def _convert_cwd(cwd: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    if cwd is None or str(cwd).strip() == "":
        return DEFAULT_CWD, None

    raw = str(cwd).strip().strip('"')
    if raw.startswith("/mnt/") and len(raw) >= 7 and raw[5].isalpha() and raw[6] == "/":
        drive = raw[5].upper()
        rest = raw[7:].replace("/", "\\")
        raw = f"{drive}:\\{rest}" if rest else f"{drive}:\\"

    path = Path(raw).expanduser()
    if not path.exists():
        return None, f"cwd does not exist: {raw}"
    if not path.is_dir():
        return None, f"cwd is not a directory: {raw}"
    return path, None


def _run_hidden(args: list[str], cwd: Optional[Path], timeout_seconds: int) -> dict:
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
        startupinfo=startupinfo,
    )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            proc.kill()
        stdout, stderr = proc.communicate(timeout=10)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "stdout": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }


def _status_payload() -> str:
    version = _run_hidden([str(HERMES_EXE), "--version"], cwd=HERMES_AGENT, timeout_seconds=30)
    config_path = _run_hidden([str(HERMES_EXE), "config", "path"], cwd=HERMES_AGENT, timeout_seconds=30)
    gateway = _run_hidden([str(HERMES_EXE), "gateway", "status"], cwd=HERMES_AGENT, timeout_seconds=60)
    return _json({
        "hermes_exe": str(HERMES_EXE),
        "hermes_home": str(HERMES_HOME),
        "default_cwd": str(DEFAULT_CWD),
        "delegate_runner_available": HERMES_EXE.exists(),
        "version": version,
        "config_path": config_path,
        "gateway_status": gateway,
    })


def _delegate_payload(
    prompt: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 900,
    max_turns: int = 90,
) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        return _json({"error": "prompt is required"})
    try:
        timeout_i = max(1, min(int(timeout_seconds), 3600))
    except Exception:
        return _json({"error": "timeout_seconds must be an integer"})
    try:
        max_turns_i = max(1, min(int(max_turns), 200))
    except Exception:
        return _json({"error": "max_turns must be an integer"})

    run_cwd, cwd_error = _convert_cwd(cwd)
    if cwd_error:
        return _json({"error": cwd_error, "cwd": cwd})

    if not HERMES_EXE.exists():
        return _json({"error": "native Windows Hermes executable not found", "hermes_exe": str(HERMES_EXE)})

    args = [
        str(HERMES_EXE),
        "chat",
        "--query", prompt,
        "--quiet",
        "--source", "mcp-windows-proxy",
        "--accept-hooks",
        "--max-turns", str(max_turns_i),
    ]
    result = _run_hidden(args, cwd=run_cwd, timeout_seconds=timeout_i)
    result.update({
        "cwd": str(run_cwd),
        "timeout_seconds": timeout_i,
        "max_turns": max_turns_i,
    })
    return _json(result)


def add_windows_proxy_tools(mcp):
    @mcp.tool()
    def bridge_agent_status() -> str:
        """Report native bridge agent readiness and gateway status."""
        return _status_payload()

    @mcp.tool()
    def bridge_agent_delegate(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: int = 90,
    ) -> str:
        """Delegate a task prompt to the native bridge agent.

        The task runs through the upstream native agent, not a raw shell proxy.
        The upstream agent uses its normal tool and approval policy. Use this
        when a container-hosted MCP client needs native Windows filesystem,
        process, desktop, credential, or host integration access.
        """
        return _delegate_payload(prompt, cwd, timeout_seconds, max_turns)

    @mcp.tool()
    def windows_agent_status() -> str:
        """Compatibility alias for bridge_agent_status."""
        return _status_payload()

    @mcp.tool()
    def windows_agent_delegate(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: int = 90,
    ) -> str:
        """Compatibility alias for bridge_agent_delegate."""
        return _delegate_payload(prompt, cwd, timeout_seconds, max_turns)


def main() -> None:
    bridge = EventBridge()
    bridge.start()
    server = create_mcp_server(event_bridge=bridge)
    add_windows_proxy_tools(server)

    async def _run() -> None:
        try:
            await server.run_stdio_async()
        finally:
            bridge.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
