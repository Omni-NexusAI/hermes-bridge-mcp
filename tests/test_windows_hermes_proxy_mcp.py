import importlib.util
import asyncio
import sys
import types
from pathlib import Path


def load_proxy_module():
    fake_mcp = types.ModuleType("mcp_serve")
    fake_mcp.EventBridge = object
    fake_mcp.create_mcp_server = lambda event_bridge=None: object()
    sys.modules.setdefault("mcp_serve", fake_mcp)

    path = Path(__file__).resolve().parents[1] / "bin" / "windows-hermes-proxy-mcp.py"
    spec = importlib.util.spec_from_file_location("windows_hermes_proxy_mcp_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bridge_does_not_import_hermes_messaging_server():
    path = Path(__file__).resolve().parents[1] / "bin" / "windows-hermes-proxy-mcp.py"
    source = path.read_text(encoding="utf-8")

    assert "from mcp_serve import" not in source
    assert "create_mcp_server" not in source
    assert "EventBridge" not in source


def test_extract_session_id_from_quiet_stderr():
    module = load_proxy_module()

    assert module._extract_session_id("final answer", "\nsession_id: 20260601_123456_ab12cd\n") == "20260601_123456_ab12cd"


def test_delegate_args_resume_existing_session():
    module = load_proxy_module()

    args = module._build_delegate_args("hello", 12, "sess_123")

    assert "--pass-session-id" in args
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "sess_123"
    assert args[args.index("--max-turns") + 1] == "12"


def test_prepare_delegate_reuses_thread_session(tmp_path, monkeypatch):
    module = load_proxy_module()
    state_file = tmp_path / "bridge-state" / "state.json"
    hermes_exe = tmp_path / "hermes.exe"
    hermes_exe.write_text("", encoding="utf-8")

    monkeypatch.setattr(module, "BRIDGE_STATE_DIR", state_file.parent)
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", state_file)
    monkeypatch.setattr(module, "HERMES_EXE", hermes_exe)
    module._update_session_record("a0-thread-1", "sess_abc", tmp_path)

    prepared, error = module._prepare_delegate(
        "do work",
        str(tmp_path),
        90,
        "a0-thread-1",
        "agentspine",
    )

    assert error is None
    assert prepared["a0_thread_key"] == "a0-thread-1"
    assert prepared["resumed_session_id"] == "sess_abc"
    assert "--resume" in prepared["args"]


def test_public_task_record_drops_private_fields():
    module = load_proxy_module()

    public = module._public_task_record({
        "task_id": "t1",
        "args": ["secret"],
        "prompt": "hidden",
        "cwd_path": Path("."),
        "stdout": "ok",
        "stderr_tail": "",
    })

    assert "args" not in public
    assert "prompt" not in public
    assert "cwd_path" not in public
    assert public["stdout"] == "ok"


def test_delegate_only_server_does_not_expose_messaging_tools():
    module = load_proxy_module()
    server = module._create_delegate_only_server()
    module.add_windows_proxy_tools(server)

    async def collect_names():
        tools = await server.list_tools()
        return {tool.name for tool in tools}

    names = asyncio.run(collect_names())

    assert "messages_send" not in names
    assert "conversations_list" not in names
    assert "bridge_agent_delegate_start" in names
    assert "windows_agent_delegate_start" in names
    assert "bridge_peer_delegate_start" in names
    assert "bridge_peer_status" in names


def test_cross_platform_home_prefers_env(monkeypatch, tmp_path):
    module = load_proxy_module()
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_BRIDGE_HOME", str(home))

    assert module._default_hermes_home() == home


def test_peer_config_loads_static_peers_and_token_env(tmp_path, monkeypatch):
    module = load_proxy_module()
    config = tmp_path / "peers.json"
    config.write_text(
        """
        {
          "peers": [
            {
              "peer_id": "quest3",
              "url": "http://10.0.0.42:18084/mcp",
              "platform": "android",
              "token_env": "QUEST_TOKEN"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEST_TOKEN", "secret")

    peers = module._load_peer_config(config)

    assert peers["quest3"]["url"] == "http://10.0.0.42:18084/mcp"
    assert peers["quest3"]["platform"] == "android"
    assert peers["quest3"]["token"] == "secret"


def test_lan_peer_http_requires_token():
    module = load_proxy_module()

    assert module._validate_http_auth("0.0.0.0", None, False)
    assert module._validate_http_auth("0.0.0.0", "secret", False) is None
    assert module._validate_http_auth("127.0.0.1", None, False) is None


def test_shared_token_verifier_accepts_only_configured_token():
    module = load_proxy_module()
    verifier = module._SharedTokenVerifier("secret")

    async def check():
        accepted = await verifier.verify_token("secret")
        rejected = await verifier.verify_token("wrong")
        return accepted, rejected

    accepted, rejected = asyncio.run(check())

    assert accepted is not None
    assert accepted.client_id == "hermes-peer"
    assert rejected is None


def test_create_server_with_auth_token():
    module = load_proxy_module()

    server = module._create_delegate_only_server(host="0.0.0.0", port=18084, auth_token="secret")

    assert server is not None


def test_peer_thread_key_is_per_peer_and_conversation(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "LOCAL_PEER_ID", "windows")

    key_a = module._peer_thread_key("quest3", "topic-a")
    key_b = module._peer_thread_key("quest3", "topic-b")
    key_c = module._peer_thread_key("android2", "topic-a")

    assert key_a.startswith("peer:windows:to:quest3:")
    assert key_a != key_b
    assert key_a != key_c


def test_peer_delegate_start_forwards_per_peer_thread_key(monkeypatch):
    module = load_proxy_module()
    calls = []

    def fake_peer_call(peer_id, tool_name, arguments):
        calls.append((peer_id, tool_name, arguments))
        return {"task_id": "remote-task", "status": "running"}

    monkeypatch.setattr(module, "LOCAL_PEER_ID", "windows")
    monkeypatch.setattr(module, "_peer_call", fake_peer_call)

    result = module._peer_delegate_start(
        "quest3",
        "hello",
        timeout_seconds=120,
        max_turns=5,
        conversation_key="shared-topic",
    )

    assert result["task_id"] == "remote-task"
    peer_id, tool_name, arguments = calls[0]
    assert peer_id == "quest3"
    assert tool_name == "bridge_agent_delegate_start"
    assert arguments["prompt"] == "hello"
    assert arguments["timeout_seconds"] == 120
    assert arguments["max_turns"] == 5
    assert arguments["caller"] == "windows"
    assert arguments["a0_thread_key"].startswith("peer:windows:to:quest3:")
