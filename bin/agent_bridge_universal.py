"""Universal agent adapters for Agent Bridge MCP.

Adapters are host-configured. Remote callers may select an enabled agent but
cannot provide executables, credentials, model overrides, or sandbox policy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse


AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
TERMINAL_STATUSES = {"completed", "failed", "timed_out", "canceled"}
ALLOWED_PLACEHOLDERS = {"prompt", "cwd", "max_turns", "session_id"}
DEFAULT_NATIVE_TOOLS = {
    "capabilities": "universal_agent_capabilities",
    "start": "universal_agent_delegate_start",
    "status": "universal_agent_delegate_status",
    "result": "universal_agent_delegate_result",
    "cancel": "universal_agent_delegate_cancel",
}


class UniversalAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(f"AGENT_BRIDGE_{name}", os.environ.get(f"HERMES_BRIDGE_{name}", default))


def _tail(value: str, limit: int = 8000) -> str:
    return str(value or "")[-limit:]


def _redact(value: str, secrets: list[str]) -> str:
    redacted = str(value or "")
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _safe_command(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise UniversalAdapterError("invalid_manifest", f"{field} must be a non-empty argument array")
    command = []
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096:
            raise UniversalAdapterError("invalid_manifest", f"{field} contains an invalid argument")
        command.append(item)
    return command


def _placeholders(value: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z0-9_]+)\}", value))


def _validate_template(command: list[str], field: str) -> None:
    for item in command:
        unknown = _placeholders(item) - ALLOWED_PLACEHOLDERS
        if unknown:
            raise UniversalAdapterError(
                "invalid_manifest",
                f"{field} contains unsupported placeholders: {', '.join(sorted(unknown))}",
            )


def _format_command(command: list[str], values: dict[str, Any]) -> list[str]:
    rendered = []
    for item in command:
        rendered.append(item.format(**{key: str(values.get(key, "")) for key in ALLOWED_PLACEHOLDERS}))
    return rendered


def _parse_tool_result(result: Any) -> tuple[Any, str]:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    texts = [
        str(getattr(item, "text"))
        for item in (getattr(result, "content", None) or [])
        if getattr(item, "text", None) is not None
    ]
    text = "\n".join(texts)
    if structured is None and text:
        try:
            structured = json.loads(text)
        except Exception:
            structured = None
    return structured, text


def _session_from_payload(payload: Any, keys: list[str]) -> Optional[str]:
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value and SESSION_ID_RE.fullmatch(str(value)):
                return str(value)
        for value in payload.values():
            found = _session_from_payload(value, keys)
            if found:
                return found
    return None


def _task_id_from_payload(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("task_id", "taskId", "id"):
        value = payload.get(key)
        if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", str(value)):
            return str(value)
    return None


class UniversalAgentRegistry:
    """Loads host-owned manifests and executes enabled adapters."""

    def __init__(
        self,
        state_dir: Path,
        hermes_executable: Path,
        command_probe: Optional[Callable[[list[str]], str]] = None,
    ):
        self.state_dir = Path(state_dir)
        self.hermes_executable = Path(hermes_executable)
        self.sessions_file = self.state_dir / "universal-sessions.json"
        self.config_file = Path(
            _env("AGENTS_CONFIG", str(self.state_dir / "agents.json")) or self.state_dir / "agents.json"
        ).expanduser()
        self.command_probe = command_probe or self._default_command_probe
        self._lock = threading.RLock()

    @staticmethod
    def _default_command_probe(command: list[str]) -> str:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            return result.stdout or ""
        except Exception:
            return ""

    def _sandbox_stub(self) -> bool:
        return _env("TEST_SANDBOX") == "1" and _env("STUB_DELEGATE") == "1"

    def _codex_manifest(self) -> dict[str, Any]:
        executable = _env("CODEX_EXE") or shutil.which("codex")
        if not executable:
            return {
                "id": "codex",
                "kind": "mcp_stdio",
                "enabled": True,
                "available": False,
                "reason": "Codex executable was not found",
                "builtin": True,
            }
        help_text = self.command_probe([str(executable), "--help"])
        subcommand = "mcp-server" if "mcp-server" in help_text else "mcp"
        return {
            "id": "codex",
            "display_name": "Codex",
            "kind": "mcp_stdio",
            "enabled": True,
            "available": True,
            "builtin": True,
            "command": [str(executable), subcommand],
            "mode": "synchronous",
            "tools": {"start": "codex", "continue": "codex-reply"},
            "fields": {
                "prompt": "prompt",
                "cwd": "cwd",
                "session_input_candidates": ["threadId", "sessionId", "conversationId"],
                "session_output_candidates": ["threadId", "sessionId", "conversationId"],
            },
            "capabilities": {
                "contract": "universal_agent_v1",
                "persistent_conversations": True,
                "cancel": False,
            },
        }

    def _hermes_manifest(self) -> dict[str, Any]:
        available = self._sandbox_stub() or self.hermes_executable.exists() or bool(
            shutil.which(str(self.hermes_executable))
        )
        return {
            "id": "hermes",
            "display_name": "Hermes",
            "kind": "cli",
            "enabled": True,
            "available": available,
            "reason": "" if available else "Hermes executable was not found",
            "builtin": True,
            "command": [
                str(self.hermes_executable),
                "chat",
                "--query",
                "{prompt}",
                "--quiet",
                "--source",
                "mcp-agent-bridge",
                "--accept-hooks",
                "--pass-session-id",
                "--max-turns",
                "{max_turns}",
            ],
            "resume_args": ["--resume", "{session_id}"],
            "session_regex": r"session_id:\s*([A-Za-z0-9_.:-]+)",
            "capabilities": {
                "contract": "universal_agent_v1",
                "persistent_conversations": True,
                "cancel": True,
            },
        }

    def _load_declared(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(self.config_file.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except Exception as exc:
            raise UniversalAdapterError("invalid_manifest", f"unable to read agents config: {exc}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise UniversalAdapterError("invalid_manifest", "agents config requires schema_version 1")
        agents = document.get("agents", [])
        if not isinstance(agents, list):
            raise UniversalAdapterError("invalid_manifest", "agents must be an array")
        return [self._validate_manifest(item) for item in agents]

    def _validate_manifest(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise UniversalAdapterError("invalid_manifest", "each agent manifest must be an object")
        manifest = dict(value)
        agent_id = str(manifest.get("id") or "").lower()
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise UniversalAdapterError("invalid_manifest", "agent id is invalid")
        kind = str(manifest.get("kind") or "")
        if kind == "native_mcp":
            kind = "mcp_http" if manifest.get("url") else "mcp_stdio"
            manifest.setdefault("mode", "polling")
            manifest.setdefault("tools", dict(DEFAULT_NATIVE_TOOLS))
        if kind not in {"cli", "mcp_stdio", "mcp_http"}:
            raise UniversalAdapterError("invalid_manifest", f"unsupported adapter kind for {agent_id}")
        manifest["id"] = agent_id
        manifest["kind"] = kind
        manifest["enabled"] = bool(manifest.get("enabled", True))
        manifest["available"] = manifest["enabled"]
        manifest["builtin"] = False
        if kind in {"cli", "mcp_stdio"}:
            manifest["command"] = _safe_command(manifest.get("command"), "command")
            _validate_template(manifest["command"], "command")
        if kind == "cli":
            resume_args = manifest.get("resume_args", [])
            manifest["resume_args"] = _safe_command(resume_args, "resume_args") if resume_args else []
            _validate_template(manifest["resume_args"], "resume_args")
            regex = str(manifest.get("session_regex") or "")
            if regex:
                try:
                    re.compile(regex)
                except re.error as exc:
                    raise UniversalAdapterError("invalid_manifest", "session_regex is invalid") from exc
        if kind == "mcp_http":
            parsed = urlparse(str(manifest.get("url") or ""))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise UniversalAdapterError("invalid_manifest", "mcp_http adapter requires an HTTP(S) URL")
            if not bool(manifest.get("allow_remote_endpoint")):
                host = parsed.hostname.lower()
                if host not in {"localhost", "127.0.0.1", "::1"} and not host.startswith("127."):
                    raise UniversalAdapterError(
                        "invalid_manifest",
                        "mcp_http endpoint must be loopback unless allow_remote_endpoint is host-configured",
                    )
        if kind.startswith("mcp_"):
            tools = manifest.get("tools")
            if not isinstance(tools, dict) or not tools.get("start"):
                raise UniversalAdapterError("invalid_manifest", "MCP adapter requires a start tool mapping")
            manifest["tools"] = {str(key): str(item) for key, item in tools.items() if item}
            fields = manifest.get("fields", {})
            if not isinstance(fields, dict):
                raise UniversalAdapterError("invalid_manifest", "MCP fields mapping must be an object")
            manifest["fields"] = fields
        capabilities = manifest.get("capabilities", {})
        manifest["capabilities"] = capabilities if isinstance(capabilities, dict) else {}
        return manifest

    def manifests(self) -> dict[str, dict[str, Any]]:
        manifests = {"hermes": self._hermes_manifest(), "codex": self._codex_manifest()}
        for declared in self._load_declared():
            agent_id = declared["id"]
            if agent_id in manifests and not bool(declared.get("replace_builtin")):
                raise UniversalAdapterError(
                    "invalid_manifest",
                    f"{agent_id} is reserved; set replace_builtin in the host-owned manifest to override it",
                )
            manifests[agent_id] = declared
        return manifests

    def public_agents(self) -> list[dict[str, Any]]:
        result = []
        for manifest in self.manifests().values():
            result.append(
                {
                    "agent": manifest["id"],
                    "display_name": manifest.get("display_name") or manifest["id"],
                    "adapter_kind": manifest["kind"],
                    "enabled": bool(manifest.get("enabled")),
                    "available": bool(manifest.get("available")),
                    "reason": manifest.get("reason") or "",
                    "builtin": bool(manifest.get("builtin")),
                    "capabilities": manifest.get("capabilities", {}),
                }
            )
        return sorted(result, key=lambda item: item["agent"])

    def _session_key(self, caller_peer: str, agent: str, conversation_key: str) -> str:
        caller = str(caller_peer or "local").strip().lower()
        conversation = str(conversation_key or "default").strip()
        if not AGENT_ID_RE.fullmatch(agent):
            raise UniversalAdapterError("agent_unavailable", "agent identifier is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", caller):
            caller = "local"
        if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,256}", conversation):
            raise UniversalAdapterError("invalid_request", "conversation_key is invalid")
        return f"{caller}|{agent}|{conversation}"

    def _load_sessions(self) -> dict[str, Any]:
        try:
            document = json.loads(self.sessions_file.read_text(encoding="utf-8"))
            return document if isinstance(document, dict) else {}
        except Exception:
            return {}

    def _session(self, key: str) -> Optional[str]:
        with self._lock:
            record = self._load_sessions().get(key)
            value = record.get("session_id") if isinstance(record, dict) else None
            return str(value) if value and SESSION_ID_RE.fullmatch(str(value)) else None

    def _save_session(self, key: str, session_id: Optional[str], cwd: Optional[Path]) -> None:
        if not session_id or not SESSION_ID_RE.fullmatch(str(session_id)):
            return
        with self._lock:
            document = self._load_sessions()
            document[key] = {
                "session_id": str(session_id),
                "cwd": str(cwd or ""),
                "updated_at": time.time(),
            }
            _atomic_json_write(self.sessions_file, document)

    def forget_peer(self, peer_id: str, dry_run: bool = False) -> int:
        prefix = f"{str(peer_id).strip().lower()}|"
        with self._lock:
            document = self._load_sessions()
            matches = [key for key in document if key.startswith(prefix)]
            if matches and not dry_run:
                for key in matches:
                    document.pop(key, None)
                _atomic_json_write(self.sessions_file, document)
            return len(matches)

    def execute(
        self,
        agent: str,
        prompt: str,
        cwd: Optional[Path],
        max_turns: int,
        caller_peer: str,
        conversation_key: str,
        hard_timeout_seconds: int,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        agent = str(agent or "").strip().lower()
        manifest = self.manifests().get(agent)
        if not manifest or not manifest.get("enabled") or not manifest.get("available"):
            raise UniversalAdapterError("agent_unavailable", f"agent is not available: {agent}")
        session_key = self._session_key(caller_peer, agent, conversation_key)
        session_id = self._session(session_key)
        if manifest["kind"] == "cli":
            result = self._execute_cli(
                manifest,
                prompt,
                cwd,
                max_turns,
                session_id,
                hard_timeout_seconds,
                cancel_event,
            )
        else:
            result = asyncio.run(
                self._execute_mcp(
                    manifest,
                    prompt,
                    cwd,
                    session_id,
                    hard_timeout_seconds,
                    cancel_event,
                )
            )
        self._save_session(session_key, result.get("session_id"), cwd)
        result["agent"] = agent
        result["adapter_kind"] = manifest["kind"]
        result["conversation_key"] = conversation_key
        result["resumed_session"] = bool(session_id)
        return result

    def _execute_cli(
        self,
        manifest: dict[str, Any],
        prompt: str,
        cwd: Optional[Path],
        max_turns: int,
        session_id: Optional[str],
        hard_timeout_seconds: int,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        if self._sandbox_stub():
            return {
                "status": "completed",
                "exit_code": 0,
                "stdout": "SANDBOX_UNIVERSAL_OK",
                "stderr_tail": "",
                "session_id": session_id or f"sandbox-{manifest['id']}-session",
            }
        values = {
            "prompt": prompt,
            "cwd": str(cwd or ""),
            "max_turns": max_turns,
            "session_id": session_id or "",
        }
        command = _format_command(manifest["command"], values)
        if session_id:
            command.extend(_format_command(manifest.get("resume_args", []), values))
        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        deadline = time.monotonic() + hard_timeout_seconds
        status = "running"
        while proc.poll() is None:
            if cancel_event.wait(0.1):
                status = "canceled"
                self._terminate(proc)
                break
            if time.monotonic() >= deadline:
                status = "timed_out"
                self._terminate(proc)
                break
        stdout, stderr = proc.communicate(timeout=10)
        if status == "running":
            status = "completed" if proc.returncode == 0 else "failed"
        secrets = [
            os.environ.get(str(name), "")
            for name in manifest.get("secret_env", [])
            if isinstance(name, str)
        ]
        session_match = None
        regex = str(manifest.get("session_regex") or "")
        if regex:
            session_match = re.search(regex, f"{stderr}\n{stdout}", re.IGNORECASE)
        return {
            "status": status,
            "exit_code": proc.returncode,
            "stdout": _tail(_redact(stdout, secrets)),
            "stderr_tail": _tail(_redact(stderr, secrets)),
            "session_id": session_match.group(1) if session_match else session_id,
        }

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
            proc.wait(timeout=2)
        if proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
                timeout=5,
            )
        else:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            with contextlib.suppress(Exception):
                proc.kill()

    async def _execute_mcp(
        self,
        manifest: dict[str, Any],
        prompt: str,
        cwd: Optional[Path],
        session_id: Optional[str],
        hard_timeout_seconds: int,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        return await asyncio.wait_for(
            self._execute_mcp_connected(manifest, prompt, cwd, session_id, cancel_event),
            timeout=max(1, hard_timeout_seconds),
        )

    async def _execute_mcp_connected(
        self,
        manifest: dict[str, Any],
        prompt: str,
        cwd: Optional[Path],
        session_id: Optional[str],
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        fields = manifest.get("fields", {})
        tools = manifest["tools"]
        prompt_field = str(fields.get("prompt") or "prompt")
        cwd_field = str(fields.get("cwd") or "cwd")
        session_inputs = fields.get(
            "session_input_candidates", ["threadId", "sessionId", "conversationId", "session_id"]
        )
        session_outputs = fields.get(
            "session_output_candidates", ["threadId", "sessionId", "conversationId", "session_id"]
        )
        secret_values: list[str] = []

        async def use_session(session: ClientSession) -> dict[str, Any]:
            await session.initialize()
            listed = await session.list_tools()
            schemas = {tool.name: tool.inputSchema for tool in listed.tools}
            capability_tool = tools.get("capabilities")
            if capability_tool:
                if capability_tool not in schemas:
                    raise UniversalAdapterError(
                        "extension_unsupported",
                        f"configured capability tool is unavailable: {capability_tool}",
                    )
                capability_result = await session.call_tool(capability_tool, {})
                capability_payload, capability_text = _parse_tool_result(
                    capability_result
                )
                contract = (
                    capability_payload.get("contract")
                    if isinstance(capability_payload, dict)
                    else None
                )
                if contract != "universal_agent_v1":
                    raise UniversalAdapterError(
                        "extension_unsupported",
                        _redact(
                            capability_text
                            or "native MCP adapter does not advertise universal_agent_v1",
                            secret_values,
                        ),
                    )
            tool_name = tools.get("continue") if session_id and tools.get("continue") else tools["start"]
            schema = schemas.get(tool_name, {})
            if tool_name not in schemas:
                raise UniversalAdapterError(
                    "extension_unsupported", f"configured MCP tool is unavailable: {tool_name}"
                )
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            arguments: dict[str, Any] = {prompt_field: prompt}
            if cwd and cwd_field in properties:
                arguments[cwd_field] = str(cwd)
            if session_id:
                selected = next((name for name in session_inputs if name in properties), None)
                if not selected:
                    raise UniversalAdapterError(
                        "extension_unsupported",
                        f"{tool_name} does not accept a compatible conversation identifier",
                    )
                arguments[selected] = session_id
            result = await session.call_tool(tool_name, arguments)
            if getattr(result, "isError", False) or getattr(result, "is_error", False):
                _, text = _parse_tool_result(result)
                raise UniversalAdapterError("agent_error", _redact(text or "agent tool failed", secret_values))
            structured, text = _parse_tool_result(result)
            new_session = _session_from_payload(structured, list(session_outputs))
            if manifest.get("mode", "synchronous") != "polling":
                output = (
                    structured.get("content")
                    if isinstance(structured, dict) and isinstance(structured.get("content"), str)
                    else text
                )
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "stdout": _tail(_redact(str(output or ""), secret_values)),
                    "stderr_tail": "",
                    "session_id": new_session or session_id,
                }

            remote_task_id = _task_id_from_payload(structured)
            if not remote_task_id:
                raise UniversalAdapterError("agent_error", "polling adapter did not return a task id")
            while True:
                if cancel_event.wait(0):
                    if tools.get("cancel"):
                        await session.call_tool(
                            tools["cancel"],
                            {str(fields.get("task_id") or "task_id"): remote_task_id},
                        )
                    return {
                        "status": "canceled",
                        "exit_code": None,
                        "stdout": "",
                        "stderr_tail": "",
                        "session_id": new_session or session_id,
                    }
                await asyncio.sleep(max(0.1, min(float(manifest.get("poll_interval", 1)), 30.0)))
                status_result = await session.call_tool(
                    tools["status"],
                    {str(fields.get("task_id") or "task_id"): remote_task_id},
                )
                status_payload, status_text = _parse_tool_result(status_result)
                status = (
                    str(status_payload.get("status") or "")
                    if isinstance(status_payload, dict)
                    else ""
                ).lower()
                if status in TERMINAL_STATUSES:
                    final_payload, final_text = status_payload, status_text
                    if tools.get("result"):
                        final_result = await session.call_tool(
                            tools["result"],
                            {str(fields.get("task_id") or "task_id"): remote_task_id},
                        )
                        final_payload, final_text = _parse_tool_result(final_result)
                    output = (
                        final_payload.get("stdout")
                        if isinstance(final_payload, dict)
                        else final_text
                    )
                    return {
                        "status": status,
                        "exit_code": 0 if status == "completed" else None,
                        "stdout": _tail(_redact(str(output or final_text or ""), secret_values)),
                        "stderr_tail": "",
                        "session_id": new_session or session_id,
                        "remote_task_id": remote_task_id,
                    }

        if manifest["kind"] == "mcp_stdio":
            env = None
            configured_env = manifest.get("env", {})
            if configured_env:
                if not isinstance(configured_env, dict):
                    raise UniversalAdapterError("invalid_manifest", "stdio env must be an object")
                env = dict(os.environ)
                env.update({str(key): str(value) for key, value in configured_env.items()})
                secret_values.extend(str(value) for value in configured_env.values())
            command = manifest["command"]
            parameters = StdioServerParameters(
                command=command[0],
                args=command[1:],
                env=env,
                cwd=str(cwd) if cwd else None,
            )
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    return await use_session(session)

        import httpx
        try:
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:  # pragma: no cover
            from mcp.client.streamable_http import streamablehttp_client as streamable_http_client
        headers = {}
        for header, env_name in manifest.get("headers_env", {}).items():
            value = os.environ.get(str(env_name), "")
            if value:
                headers[str(header)] = value
                secret_values.append(value)
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            async with streamable_http_client(manifest["url"], http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    return await use_session(session)
