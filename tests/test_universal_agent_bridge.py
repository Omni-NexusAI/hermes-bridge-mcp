import json
import sys
import threading
import time
from pathlib import Path

import pytest


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from agent_bridge_universal import UniversalAdapterError, UniversalAgentRegistry


def _registry(tmp_path, monkeypatch, command_probe=lambda command: "mcp-server"):
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "1")
    monkeypatch.setenv("AGENT_BRIDGE_STUB_DELEGATE", "1")
    monkeypatch.setenv("AGENT_BRIDGE_CODEX_EXE", "codex-test")
    return UniversalAgentRegistry(
        tmp_path / "state",
        tmp_path / "hermes-test",
        command_probe=command_probe,
    )


def _write_agents(registry, agents):
    registry.config_file.parent.mkdir(parents=True, exist_ok=True)
    registry.config_file.write_text(
        json.dumps({"schema_version": 1, "agents": agents}),
        encoding="utf-8",
    )


def test_builtin_manifests_auto_detect_hermes_and_modern_codex(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)

    agents = {item["agent"]: item for item in registry.public_agents()}

    assert agents["hermes"]["available"] is True
    assert agents["codex"]["available"] is True
    assert agents["codex"]["adapter_kind"] == "mcp_stdio"
    assert registry.manifests()["codex"]["command"] == ["codex-test", "mcp-server"]


def test_codex_legacy_mcp_subcommand_is_normalized(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, command_probe=lambda command: "commands: mcp")

    assert registry.manifests()["codex"]["command"] == ["codex-test", "mcp"]


def test_cli_adapter_uses_argument_arrays_without_shell_injection(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    _write_agents(
        registry,
        [
            {
                "id": "safe-cli",
                "kind": "cli",
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1])",
                    "{prompt}",
                ],
            }
        ],
    )
    prompt = '"; echo SHOULD_NOT_EXECUTE; "'

    result = registry.execute(
        "safe-cli",
        prompt,
        tmp_path,
        5,
        "local",
        "injection-test",
        30,
        threading.Event(),
    )

    assert result["status"] == "completed"
    assert result["stdout"].strip() == prompt
    assert "SHOULD_NOT_EXECUTE" in result["stdout"]


def test_sessions_are_scoped_by_caller_agent_and_conversation(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    _write_agents(
        registry,
        [
            {
                "id": "session-cli",
                "kind": "cli",
                "command": [
                    sys.executable,
                    "-c",
                    (
                        "import sys; print('ARGS=' + '|'.join(sys.argv[1:])); "
                        "print('session_id: session-123')"
                    ),
                    "{prompt}",
                ],
                "resume_args": ["--resume", "{session_id}"],
                "session_regex": r"session_id:\s*([A-Za-z0-9_.:-]+)",
            }
        ],
    )

    first = registry.execute(
        "session-cli", "first", tmp_path, 5, "peer-a", "work", 30, threading.Event()
    )
    resumed = registry.execute(
        "session-cli", "second", tmp_path, 5, "peer-a", "work", 30, threading.Event()
    )
    isolated = registry.execute(
        "session-cli", "third", tmp_path, 5, "peer-b", "work", 30, threading.Event()
    )

    assert first["resumed_session"] is False
    assert "--resume|session-123" in resumed["stdout"]
    assert resumed["resumed_session"] is True
    assert "--resume" not in isolated["stdout"]
    assert isolated["resumed_session"] is False


def test_manifest_rejects_non_loopback_mcp_and_unknown_placeholders(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    _write_agents(
        registry,
        [
            {
                "id": "unsafe-http",
                "kind": "mcp_http",
                "url": "https://192.0.2.20/mcp",
                "tools": {"start": "delegate"},
            }
        ],
    )
    with pytest.raises(UniversalAdapterError, match="loopback"):
        registry.public_agents()

    _write_agents(
        registry,
        [
            {
                "id": "unsafe-cli",
                "kind": "cli",
                "command": ["agent", "{remote_supplied_executable}"],
            }
        ],
    )
    with pytest.raises(UniversalAdapterError, match="unsupported placeholders"):
        registry.public_agents()

    _write_agents(
        registry,
        [
            {
                "id": "malformed-mcp",
                "kind": "mcp_stdio",
                "command": ["agent"],
                "tools": {"status": "agent_status"},
            }
        ],
    )
    with pytest.raises(UniversalAdapterError, match="start tool mapping"):
        registry.public_agents()


def test_unknown_and_disabled_agents_return_agent_unavailable(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    _write_agents(
        registry,
        [
            {
                "id": "disabled",
                "kind": "cli",
                "enabled": False,
                "command": ["disabled-agent", "{prompt}"],
            }
        ],
    )

    for agent in ("unknown", "disabled"):
        with pytest.raises(UniversalAdapterError) as error:
            registry.execute(
                agent, "hello", tmp_path, 5, "local", "work", 30, threading.Event()
            )
        assert error.value.code == "agent_unavailable"


def test_cli_adapter_redacts_host_configured_secrets(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    monkeypatch.setenv("AGENT_TEST_SECRET", "do-not-return-this-value")
    _write_agents(
        registry,
        [
            {
                "id": "redacting-cli",
                "kind": "cli",
                "command": [
                    sys.executable,
                    "-c",
                    "import os; print(os.environ['AGENT_TEST_SECRET'])",
                ],
                "secret_env": ["AGENT_TEST_SECRET"],
            }
        ],
    )

    result = registry.execute(
        "redacting-cli", "hello", tmp_path, 5, "local", "redact", 30, threading.Event()
    )

    assert result["stdout"].strip() == "[REDACTED]"
    assert "do-not-return-this-value" not in json.dumps(result)


def test_cli_adapter_cancellation_is_cooperative(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    _write_agents(
        registry,
        [
            {
                "id": "slow-cli",
                "kind": "cli",
                "command": [
                    sys.executable,
                    "-c",
                    "import time; time.sleep(30)",
                ],
            }
        ],
    )
    cancel = threading.Event()
    result_box = {}

    def run():
        result_box["result"] = registry.execute(
            "slow-cli", "hello", tmp_path, 5, "local", "cancel", 60, cancel
        )

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.3)
    cancel.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result_box["result"]["status"] == "canceled"


def test_forget_peer_removes_only_that_callers_universal_sessions(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    registry.sessions_file.parent.mkdir(parents=True, exist_ok=True)
    registry.sessions_file.write_text(
        json.dumps(
            {
                "peer-a|codex|work": {"session_id": "a"},
                "peer-b|codex|work": {"session_id": "b"},
                "local|codex|work": {"session_id": "local"},
            }
        ),
        encoding="utf-8",
    )

    assert registry.forget_peer("peer-a", dry_run=True) == 1
    assert registry.forget_peer("peer-a") == 1
    remaining = json.loads(registry.sessions_file.read_text(encoding="utf-8"))
    assert set(remaining) == {"peer-b|codex|work", "local|codex|work"}


def test_native_universal_agent_v1_mapping_polls_and_returns_result(
    tmp_path, monkeypatch
):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    server = tmp_path / "fake_native_agent.py"
    server.write_text(
        """
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fake-native-agent")
@mcp.tool()
def universal_agent_capabilities():
    return {"contract": "universal_agent_v1"}
@mcp.tool()
def universal_agent_delegate_start(prompt: str):
    return {"task_id": "remote-task"}
@mcp.tool()
def universal_agent_delegate_status(task_id: str):
    return {"status": "completed"}
@mcp.tool()
def universal_agent_delegate_result(task_id: str):
    return {"status": "completed", "stdout": "NATIVE_OK"}
@mcp.tool()
def universal_agent_delegate_cancel(task_id: str):
    return {"status": "canceled"}
mcp.run()
""".strip(),
        encoding="utf-8",
    )
    _write_agents(
        registry,
        [
            {
                "id": "native-test",
                "kind": "native_mcp",
                "command": [sys.executable, str(server)],
                "poll_interval": 0.1,
            }
        ],
    )

    result = registry.execute(
        "native-test", "hello", tmp_path, 5, "peer-a", "native", 30, threading.Event()
    )

    assert result["status"] == "completed"
    assert result["stdout"] == "NATIVE_OK"
    assert result["remote_task_id"] == "remote-task"


@pytest.mark.parametrize(
    ("session_key", "session_input"),
    [("threadId", "threadId"), ("sessionId", "sessionId")],
)
def test_codex_mcp_session_identifier_variants(
    tmp_path, monkeypatch, session_key, session_input
):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENT_BRIDGE_TEST_SANDBOX", "0")
    server = tmp_path / f"fake_codex_{session_key}.py"
    server.write_text(
        f"""
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("fake-codex")
@mcp.tool()
def codex(prompt: str, cwd: str = ""):
    return {{"{session_key}": "codex-session", "content": "FIRST_OK"}}
@mcp.tool()
def codex_reply(prompt: str, {session_input}: str):
    return {{"{session_key}": {session_input}, "content": "REPLY_OK"}}
mcp.run()
""".strip(),
        encoding="utf-8",
    )
    _write_agents(
        registry,
        [
            {
                "id": "codex-test",
                "kind": "mcp_stdio",
                "command": [sys.executable, str(server)],
                "mode": "synchronous",
                "tools": {"start": "codex", "continue": "codex_reply"},
                "fields": {
                    "prompt": "prompt",
                    "cwd": "cwd",
                    "session_input_candidates": ["threadId", "sessionId"],
                    "session_output_candidates": ["threadId", "sessionId"],
                },
            }
        ],
    )

    first = registry.execute(
        "codex-test", "first", tmp_path, 5, "local", "codex", 30, threading.Event()
    )
    second = registry.execute(
        "codex-test", "second", tmp_path, 5, "local", "codex", 30, threading.Event()
    )

    assert first["stdout"] == "FIRST_OK"
    assert first["session_id"] == "codex-session"
    assert second["stdout"] == "REPLY_OK"
    assert second["resumed_session"] is True
