from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

def _default_hermes_home() -> Path:
    explicit = os.environ.get("HERMES_BRIDGE_HOME") or os.environ.get("HERMES_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
    return Path.home() / ".hermes"


def _default_hermes_agent(home: Path) -> Path:
    explicit = os.environ.get("HERMES_AGENT_HOME")
    if explicit:
        return Path(explicit).expanduser()
    return home / "hermes-agent"


def _default_hermes_exe(home: Path, agent: Path) -> Path:
    explicit = os.environ.get("HERMES_EXE")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return agent / "venv" / "Scripts" / "hermes.exe"
    for candidate in (
        agent / "venv" / "bin" / "hermes",
        home / "venv" / "bin" / "hermes",
    ):
        if candidate.exists():
            return candidate
    found = shutil.which("hermes")
    return Path(found) if found else agent / "venv" / "bin" / "hermes"


HERMES_HOME = _default_hermes_home()
HERMES_AGENT = _default_hermes_agent(HERMES_HOME)
HERMES_EXE = _default_hermes_exe(HERMES_HOME, HERMES_AGENT)
DEFAULT_CWD = Path.home()
BRIDGE_STATE_DIR = Path(os.environ.get("HERMES_BRIDGE_STATE_DIR", str(HERMES_HOME / "bridge-state"))).expanduser()
BRIDGE_STATE_FILE = Path(
    os.environ.get(
        "HERMES_BRIDGE_STATE_FILE",
        str(BRIDGE_STATE_DIR / ("windows-hermes-proxy-state.json" if os.name == "nt" else "hermes-bridge-state.json")),
    )
).expanduser()
PEER_CONFIG_FILE = Path(os.environ.get("HERMES_BRIDGE_PEERS_CONFIG", str(BRIDGE_STATE_DIR / "peers.json"))).expanduser()
LOCAL_PEER_ID = os.environ.get("HERMES_BRIDGE_PEER_ID") or f"{platform.node() or 'hermes'}-{platform.system().lower() or 'peer'}"
TASK_RETENTION_SECONDS = 24 * 60 * 60
SESSION_ID_RE = re.compile(r"session_id:\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE)

_STATE_LOCK = threading.RLock()
_TASKS: dict[str, dict[str, Any]] = {}
_PROCS: dict[str, subprocess.Popen] = {}

import httpx  # noqa: E402
from mcp import ClientSession  # noqa: E402
try:  # noqa: E402
    from mcp.client.streamable_http import streamable_http_client as _streamable_http_client  # noqa: E402
except ImportError:  # pragma: no cover
    from mcp.client.streamable_http import streamablehttp_client as _streamable_http_client  # type: ignore # noqa: E402
from mcp.server.auth.provider import AccessToken, TokenVerifier  # noqa: E402
from mcp.server.auth.settings import AuthSettings  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402


class _SharedTokenVerifier(TokenVerifier):
    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if token != self._token:
            return None
        return AccessToken(token=token, client_id="hermes-peer", scopes=["hermes-bridge"])


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _parse_json_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


def _tail(text: str, max_chars: int = 8000) -> str:
    if not text:
        return ""
    return text[-max_chars:]


def _now() -> float:
    return time.time()


def _load_state() -> dict:
    try:
        raw = BRIDGE_STATE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("sessions", {})
            data.setdefault("tasks", {})
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {"sessions": {}, "tasks": {}}


def _save_state(data: dict) -> None:
    BRIDGE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BRIDGE_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(BRIDGE_STATE_FILE)


def _prune_old_tasks(data: dict) -> None:
    cutoff = _now() - TASK_RETENTION_SECONDS
    tasks = data.setdefault("tasks", {})
    for task_id, record in list(tasks.items()):
        finished_at = record.get("finished_at")
        if finished_at and float(finished_at) < cutoff:
            tasks.pop(task_id, None)


def _public_task_record(record: dict) -> dict:
    public = dict(record)
    public.pop("args", None)
    public.pop("prompt", None)
    public.pop("cwd_path", None)
    public["stdout"] = _tail(str(public.get("stdout", "")))
    public["stderr_tail"] = _tail(str(public.get("stderr_tail", "")))
    return public


def _persist_task(record: dict) -> None:
    with _STATE_LOCK:
        data = _load_state()
        _prune_old_tasks(data)
        data.setdefault("tasks", {})[record["task_id"]] = _public_task_record(record)
        _save_state(data)


def _session_record(a0_thread_key: str) -> Optional[dict]:
    if not a0_thread_key:
        return None
    with _STATE_LOCK:
        data = _load_state()
        record = data.setdefault("sessions", {}).get(a0_thread_key)
        return dict(record) if isinstance(record, dict) else None


def _update_session_record(a0_thread_key: str, session_id: str, cwd: Optional[Path]) -> None:
    if not a0_thread_key or not session_id:
        return
    with _STATE_LOCK:
        data = _load_state()
        data.setdefault("sessions", {})[a0_thread_key] = {
            "session_id": session_id,
            "updated_at": _now(),
            "cwd": str(cwd) if cwd else "",
            "source": "mcp-windows-proxy",
        }
        _save_state(data)


def _derive_thread_key(cwd: Optional[Path], caller: Optional[str]) -> str:
    raw = f"{caller or 'a0'}|{str(cwd) if cwd else str(DEFAULT_CWD)}"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"derived:{digest}"


def _coerce_int_value(value: Any, name: str, default: int, minimum: int, maximum: int) -> tuple[Optional[int], Optional[str]]:
    try:
        coerced = int(value)
    except Exception:
        return None, f"{name} must be an integer"
    return max(minimum, min(coerced, maximum)), None


def _extract_session_id(stdout: str, stderr: str) -> Optional[str]:
    combined = f"{stderr}\n{stdout}"
    match = SESSION_ID_RE.search(combined)
    return match.group(1).strip() if match else None


def _delegate_runner_available() -> bool:
    if HERMES_EXE.exists():
        return True
    return bool(shutil.which(str(HERMES_EXE)))


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


def _start_hidden(args: list[str], cwd: Optional[Path]) -> subprocess.Popen:
    creationflags = 0
    startupinfo = None
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0

    return subprocess.Popen(
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


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
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


def _build_delegate_args(prompt: str, max_turns: int, session_id: Optional[str]) -> list[str]:
    args = [
        str(HERMES_EXE),
        "chat",
        "--query", prompt,
        "--quiet",
        "--source", "mcp-windows-proxy",
        "--accept-hooks",
        "--pass-session-id",
        "--max-turns", str(max_turns),
    ]
    if session_id:
        args.extend(["--resume", session_id])
    return args


def _prepare_delegate(
    prompt: str,
    cwd: Optional[str],
    max_turns: Any,
    a0_thread_key: Optional[str],
    caller: Optional[str],
) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(prompt, str) or not prompt.strip():
        return None, "prompt is required"
    max_turns_i, err = _coerce_int_value(max_turns, "max_turns", 90, 1, 200)
    if err:
        return None, err
    run_cwd, cwd_error = _convert_cwd(cwd)
    if cwd_error:
        return None, cwd_error
    if not _delegate_runner_available():
        return None, f"Hermes executable not found: {HERMES_EXE}"

    thread_key = str(a0_thread_key or "").strip() or _derive_thread_key(run_cwd, caller)
    existing = _session_record(thread_key)
    session_id = existing.get("session_id") if existing else None
    args = _build_delegate_args(prompt.strip(), max_turns_i or 90, session_id)
    return {
        "prompt": prompt.strip(),
        "cwd": run_cwd,
        "max_turns": max_turns_i,
        "a0_thread_key": thread_key,
        "resumed_session_id": session_id,
        "args": args,
    }, None


def _complete_task(task_id: str, proc: subprocess.Popen, timeout_seconds: int) -> None:
    with _STATE_LOCK:
        record = _TASKS.get(task_id)
    if not record:
        return

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(proc)
        stdout, stderr = proc.communicate(timeout=10)

    finished = _now()
    session_id = _extract_session_id(stdout or "", stderr or "")
    if session_id:
        _update_session_record(record.get("a0_thread_key", ""), session_id, record.get("cwd_path"))

    status = "timed_out" if timed_out else ("completed" if proc.returncode == 0 else "failed")
    with _STATE_LOCK:
        current = _TASKS.get(task_id, record)
        if current.get("status") == "canceled":
            status = "canceled"
        current.update({
            "status": status,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "elapsed_ms": int((finished - float(current["started_at"])) * 1000),
            "finished_at": finished,
            "stdout": _tail(stdout or ""),
            "stderr_tail": _tail(stderr or ""),
            "session_id": session_id or current.get("resumed_session_id"),
        })
        _TASKS[task_id] = current
        _PROCS.pop(task_id, None)
        public = _public_task_record(current)
    _persist_task(public)


def _start_delegate_task(prepared: dict, timeout_seconds: int) -> dict:
    task_id = uuid.uuid4().hex
    proc = _start_hidden(prepared["args"], prepared["cwd"])
    record = {
        "task_id": task_id,
        "status": "running",
        "pid": proc.pid,
        "started_at": _now(),
        "finished_at": None,
        "elapsed_ms": 0,
        "exit_code": None,
        "timed_out": False,
        "stdout": "",
        "stderr_tail": "",
        "cwd": str(prepared["cwd"]),
        "cwd_path": prepared["cwd"],
        "timeout_seconds": timeout_seconds,
        "max_turns": prepared["max_turns"],
        "a0_thread_key": prepared["a0_thread_key"],
        "resumed_session_id": prepared.get("resumed_session_id"),
        "session_id": prepared.get("resumed_session_id"),
        "args": prepared["args"],
        "prompt": prepared["prompt"],
    }
    with _STATE_LOCK:
        _TASKS[task_id] = record
        _PROCS[task_id] = proc
    _persist_task(record)
    thread = threading.Thread(target=_complete_task, args=(task_id, proc, timeout_seconds), daemon=True)
    thread.start()
    return _public_task_record(record)


def _task_status(task_id: str) -> Optional[dict]:
    with _STATE_LOCK:
        record = _TASKS.get(task_id)
    if record:
        proc = _PROCS.get(task_id)
        public = _public_task_record(record)
        if proc and proc.poll() is None:
            public["elapsed_ms"] = int((_now() - float(record["started_at"])) * 1000)
        return public
    data = _load_state()
    saved = data.get("tasks", {}).get(task_id)
    return dict(saved) if isinstance(saved, dict) else None


def _load_peer_config(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    config_path = path or PEER_CONFIG_FILE
    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ValueError(f"failed to load peer config {config_path}: {exc}") from exc

    peers_raw = data.get("peers", data) if isinstance(data, dict) else data
    peers: dict[str, dict[str, Any]] = {}
    if isinstance(peers_raw, dict):
        iterable = []
        for peer_id, record in peers_raw.items():
            if isinstance(record, dict):
                item = dict(record)
                item.setdefault("peer_id", peer_id)
                iterable.append(item)
    elif isinstance(peers_raw, list):
        iterable = peers_raw
    else:
        raise ValueError("peer config must be a JSON object or list")

    for item in iterable:
        if not isinstance(item, dict):
            continue
        peer_id = str(item.get("peer_id") or item.get("id") or "").strip()
        url = str(item.get("url") or "").strip()
        if not peer_id or not url:
            continue
        token = item.get("token")
        token_env = item.get("token_env")
        if token_env and not token:
            token = os.environ.get(str(token_env))
        peers[peer_id] = {
            "peer_id": peer_id,
            "url": url,
            "platform": str(item.get("platform") or "unknown"),
            "token": token,
            "token_env": token_env,
        }
    return peers


def _get_peer(peer_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if not isinstance(peer_id, str) or not peer_id.strip():
        return None, "peer_id is required"
    try:
        peers = _load_peer_config()
    except ValueError as exc:
        return None, str(exc)
    peer = peers.get(peer_id.strip())
    if not peer:
        return None, f"peer not found: {peer_id}"
    if not peer.get("token"):
        return None, f"peer token is missing for {peer_id}; set token_env or token in {PEER_CONFIG_FILE}"
    return peer, None


def _configured_peer_ids() -> list[str]:
    try:
        return sorted(_load_peer_config().keys())
    except Exception:
        return []


def _peer_thread_key(remote_peer_id: str, conversation_key: Optional[str]) -> str:
    raw = str(conversation_key or "default").strip() or "default"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"peer:{LOCAL_PEER_ID}:to:{remote_peer_id}:{digest}"


async def _call_peer_tool(peer: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> Any:
    headers = {"Authorization": f"Bearer {peer['token']}"}
    async with httpx.AsyncClient(headers=headers, timeout=None) as client:
        async with _streamable_http_client(peer["url"], http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if not result.content:
                    return None
                return _parse_json_text(result.content[0].text)


def _run_coroutine_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _peer_result(peer: dict[str, Any], remote: Any) -> dict[str, Any]:
    if isinstance(remote, dict):
        data = dict(remote)
    else:
        data = {"remote_result": remote}
    data.setdefault("peer_id", peer["peer_id"])
    data.setdefault("peer_url", peer["url"])
    data.setdefault("peer_platform", peer.get("platform", "unknown"))
    return data


def _peer_call(peer_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    peer, err = _get_peer(peer_id)
    if err:
        return {"error": err, "peer_id": peer_id}
    try:
        remote = _run_coroutine_sync(_call_peer_tool(peer, tool_name, arguments))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "peer_id": peer_id, "peer_url": peer["url"]}
    return _peer_result(peer, remote)


def _peer_delegate_start(
    peer_id: str,
    prompt: str,
    cwd: Optional[str] = None,
    timeout_seconds: int = 3600,
    max_turns: int = 90,
    conversation_key: Optional[str] = None,
) -> dict[str, Any]:
    thread_key = _peer_thread_key(peer_id, conversation_key)
    return _peer_call(peer_id, "bridge_agent_delegate_start", {
        "prompt": prompt,
        "cwd": cwd,
        "timeout_seconds": timeout_seconds,
        "max_turns": max_turns,
        "a0_thread_key": thread_key,
        "caller": LOCAL_PEER_ID,
    })


def add_windows_proxy_tools(mcp):
    @mcp.tool()
    def windows_agent_status() -> str:
        """Report native Windows Hermes proxy readiness.

        Direct delegation does not require Telegram or Hermes Gateway.
        """
        version = _run_hidden([str(HERMES_EXE), "--version"], cwd=HERMES_AGENT, timeout_seconds=30)
        config_path = _run_hidden([str(HERMES_EXE), "config", "path"], cwd=HERMES_AGENT, timeout_seconds=30)
        state = _load_state()
        return _json({
            "hermes_exe": str(HERMES_EXE),
            "hermes_home": str(HERMES_HOME),
            "hermes_agent": str(HERMES_AGENT),
            "bridge_state_file": str(BRIDGE_STATE_FILE),
            "peer_config_file": str(PEER_CONFIG_FILE),
            "local_peer_id": LOCAL_PEER_ID,
            "configured_peers": _configured_peer_ids(),
            "platform": platform.system().lower() or "unknown",
            "default_cwd": str(DEFAULT_CWD),
            "delegate_runner_available": _delegate_runner_available(),
            "delegate_requires_telegram_gateway": False,
            "delegate_transport": "local hermes chat subprocess via Hermes bridge",
            "tracked_a0_threads": len(state.get("sessions", {})),
            "tracked_tasks": len(state.get("tasks", {})),
            "version": version,
            "config_path": config_path,
        })

    @mcp.tool()
    def windows_agent_delegate(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: int = 90,
        a0_thread_key: Optional[str] = None,
        caller: Optional[str] = None,
        kill_on_timeout: bool = False,
    ) -> str:
        """Delegate a task prompt to the native Windows Hermes agent.

        The task runs through Windows Hermes itself, not a raw shell proxy. The
        Windows agent uses its normal tool and approval policy. Use this when a
        Docker-hosted Hermes agent needs native Windows filesystem, process,
        desktop, credential, or host integration access.

        For long-running work, this compatibility wrapper starts a background
        task and waits up to timeout_seconds. If the task is still running, it
        returns a task_id instead of killing Hermes unless kill_on_timeout is
        true.
        """
        timeout_i, err = _coerce_int_value(timeout_seconds, "timeout_seconds", 900, 1, 3600)
        if err:
            return _json({"error": err})
        prepared, prep_error = _prepare_delegate(prompt, cwd, max_turns, a0_thread_key, caller)
        if prep_error:
            return _json({"error": prep_error, "cwd": cwd})

        task = _start_delegate_task(prepared, timeout_i or 900)
        task_id = task["task_id"]
        deadline = time.monotonic() + (timeout_i or 900)
        while time.monotonic() < deadline:
            status = _task_status(task_id)
            if status and status.get("status") in {"completed", "failed", "timed_out", "canceled"}:
                return _json(status)
            time.sleep(0.25)

        if kill_on_timeout:
            proc = _PROCS.get(task_id)
            if proc:
                with _STATE_LOCK:
                    record = _TASKS.get(task_id, {})
                    record["status"] = "timed_out"
                    record["timed_out"] = True
                    _TASKS[task_id] = record
                _terminate_process_tree(proc)

        status = _task_status(task_id) or task
        status.update({
            "status": "still_running" if not kill_on_timeout else status.get("status", "timed_out"),
            "message": "Delegation is still running. Poll windows_agent_delegate_status with task_id.",
        })
        return _json(status)

    @mcp.tool()
    def windows_agent_delegate_start(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 3600,
        max_turns: int = 90,
        a0_thread_key: Optional[str] = None,
        caller: Optional[str] = None,
    ) -> str:
        """Start a native Windows Hermes delegation and return a pollable task_id."""
        timeout_i, err = _coerce_int_value(timeout_seconds, "timeout_seconds", 3600, 1, 24 * 3600)
        if err:
            return _json({"error": err})
        prepared, prep_error = _prepare_delegate(prompt, cwd, max_turns, a0_thread_key, caller)
        if prep_error:
            return _json({"error": prep_error, "cwd": cwd})
        task = _start_delegate_task(prepared, timeout_i or 3600)
        task.update({
            "poll_after_seconds": 5,
            "message": "Delegation started. Poll windows_agent_delegate_status or windows_agent_delegate_result with task_id.",
        })
        return _json(task)

    @mcp.tool()
    def windows_agent_delegate_status(task_id: str) -> str:
        """Return status for a background Hermes delegation task."""
        if not isinstance(task_id, str) or not task_id.strip():
            return _json({"error": "task_id is required"})
        status = _task_status(task_id.strip())
        if not status:
            return _json({"error": f"task not found: {task_id}"})
        return _json(status)

    @mcp.tool()
    def windows_agent_delegate_result(task_id: str) -> str:
        """Return final output for a delegation task, or latest status if still running."""
        if not isinstance(task_id, str) or not task_id.strip():
            return _json({"error": "task_id is required"})
        status = _task_status(task_id.strip())
        if not status:
            return _json({"error": f"task not found: {task_id}"})
        if status.get("status") not in {"completed", "failed", "timed_out", "canceled"}:
            status["message"] = "Delegation is still running. Poll again later."
        return _json(status)

    @mcp.tool()
    def windows_agent_delegate_cancel(task_id: str) -> str:
        """Cancel a running Hermes delegation task."""
        if not isinstance(task_id, str) or not task_id.strip():
            return _json({"error": "task_id is required"})
        task_id = task_id.strip()
        proc = _PROCS.get(task_id)
        status = _task_status(task_id)
        if not status:
            return _json({"error": f"task not found: {task_id}"})
        if not proc or proc.poll() is not None:
            return _json(status)
        _terminate_process_tree(proc)
        with _STATE_LOCK:
            record = _TASKS.get(task_id, {})
            record.update({
                "status": "canceled",
                "finished_at": _now(),
                "elapsed_ms": int((_now() - float(record.get("started_at", _now()))) * 1000),
            })
            _TASKS[task_id] = record
        _persist_task(record)
        return _json(_public_task_record(record))

    @mcp.tool()
    def bridge_peer_status(peer_id: str) -> str:
        """Return status from a configured remote Hermes bridge peer."""
        return _json(_peer_call(peer_id, "bridge_agent_status", {}))

    @mcp.tool()
    def bridge_peer_delegate_start(
        peer_id: str,
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 3600,
        max_turns: int = 90,
        conversation_key: Optional[str] = None,
    ) -> str:
        """Start a pollable delegation on a configured remote Hermes peer."""
        return _json(_peer_delegate_start(peer_id, prompt, cwd, timeout_seconds, max_turns, conversation_key))

    @mcp.tool()
    def bridge_peer_delegate_status(peer_id: str, task_id: str) -> str:
        """Return status for a remote Hermes peer delegation task."""
        return _json(_peer_call(peer_id, "bridge_agent_delegate_status", {"task_id": task_id}))

    @mcp.tool()
    def bridge_peer_delegate_result(peer_id: str, task_id: str) -> str:
        """Return final output for a remote Hermes peer delegation task."""
        return _json(_peer_call(peer_id, "bridge_agent_delegate_result", {"task_id": task_id}))

    @mcp.tool()
    def bridge_peer_delegate_cancel(peer_id: str, task_id: str) -> str:
        """Cancel a running remote Hermes peer delegation task."""
        return _json(_peer_call(peer_id, "bridge_agent_delegate_cancel", {"task_id": task_id}))

    @mcp.tool()
    def bridge_agent_status() -> str:
        """Alias for windows_agent_status. Does not require Telegram."""
        return windows_agent_status()

    @mcp.tool()
    def bridge_agent_delegate(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 900,
        max_turns: int = 90,
        a0_thread_key: Optional[str] = None,
        caller: Optional[str] = None,
        kill_on_timeout: bool = False,
    ) -> str:
        """Alias for windows_agent_delegate. Use this for direct A0-to-Hermes work."""
        return windows_agent_delegate(prompt, cwd, timeout_seconds, max_turns, a0_thread_key, caller, kill_on_timeout)

    @mcp.tool()
    def bridge_agent_delegate_start(
        prompt: str,
        cwd: Optional[str] = None,
        timeout_seconds: int = 3600,
        max_turns: int = 90,
        a0_thread_key: Optional[str] = None,
        caller: Optional[str] = None,
    ) -> str:
        """Alias for windows_agent_delegate_start. Start a pollable direct Hermes task."""
        return windows_agent_delegate_start(prompt, cwd, timeout_seconds, max_turns, a0_thread_key, caller)

    @mcp.tool()
    def bridge_agent_delegate_status(task_id: str) -> str:
        """Alias for windows_agent_delegate_status."""
        return windows_agent_delegate_status(task_id)

    @mcp.tool()
    def bridge_agent_delegate_result(task_id: str) -> str:
        """Alias for windows_agent_delegate_result."""
        return windows_agent_delegate_result(task_id)

    @mcp.tool()
    def bridge_agent_delegate_cancel(task_id: str) -> str:
        """Alias for windows_agent_delegate_cancel."""
        return windows_agent_delegate_cancel(task_id)


def _is_loopback_host(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in {"127.0.0.1", "localhost", "::1"} or value.startswith("127.")


def _validate_http_auth(host: str, auth_token: Optional[str], allow_unsafe_lan: bool) -> Optional[str]:
    if _is_loopback_host(host):
        return None
    if auth_token:
        return None
    if allow_unsafe_lan:
        return None
    return "LAN-facing peer bridge requires HERMES_BRIDGE_AUTH_TOKEN or --auth-token"


def _create_delegate_only_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: Optional[str] = None,
) -> "FastMCP":
    token_verifier = _SharedTokenVerifier(auth_token) if auth_token else None
    auth = None
    if auth_token:
        resource_url = f"http://localhost:{port}"
        auth = AuthSettings(issuer_url=resource_url, resource_server_url=resource_url, required_scopes=["hermes-bridge"])
    return FastMCP(
        "hermes-bridge",
        instructions=(
            "Direct A0/Agentspine to native Windows Hermes bridge. Use "
            "bridge_agent_delegate_start/status/result/cancel, or "
            "bridge_agent_delegate for short tasks. Use bridge_peer_* tools "
            "for authenticated Hermes-to-Hermes network delegation. This "
            "bridge does not require Telegram, Discord, Slack, WhatsApp, or "
            "Hermes Gateway."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        token_verifier=token_verifier,
        auth=auth,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes Bridge MCP")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default=os.environ.get("HERMES_BRIDGE_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("HERMES_BRIDGE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HERMES_BRIDGE_PORT", "8000")))
    parser.add_argument("--auth-token", default=os.environ.get("HERMES_BRIDGE_AUTH_TOKEN"))
    parser.add_argument("--allow-unsafe-lan", action="store_true", default=os.environ.get("HERMES_BRIDGE_ALLOW_UNSAFE_LAN") == "1")
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    if args.transport == "streamable-http":
        auth_error = _validate_http_auth(args.host, args.auth_token, args.allow_unsafe_lan)
        if auth_error:
            raise SystemExit(auth_error)

    server = _create_delegate_only_server(args.host, args.port, args.auth_token)
    add_windows_proxy_tools(server)

    async def _run() -> None:
        if args.transport == "streamable-http":
            await server.run_streamable_http_async()
        else:
            await server.run_stdio_async()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
