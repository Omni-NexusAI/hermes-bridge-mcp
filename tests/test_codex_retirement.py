import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

from test_windows_hermes_proxy_mcp import load_proxy_module
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from agent_bridge_conversations import ConversationRuntime, RESULT_TOOLS, TOOLS
from agent_bridge_universal import UniversalAdapterError, UniversalAgentRegistry


def test_default_codex_retirement_never_probes_or_executes(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_ENABLE_CODEX_ADAPTER", raising=False)
    monkeypatch.delenv("HERMES_BRIDGE_ENABLE_CODEX_ADAPTER", raising=False)
    def forbidden(*args):
        pytest.fail("A retired adapter must not invoke Codex")
    registry = UniversalAgentRegistry(tmp_path, tmp_path / "hermes", command_probe=forbidden)
    agents = {row["agent"]: row for row in registry.public_agents()}
    assert agents["codex"]["enabled"] is False
    assert agents["codex"]["available"] is False
    assert agents["hermes"]["enabled"] is True
    with pytest.raises(UniversalAdapterError) as caught:
        registry.execute("codex", "test", tmp_path, 1, "local", "test", 2, threading.Event())
    assert caught.value.code == "agent_unavailable"


def test_default_tools_hide_owner_routes_but_keep_results(monkeypatch):
    monkeypatch.delenv("AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", raising=False)
    monkeypatch.delenv("HERMES_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", raising=False)
    module = load_proxy_module()
    server = module._create_delegate_only_server(host="127.0.0.1", port=23887)
    module.add_bridge_tools(server)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names.intersection(TOOLS) == set(RESULT_TOOLS)
    assert set(module.DEFAULT_PUBLIC_TOOLS).issubset(names)
    assert set(module.UNIVERSAL_EXTENSION_TOOLS).issubset(names)
    capability = module._conversation_runtime().capabilities()
    assert capability["support_status"] == "unfinished"
    assert capability["enabled"] is False
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", "1")
    experimental = module._create_delegate_only_server(host="127.0.0.1", port=23888)
    module.add_bridge_tools(experimental)
    assert set(TOOLS).issubset({tool.name for tool in asyncio.run(experimental.list_tools())})


def test_retirement_preserves_results_and_never_resumes_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", "0")
    runtime = ConversationRuntime(tmp_path)
    runtime.jobs.mutate(lambda data: data.update({
        "done": {"caller": "local", "status": "completed", "reply": "retained reply"},
        "pending": {"caller": "local", "status": "pending", "_payload": {"private": "keep"}},
    }))
    before = runtime.jobs.read()
    assert runtime.recover_pending() == {"scheduled": []}
    assert runtime.result("local", "done")["reply"] == "retained reply"
    assert runtime.result("local", "pending")["error_code"] == "integration_disabled"
    assert runtime.jobs.read() == before
    assert not runtime.threads


@pytest.mark.parametrize("contract", [
    {"conversation_routing_extension": "conversation_routing_v1"},
    {"extension_tools": ["bridge_agent_complete_result"]},
])
def test_peer_results_remain_readable_without_owner_routing(monkeypatch, contract):
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", "0")
    module = load_proxy_module()
    monkeypatch.setattr(module, "_get_peer", lambda peer: ({"managed": True, "cert_pem": "synthetic"}, None))
    calls = []
    def peer_call(peer, tool, arguments):
        calls.append(tool)
        return {"public_tool_contract": contract} if tool == "bridge_agent_status" else {"reply": "retained"}
    monkeypatch.setattr(module, "_peer_call", peer_call)
    assert module._peer_conversation_call("peer", "bridge_agent_complete_result", {"task_id": "old"}) == {"reply": "retained"}
    assert calls == ["bridge_agent_status", "bridge_agent_complete_result"]
    calls.clear()
    assert module._peer_conversation_call("peer", "bridge_agent_routed_delegate_start", {})["error"] == "integration_disabled"
    assert calls == []


def test_switch_framework_then_return_resumes_only_that_framework(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_STUB_DELEGATE", "0")
    monkeypatch.setenv("HERMES_BRIDGE_STUB_DELEGATE", "0")
    monkeypatch.setenv("AGENT_BRIDGE_ENABLE_CODEX_ADAPTER", "0")
    registry = UniversalAgentRegistry(tmp_path, tmp_path / "missing-hermes")
    # Real child interpreters, isolated synthetic frameworks; no installed agent.
    agents = [{"id": agent, "kind": "cli", "command": [sys.executable, "-c",
               "import sys; print('session_id: ' + sys.argv[1]); print('|'.join(sys.argv[2:]))",
               agent + "-session", "{prompt}"], "resume_args": ["resume", "{session_id}"],
               "session_regex": r"session_id: ([a-z-]+)"} for agent in ("framework-a", "framework-b")]
    registry.config_file.write_text(json.dumps({"schema_version": 1, "agents": agents}))
    def send(agent, prompt):
        return registry.execute(agent, prompt, tmp_path, 1, "same-peer", "same-topic", 20, threading.Event())
    a = send("framework-a", "first")
    b = send("framework-b", "second")
    back = send("framework-a", "follow-up")
    assert a["agent"] == "framework-a" and not a["resumed_session"]
    assert b["agent"] == "framework-b" and not b["resumed_session"]
    assert back["agent"] == "framework-a" and back["resumed_session"]
    assert "resume|framework-a-session" in back["stdout"]
    assert "framework-b-session" not in back["stdout"]
    for unavailable in ("missing", "codex"):
        with pytest.raises(UniversalAdapterError) as caught:
            send(unavailable, "must not reach Hermes")
        assert caught.value.code == "agent_unavailable"
