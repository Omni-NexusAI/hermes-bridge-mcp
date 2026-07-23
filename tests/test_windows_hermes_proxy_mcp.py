import importlib.util
import asyncio
import json
import sys
import types
import time
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


def load_a0_config_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "configure-a0-mcp.py"
    spec = importlib.util.spec_from_file_location("configure_a0_mcp_test", path)
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


def test_a0_config_helper_uses_universal_bridge_settings():
    module = load_a0_config_module()
    settings = {}

    updated, changed = module.configure(settings, "hermes-bridge", "http://host.docker.internal:18082/mcp")
    servers = json.loads(updated["mcp_servers"])["mcpServers"]
    entry = servers["hermes-bridge"]

    assert changed is True
    assert entry["type"] == "streamable-http"
    assert entry["url"] == "http://host.docker.internal:18082/mcp"
    assert entry["timeout"] == 900
    assert entry["tool_timeout"] == 900
    assert "messenger gateway" in entry["description"]
    assert "windows-hermes" not in servers

    updated, _ = module.configure({}, "hermes-bridge", module.DEFAULT_URL, "secret-token")
    authenticated = json.loads(updated["mcp_servers"])["mcpServers"]["hermes-bridge"]
    assert authenticated["headers"]["Authorization"] == "Bearer secret-token"


def test_messaging_gateway_artifacts_are_not_shipped():
    root = Path(__file__).resolve().parents[1]
    forbidden = [
        root / "bin" / "start-windows-hermes-gateway.ps1",
        root / "bin" / "windows-hermes-gateway-background-watchdog.ps1",
        root / "startup" / "Watch Windows Hermes Gateway.vbs",
    ]

    assert [path for path in forbidden if path.exists()] == []


def test_local_windows_launcher_forces_http_even_when_discovery_is_enabled():
    launcher = Path(__file__).resolve().parents[1] / "bin" / "start-windows-hermes-bridge.ps1"
    source = launcher.read_text(encoding="utf-8")
    assert '$env:HERMES_BRIDGE_SECURE_NETWORK = "0"' in source
    assert '"--stateless-http"' in source


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


def test_adaptive_timeout_helper_extends_long_tasks():
    module = load_proxy_module()

    shape = module._estimate_task_shape(
        "Implement and test a deep research workflow with multiple validation passes.",
        max_turns=120,
        requested_wait_seconds=5,
    )

    assert shape["likely_long"] is True
    assert shape["hard_timeout_seconds"] > 5
    assert shape["recommended_poll_seconds"] >= 15


def test_start_delegate_task_separates_wait_timeout_from_hard_deadline(monkeypatch, tmp_path):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_DIR", tmp_path / "bridge-state")
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "bridge-state" / "state.json")

    class FakeProc:
        pid = 1234

        def poll(self):
            return None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(module, "_start_hidden", lambda args, cwd: FakeProc())
    monkeypatch.setattr(module.threading, "Thread", FakeThread)

    task = module._start_delegate_task(
        {
            "args": ["fake-hermes"],
            "cwd": tmp_path,
            "prompt": "Implement a long task and report later.",
            "max_turns": 100,
            "a0_thread_key": "thread-1",
            "resumed_session_id": None,
        },
        hard_timeout_seconds=7200,
        wait_timeout_seconds=1,
    )

    assert task["timeout_seconds"] == 1
    assert task["wait_timeout_seconds"] == 1
    assert task["hard_timeout_seconds"] == 7200
    assert task["hard_deadline_at"] > task["started_at"]
    assert task["remaining_timeout_seconds"] > 0
    assert task["status_tool"] == "bridge_agent_delegate_status"
    assert task["result_tool"] == "bridge_agent_delegate_result"


def test_task_status_adds_polling_guidance(monkeypatch):
    module = load_proxy_module()
    task_id = "task-guidance"
    monkeypatch.setattr(module, "_TASKS", {
        task_id: {
            "task_id": task_id,
            "status": "running",
            "started_at": time.time() - 2,
            "updated_at": time.time() - 2,
            "hard_timeout_seconds": 3600,
            "hard_deadline_at": time.time() + 3598,
            "recommended_poll_seconds": 15,
            "estimated_remaining_seconds": 600,
            "stdout": "",
            "stderr_tail": "",
        }
    })
    monkeypatch.setattr(module, "_PROCS", {})

    status = module._task_status(task_id)

    assert status["poll_after_seconds"] == 15
    assert status["remaining_timeout_seconds"] > 0
    assert status["estimated_remaining_seconds"] <= 600
    assert "Poll bridge_agent_delegate_status" in status["next_action"]


def test_delegate_only_server_does_not_expose_messaging_tools():
    module = load_proxy_module()
    server = module._create_delegate_only_server()
    module.add_bridge_tools(server)

    async def collect_names():
        tools = await server.list_tools()
        return {tool.name for tool in tools}

    names = asyncio.run(collect_names())

    assert set(module.DEFAULT_PUBLIC_TOOLS).issubset(names)
    assert len(module.DEFAULT_PUBLIC_TOOLS) == 11
    assert set(module.NETWORK_EXTENSION_TOOLS).issubset(names)
    assert set(module.UNIVERSAL_EXTENSION_TOOLS).issubset(names)
    assert "messages_send" not in names
    assert "conversations_list" not in names
    assert "bridge_agent_delegate_start" in names
    assert "windows_agent_delegate_start" not in names
    assert "bridge_peer_delegate_start" in names
    assert "bridge_peer_status" in names


def test_v127_core_tool_argument_contract_is_unchanged():
    module = load_proxy_module()
    server = module._create_delegate_only_server()
    module.add_bridge_tools(server)

    async def collect_schemas():
        tools = await server.list_tools()
        return {tool.name: tool.inputSchema for tool in tools}

    schemas = asyncio.run(collect_schemas())
    expected_properties = {
        "bridge_agent_status": set(),
        "bridge_agent_delegate": {"prompt", "cwd", "timeout_seconds", "max_turns", "a0_thread_key", "caller", "kill_on_timeout", "hard_timeout_seconds"},
        "bridge_agent_delegate_start": {"prompt", "cwd", "timeout_seconds", "max_turns", "a0_thread_key", "caller", "hard_timeout_seconds"},
        "bridge_agent_delegate_status": {"task_id"},
        "bridge_agent_delegate_result": {"task_id"},
        "bridge_agent_delegate_cancel": {"task_id"},
        "bridge_peer_status": {"peer_id"},
        "bridge_peer_delegate_start": {"peer_id", "prompt", "cwd", "timeout_seconds", "max_turns", "conversation_key", "hard_timeout_seconds"},
        "bridge_peer_delegate_status": {"peer_id", "task_id"},
        "bridge_peer_delegate_result": {"peer_id", "task_id"},
        "bridge_peer_delegate_cancel": {"peer_id", "task_id"},
    }
    for tool_name, properties in expected_properties.items():
        assert set(schemas[tool_name].get("properties", {})) == properties

    universal_properties = {
        "bridge_agent_universal_list": set(),
        "bridge_agent_universal_delegate_start": {
            "agent",
            "prompt",
            "cwd",
            "timeout_seconds",
            "max_turns",
            "conversation_key",
            "hard_timeout_seconds",
        },
        "bridge_peer_universal_list": {"peer_id"},
        "bridge_peer_universal_delegate_start": {
            "peer_id",
            "agent",
            "prompt",
            "cwd",
            "timeout_seconds",
            "max_turns",
            "conversation_key",
            "hard_timeout_seconds",
        },
    }
    for tool_name, properties in universal_properties.items():
        assert set(schemas[tool_name].get("properties", {})) == properties


def test_tool_descriptions_explain_local_vs_network_routing():
    module = load_proxy_module()
    server = module._create_delegate_only_server()
    module.add_bridge_tools(server)

    async def collect_descriptions():
        tools = await server.list_tools()
        return {tool.name: tool.description for tool in tools}

    descriptions = asyncio.run(collect_descriptions())

    assert "Local bridge only" in descriptions["bridge_agent_delegate_start"]
    assert "Do not use this for another device" in descriptions["bridge_agent_delegate"]
    assert "Network peer only" in descriptions["bridge_peer_delegate_start"]
    assert "Requires peer_id" in descriptions["bridge_peer_delegate_start"]


def test_server_instructions_explain_routing_rule():
    module = load_proxy_module()
    server = module._create_delegate_only_server()

    instructions = server._mcp_server.instructions

    assert "Use bridge_agent_* only for the local Hermes agent" in instructions
    assert "Never use bridge_agent_* to reach another machine" in instructions
    assert "Use bridge_peer_* with peer_id" in instructions
    assert "call bridge_agent_status first" in instructions


def test_legacy_windows_tools_are_opt_in(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setenv("HERMES_BRIDGE_ENABLE_LEGACY_WINDOWS_TOOLS", "1")
    server = module._create_delegate_only_server()
    module.add_bridge_tools(server)

    async def collect_names():
        tools = await server.list_tools()
        return {tool.name for tool in tools}

    names = asyncio.run(collect_names())

    assert "bridge_agent_delegate_start" in names
    assert "windows_agent_delegate_start" in names
    assert "windows_agent_status" in names


def test_bridge_agent_status_reports_bridge_version(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_run_hidden", lambda *args, **kwargs: "hermes-runtime")
    monkeypatch.setattr(module, "_load_state", lambda: {"sessions": {}, "tasks": {}})
    monkeypatch.setattr(module, "_peer_diagnostics", lambda: [
        {
            "peer_id": "quest3",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token_configured": True,
            "pair_key_env": "HERMES_BRIDGE_PAIR_KEY",
            "token_env": "",
        }
    ])
    server = module._create_delegate_only_server()
    module.add_bridge_tools(server)

    async def call_status():
        content, _ = await server.call_tool("bridge_agent_status", {})
        return json.loads(content[0].text)

    status = asyncio.run(call_status())

    assert module.BRIDGE_VERSION == "v1.3.5"
    assert module.MIN_COMPATIBLE_BRIDGE_VERSION == "v1.2.7"
    assert status["bridge_version"] == "v1.3.5"
    assert status["min_compatible_bridge_version"] == "v1.2.7"
    assert "Versions >= v1.2.7" in status["compatibility_policy"]
    assert status["hermes_version"] == "hermes-runtime"
    assert "version" not in status
    assert status["configured_peers"] == ["quest3"]
    assert status["peer_tools_available"] is True
    assert status["peers"][0]["token_configured"] is True
    assert "token" not in status["peers"][0]
    assert status["tool_routing"]["local_tools"] == "bridge_agent_*"
    assert status["tool_routing"]["network_peer_tools"] == "bridge_peer_*"
    assert "Use bridge_agent_* only for the local Hermes agent" in status["tool_routing"]["rule"]
    assert status["public_tool_contract"]["default_tool_count"] == 11
    assert status["public_tool_contract"]["default_tools"] == list(module.DEFAULT_PUBLIC_TOOLS)
    assert status["public_tool_contract"]["stable_since"] == "v1.2.7"


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


def test_peer_config_accepts_utf8_bom(tmp_path):
    module = load_proxy_module()
    config = tmp_path / "peers.json"
    config.write_text(
        '{"peers": [{"peer_id": "desktop", "url": "http://192.168.0.2:18084/mcp", "pair_key": "test"}]}',
        encoding="utf-8-sig",
    )

    peers = module._parse_peer_config(config)

    assert peers["desktop"]["url"] == "http://192.168.0.2:18084/mcp"


def test_managed_pair_overrides_colliding_legacy_peer(tmp_path, monkeypatch):
    module = load_proxy_module()
    config = tmp_path / "peers.json"
    config.write_text(
        '{"peers": [{"peer_id": "desktop", "url": "http://192.168.0.2:18084/mcp", "pair_key": "legacy"}]}',
        encoding="utf-8",
    )

    class ManagedPeers:
        def managed_peer_config(self):
            return {
                "desktop": {
                    "peer_id": "desktop",
                    "url": "https://192.168.0.2:18443/mcp",
                    "managed": True,
                }
            }

    monkeypatch.setattr(module, "_network_manager", lambda: ManagedPeers())

    peers = module._load_peer_config(config)

    assert peers["desktop"]["url"] == "http://192.168.0.2:18084/mcp"
    # The no-path call is the runtime resolution path and merges managed state.
    monkeypatch.setattr(module, "_peer_config_candidates", lambda path=None: [config])
    peers = module._load_peer_config()
    assert peers["desktop"]["url"] == "https://192.168.0.2:18443/mcp"
    assert peers["desktop"]["managed"] is True


def test_peer_config_prefers_pair_key_env(tmp_path, monkeypatch):
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
              "pair_key_env": "HERMES_PAIR_QUEST3",
              "token_env": "OLD_QUEST_TOKEN"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_PAIR_QUEST3", "pair-secret")
    monkeypatch.setenv("OLD_QUEST_TOKEN", "legacy-secret")

    peers = module._load_peer_config(config)

    assert peers["quest3"]["token"] == "pair-secret"
    assert peers["quest3"]["pair_key_env"] == "HERMES_PAIR_QUEST3"


def test_peer_config_uses_default_pair_key_for_multiple_peers(tmp_path, monkeypatch):
    module = load_proxy_module()
    config = tmp_path / "peers.json"
    config.write_text(
        """
        {
          "peers": [
            {"peer_id": "quest3", "url": "http://10.0.0.42:18084/mcp"},
            {"peer_id": "laptop", "url": "http://10.0.0.43:18084/mcp"}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_BRIDGE_PAIR_KEY", "shared-pair-secret")

    peers = module._load_peer_config(config)

    assert peers["quest3"]["token"] == "shared-pair-secret"
    assert peers["laptop"]["token"] == "shared-pair-secret"


def test_peer_config_supports_distinct_per_peer_pair_keys(tmp_path, monkeypatch):
    module = load_proxy_module()
    config = tmp_path / "peers.json"
    config.write_text(
        """
        {
          "peers": [
            {"peer_id": "quest3", "url": "http://10.0.0.42:18084/mcp", "pair_key_env": "PAIR_QUEST"},
            {"peer_id": "laptop", "url": "http://10.0.0.43:18084/mcp", "pair_key_env": "PAIR_LAPTOP"}
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("PAIR_QUEST", "quest-secret")
    monkeypatch.setenv("PAIR_LAPTOP", "laptop-secret")

    peers = module._load_peer_config(config)

    assert peers["quest3"]["token"] == "quest-secret"
    assert peers["laptop"]["token"] == "laptop-secret"


def test_peer_config_candidates_include_android_shared_storage(tmp_path):
    module = load_proxy_module()
    primary = tmp_path / "peers.json"

    assert module._peer_config_candidates(primary)[0] == primary

    candidates = [candidate.as_posix() for candidate in module._peer_config_candidates(primary, os_name="posix")]

    assert "/sdcard/Download/hermes-q3-peers.json" in candidates
    assert "/storage/self/primary/Download/hermes-q3-peers.json" in candidates


def test_lan_peer_http_requires_token():
    module = load_proxy_module()

    assert module._validate_http_auth("0.0.0.0", None, False)
    assert module._validate_http_auth("0.0.0.0", "secret", False) is None
    assert module._validate_http_auth("127.0.0.1", None, False) is None


def test_lan_peer_http_accepts_pair_key(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setenv("HERMES_BRIDGE_PAIR_KEY", "pair-secret")

    assert module._validate_http_auth("0.0.0.0", None, False) is None


def test_shared_token_verifier_accepts_only_configured_tokens():
    module = load_proxy_module()
    verifier = module._SharedTokenVerifier(["legacy-secret", "pair-secret"])

    async def check():
        accepted_legacy = await verifier.verify_token("legacy-secret")
        accepted_pair = await verifier.verify_token("pair-secret")
        rejected = await verifier.verify_token("wrong")
        return accepted_legacy, accepted_pair, rejected

    accepted_legacy, accepted_pair, rejected = asyncio.run(check())

    assert accepted_legacy is not None
    assert accepted_pair is not None
    assert accepted_pair.client_id == "hermes-peer"
    assert rejected is None


def test_create_server_with_auth_token():
    module = load_proxy_module()

    server = module._create_delegate_only_server(host="0.0.0.0", port=18084, auth_token="secret")

    assert server is not None
    assert server.settings.auth is None
    assert server._token_verifier is None
    assert server._bridge_bearer_tokens == ["secret"]


def test_bearer_token_middleware_enforces_shared_token():
    module = load_proxy_module()
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def invoke(headers):
        sent = []
        async def send(message):
            sent.append(message)

        middleware = module._BearerTokenMiddleware(app, ["secret"], "/mcp")
        await middleware(
            {
                "type": "http",
                "path": "/mcp",
                "headers": headers,
            },
            None,
            send,
        )
        return sent

    accepted = asyncio.run(invoke([(b"authorization", b"Bearer secret")]))
    rejected = asyncio.run(invoke([(b"authorization", b"Bearer wrong")]))
    missing = asyncio.run(invoke([]))

    assert accepted[0]["status"] == 200
    assert rejected[0]["status"] == 401
    assert missing[0]["status"] == 401


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
        hard_timeout_seconds=7200,
    )

    assert result["task_id"] == "remote-task"
    peer_id, tool_name, arguments = calls[0]
    assert peer_id == "quest3"
    assert tool_name == "bridge_agent_delegate_start"
    assert not tool_name.startswith("windows_agent_")
    assert arguments["prompt"] == "hello"
    assert arguments["timeout_seconds"] == 120
    assert arguments["hard_timeout_seconds"] == 7200
    assert arguments["max_turns"] == 5
    assert arguments["caller"] == "windows"
    assert arguments["a0_thread_key"].startswith("peer:windows:to:quest3:")


def test_peer_not_found_error_includes_available_peers(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_load_peer_config", lambda: {
        "quest3": {
            "peer_id": "quest3",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token": "secret",
        }
    })

    result = module._peer_call("missing", "bridge_agent_status", {})

    assert result["peer_id"] == "missing"
    assert result["available_peers"] == ["quest3"]
    assert "Call bridge_agent_status" in result["next_action"]


def test_peer_missing_token_error_has_pair_key_guidance(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_load_peer_config", lambda: {
        "quest3": {
            "peer_id": "quest3",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token": "",
        }
    })

    result = module._peer_call("quest3", "bridge_agent_status", {})

    assert result["auth_required"] == "shared_bearer_pair_key"
    assert "HERMES_BRIDGE_PAIR_KEY" in result["accepted_token_sources"]
    assert result["token_configured"] is False


def test_peer_connection_error_includes_recovery_fields(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_load_peer_config", lambda: {
        "quest3": {
            "peer_id": "quest3",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token": "secret",
        }
    })

    async def fail_call(peer, tool_name, arguments):
        raise TimeoutError("offline")

    monkeypatch.setattr(module, "_call_peer_tool", fail_call)
    result = module._peer_call("quest3", "bridge_agent_status", {})

    assert result["error_type"] == "TimeoutError"
    assert result["peer_url"] == "http://192.168.0.72:18084/mcp"
    assert result["peer_platform"] == "android"
    assert result["remote_tool_called"] == "bridge_agent_status"
    assert result["used_tool_family"] == "bridge_peer"
    assert "remote peer bridge is running" in result["next_action"]


def test_peer_success_includes_tool_family_metadata(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_load_peer_config", lambda: {
        "quest3": {
            "peer_id": "quest3",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token": "secret",
        }
    })
    async def ok_call(peer, tool_name, arguments):
        return {"status": "ok"}

    monkeypatch.setattr(module, "_call_peer_tool", ok_call)

    result = module._peer_call("quest3", "bridge_agent_status", {})

    assert result["status"] == "ok"
    assert result["used_tool_family"] == "bridge_peer"
    assert result["remote_tool_called"] == "bridge_agent_status"
    assert result["peer_id"] == "quest3"


def test_peer_universal_rejects_legacy_static_peer(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_load_peer_config", lambda: {
        "legacy": {
            "peer_id": "legacy",
            "url": "http://192.168.0.72:18084/mcp",
            "platform": "android",
            "token": "secret",
        }
    })

    result = module._peer_universal_call(
        "legacy", "bridge_agent_universal_list", {}
    )

    assert result["error"] == "extension_unsupported"
    assert "certificate-pinned" in result["message"]


def test_peer_universal_normalizes_old_managed_peer_tool_error(monkeypatch):
    module = load_proxy_module()
    peer = {
        "peer_id": "managed",
        "url": "https://192.168.0.72:18443/mcp",
        "platform": "windows",
        "token": "secret",
        "managed": True,
        "cert_pem": "certificate",
    }
    monkeypatch.setattr(module, "_load_peer_config", lambda: {"managed": peer})
    monkeypatch.setattr(
        module,
        "_peer_call",
        lambda peer_id, tool_name, arguments: {
            "error": "Tool not found: bridge_agent_universal_list"
        },
    )

    result = module._peer_universal_call(
        "managed", "bridge_agent_universal_list", {}
    )

    assert result["error"] == "extension_unsupported"
    assert result["peer_id"] == "managed"
