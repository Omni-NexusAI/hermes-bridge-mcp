from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

HERMES_HOME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
HERMES_AGENT = HERMES_HOME / "hermes-agent"
HERMES_EXE = HERMES_AGENT / "venv" / "Scripts" / "hermes.exe"
HERMES_PYTHON = HERMES_AGENT / "venv" / "Scripts" / "python.exe"
HERMES_CMD = HERMES_HOME / "bin" / "hermes.cmd"
DEFAULT_CWD = Path.home()
DEFAULT_DELEGATE_TOOLSETS = os.environ.get("HERMES_BRIDGE_DEFAULT_TOOLSETS", "terminal,file")
LOG_PATH = HERMES_HOME / "logs" / "windows-hermes-proxy-mcp.jsonl"

if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))

from mcp_serve import EventBridge, create_mcp_server  # noqa: E402


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _tail(text: str, max_chars: int = 8000) -> str:
    if not text:
        return ""
    return text[-max_chars:]


def _windows_exit_code(returncode: Optional[int]) -> Optional[int]:
    if returncode is None:
        return None
    if os.name == "nt" and returncode > 0x7FFFFFFF:
        return returncode - 0x100000000
    return returncode


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("USERPROFILE", str(Path.home()))
    env.setdefault("LOCALAPPDATA", str(HERMES_HOME.parent))
    env.setdefault("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    env.setdefault("HERMES_HOME", str(HERMES_HOME))
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def _append_diag(event: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(event)
        payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _hermes_command() -> tuple[list[str], str]:
    if HERMES_EXE.exists():
        return [str(HERMES_EXE)], "exe"
    if HERMES_PYTHON.exists():
        return [str(HERMES_PYTHON), "-m", "hermes_cli.main"], "python-module"
    if HERMES_CMD.exists():
        return [str(HERMES_CMD)], "cmd-shim"
    path_hermes = shutil.which("hermes")
    if path_hermes:
        return [path_hermes], "path"
    return ["hermes"], "missing"


def _hermes_args(*args: str) -> tuple[list[str], str]:
    command, mode = _hermes_command()
    return [*command, *args], mode


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


def _normalize_toolsets(toolsets: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if toolsets is None:
        return DEFAULT_DELEGATE_TOOLSETS, None

    value = str(toolsets).strip()
    if not value:
        return None, None

    if value.lower() in {"default", "defaults", "config", "configured", "configured-default", "all"}:
        return None, None

    parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if not parts:
        return None, None

    for part in parts:
        if not all(ch.isalnum() or ch in {"-", "_"} for ch in part):
            return None, f"invalid toolset name: {part}"

    return ",".join(parts), None


def _run_hidden(args: list[str], cwd: Optional[Path], timeout_seconds: int, operation_id: Optional[str] = None) -> dict:
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    started = time.monotonic()
    if operation_id:
        _append_diag({
            "operation_id": operation_id,
            "event": "process.start",
            "cwd": str(cwd) if cwd else None,
            "argv_head": args[:4],
            "timeout_seconds": timeout_seconds,
        })
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if operation_id:
            _append_diag({
                "operation_id": operation_id,
                "event": "process.start_failed",
                "elapsed_ms": elapsed_ms,
                "error": f"{type(exc).__name__}: {exc}",
            })
        return {
            "ok": False,
            "exit_code": None,
            "timed_out": False,
            "elapsed_ms": elapsed_ms,
            "stdout": "",
            "stderr_tail": f"{type(exc).__name__}: {exc}",
        }

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
    exit_code = _windows_exit_code(proc.returncode)
    result = {
        "ok": exit_code == 0 and not timed_out,
        "exit_code": _windows_exit_code(proc.returncode),
        "timed_out": timed_out,
        "elapsed_ms": elapsed_ms,
        "stdout": _tail(stdout),
        "stderr_tail": _tail(stderr),
    }
    if operation_id:
        _append_diag({
            "operation_id": operation_id,
            "event": "process.finish",
            "ok": result["ok"],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "elapsed_ms": elapsed_ms,
            "stderr_tail": _tail(stderr, max_chars=2000),
        })
    return result


def _status_payload() -> str:
    operation_id = f"status-{uuid4().hex[:12]}"
    version_args, command_mode = _hermes_args("--version")
    config_args, _ = _hermes_args("config", "path")
    gateway_args, _ = _hermes_args("gateway", "status")
    version = _run_hidden(version_args, cwd=HERMES_AGENT, timeout_seconds=30, operation_id=operation_id)
    config_path = _run_hidden(config_args, cwd=HERMES_AGENT, timeout_seconds=30, operation_id=operation_id)
    gateway = _run_hidden(gateway_args, cwd=HERMES_AGENT, timeout_seconds=60, operation_id=operation_id)
    return _json({
        "operation_id": operation_id,
        "hermes_command": version_args[:2] if command_mode == "python-module" else version_args[:1],
        "hermes_command_mode": command_mode,
        "hermes_home": str(HERMES_HOME),
        "default_cwd": str(DEFAULT_CWD),
        "delegate_runner_available": command_mode != "missing",
        "default_delegate_toolsets": DEFAULT_DELEGATE_TOOLSETS,
        "diagnostics_log": str(LOG_PATH),
        "client_guidance": (
            "Use the MCP client's normal tool call for bridge_agent_delegate. "
            "Do not shell out to Python, curl, or raw HTTP clients unless you are "
            "diagnosing a broken MCP client wrapper."
        ),
        "recommended_client_timeouts": {
            "init_timeout": 30,
            "connect_timeout": 30,
            "tool_timeout": 900,
            "timeout": 900,
        },
        "version": version,
        "config_path": config_path,
        "gateway_status": gateway,
    })


def _delegate_payload(
    prompt: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 900,
    max_turns: int = 90,
    toolsets: Optional[str] = DEFAULT_DELEGATE_TOOLSETS,
) -> str:
    operation_id = f"delegate-{uuid4().hex[:12]}"
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

    toolsets_arg, toolsets_error = _normalize_toolsets(toolsets)
    if toolsets_error:
        return _json({"error": toolsets_error, "toolsets": toolsets})

    command, command_mode = _hermes_command()
    if command_mode == "missing":
        return _json({
            "error": "native Windows Hermes launcher not found",
            "checked": [str(HERMES_EXE), str(HERMES_PYTHON), str(HERMES_CMD)],
        })

    args = [
        *command,
        "chat",
        "--query", prompt,
        "--quiet",
        "--source", "mcp-windows-proxy",
        "--accept-hooks",
        "--max-turns", str(max_turns_i),
    ]
    if toolsets_arg:
        args.extend(["--toolsets", toolsets_arg])

    result = _run_hidden(args, cwd=run_cwd, timeout_seconds=timeout_i, operation_id=operation_id)
    result.update({
        "operation_id": operation_id,
        "cwd": str(run_cwd),
        "timeout_seconds": timeout_i,
        "max_turns": max_turns_i,
        "toolsets": toolsets_arg or "configured-default",
        "hermes_command_mode": command_mode,
        "diagnostics_log": str(LOG_PATH),
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
        toolsets: Optional[str] = DEFAULT_DELEGATE_TOOLSETS,
    ) -> str:
        """Delegate a task prompt to the native bridge agent.

        The task runs through the upstream native agent, not a raw shell proxy.
        The upstream agent uses its normal approval policy. By default the
        bridge uses a focused native toolset so unrelated MCP connectors do not
        slow or fail the delegation path. Pass toolsets="configured-default" or
        toolsets="all" to let Windows Hermes load its configured defaults. This
        tool is intended to be called through the client's normal MCP tool
        interface; raw HTTP/Python callers should be used only for diagnostics.
        """
        return _delegate_payload(prompt, cwd, timeout_seconds, max_turns, toolsets)

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
        toolsets: Optional[str] = DEFAULT_DELEGATE_TOOLSETS,
    ) -> str:
        """Compatibility alias for bridge_agent_delegate."""
        return _delegate_payload(prompt, cwd, timeout_seconds, max_turns, toolsets)


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
