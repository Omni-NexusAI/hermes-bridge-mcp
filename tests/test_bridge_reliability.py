import asyncio
import json
import threading
import time
from pathlib import Path
import pytest
from test_windows_hermes_proxy_mcp import load_proxy_module


def test_complete_reply_survives_process_cache_loss(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "tasks.json")
    reply = "important opening decision\n" + "x" * 24000 + "\nfinal reply"
    module._persist_task({"task_id": "long", "status": "completed", "stdout": reply})
    module._TASKS.clear()
    saved = module._load_state()["tasks"]["long"]
    assert saved["stdout"] == reply
    assert len(module._task_status("long")["stdout"]) <= 8000


def test_readyz_identifies_running_payload(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setenv("AGENT_BRIDGE_BUILD_REVISION", "test-payload")
    messages = []
    async def send(message):
        messages.append(message)
    asyncio.run(module._HealthASGI(None, lambda: True)(
        {"type": "http", "method": "GET", "path": "/readyz"}, None, send))
    body = json.loads(messages[-1]["body"])
    assert body["status"] == "ready"
    assert body["build_revision"] == "test-payload"
    assert body["bridge_version"] == module.BRIDGE_VERSION
    assert body["process_id"] > 0


def test_status_works_without_hermes(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "HERMES_EXE", tmp_path / "missing-hermes")
    monkeypatch.setattr(module, "_delegate_runner_available", lambda: False)
    def forbidden(*args, **kwargs):
        raise AssertionError("Missing Hermes must never execute")
    monkeypatch.setattr(module, "_run_hidden", forbidden)
    tools = {}
    class Server:
        def tool(self, *args, **kwargs):
            def register(fn):
                tools[kwargs.get("name", fn.__name__)] = fn
                return fn
            return register
    module.add_bridge_tools(Server())
    result = json.loads(tools["bridge_agent_status"]())
    assert result["delegate_runner_available"] is False
    assert result["hermes_version"]["error"] == "adapter_unavailable"


def test_exact_fifteen_stable_schemas():
    # Fixed independently of the snapshot: an empty/truncated snapshot must
    # never turn this compatibility check into an empty-dictionary equality.
    core_names = {
        "bridge_agent_status", "bridge_agent_delegate", "bridge_agent_delegate_start",
        "bridge_agent_delegate_status", "bridge_agent_delegate_result", "bridge_agent_delegate_cancel",
        "bridge_peer_status", "bridge_peer_delegate_start", "bridge_peer_delegate_status",
        "bridge_peer_delegate_result", "bridge_peer_delegate_cancel",
    }
    universal_names = {
        "bridge_agent_universal_list", "bridge_agent_universal_delegate_start",
        "bridge_peer_universal_list", "bridge_peer_universal_delegate_start",
    }
    expected_names = core_names | universal_names
    assert len(core_names) == 11 and len(universal_names) == 4 and len(expected_names) == 15
    baseline = json.loads(Path(__file__).with_name("stable_tool_schemas.json").read_text())
    assert set(baseline) == expected_names, "Baseline 7101286 must contain all fifteen stable tool schemas"
    assert len(baseline) == 15
    module = load_proxy_module()
    assert len(module.DEFAULT_PUBLIC_TOOLS) == 11 and set(module.DEFAULT_PUBLIC_TOOLS) == core_names
    assert len(module.UNIVERSAL_EXTENSION_TOOLS) == 4 and set(module.UNIVERSAL_EXTENSION_TOOLS) == universal_names
    server = module._create_delegate_only_server(host="127.0.0.1", port=23988)
    module.add_bridge_tools(server)
    async def collect():
        return {tool.name: tool.inputSchema for tool in await server.list_tools() if tool.name in expected_names}
    actual = asyncio.run(collect())
    assert set(actual) == expected_names and len(actual) == 15, "Stable tools must actually be registered"
    assert actual == baseline


def _tools(module):
    result = {}
    class Server:
        def tool(self, *args, **kwargs):
            def register(fn):
                result[kwargs.get("name", fn.__name__)] = fn
                return fn
            return register
    module.add_bridge_tools(Server())
    return result


def test_complete_result_pages_rebuild_original_reply(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "tasks.json")
    reply = "opening\n" + "context" * 4000 + "\nanswer"
    module._persist_task({"task_id": "long", "caller_peer": "local", "status": "completed", "stdout": reply})
    tools = _tools(module)
    chunks, offset = [], 0
    while True:
        page = json.loads(tools["bridge_agent_complete_result"]("long", offset, 10000))
        chunks.append(page["reply"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(chunks) == reply
    token = module._CALLER_PEER_CONTEXT.set("different-peer")
    try:
        assert json.loads(tools["bridge_agent_complete_result"]("long"))["error"] == "task_not_found"
    finally:
        module._CALLER_PEER_CONTEXT.reset(token)


def test_peer_route_requires_advertised_extension(monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "_get_peer", lambda peer: ({"managed": True, "cert_pem": "synthetic"}, None))
    calls = []
    def peer_call(peer, tool, arguments):
        calls.append(tool)
        return {"public_tool_contract": {}}
    monkeypatch.setattr(module, "_peer_call", peer_call)
    result = module._peer_conversation_call("peer", "bridge_agent_routed_delegate_start", {})
    assert result["error"] == "extension_unsupported"
    assert calls == ["bridge_agent_status"]


def test_completed_routed_reply_survives_missing_owner_config(tmp_path):
    from agent_bridge_conversations import ConversationRuntime
    runtime = ConversationRuntime(tmp_path)
    runtime._save("codex-task", {"caller": "peer", "status": "completed", "reply": "retained answer", "fingerprint": "private"})
    assert runtime.result("peer", "codex-task")["reply"] == "retained answer"


def test_interrupted_owner_does_not_remain_running(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "tasks.json")
    module._persist_task({"task_id": "interrupted", "status": "running", "owner_pid": 123,
                          "owner_token": "old", "stdout": ""})
    monkeypatch.setattr(module, "process_alive", lambda pid: False)
    assert module._task_status("interrupted")["status"] == "interrupted"
    assert "will not rerun" in module._task_status("interrupted")["next_action"]


def test_foreign_live_owner_is_not_marked_interrupted(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "tasks.json")
    module._persist_task({"task_id": "live", "status": "running", "owner_pid": 123})
    for alive in (True, None):
        monkeypatch.setattr(module, "process_alive", lambda pid: alive)
        assert module._task_status("live")["status"] == "running"


def test_legacy_token_cannot_use_conversation_extensions():
    module = load_proxy_module()
    tools = _tools(module)
    token = module._CONVERSATION_ACCESS_CONTEXT.set(False)
    try:
        assert json.loads(tools["bridge_agent_projects_list"]())["error"] == "managed_peer_required"
    finally:
        module._CONVERSATION_ACCESS_CONTEXT.reset(token)


def test_unauthenticated_http_also_gates_conversation_access():
    module = load_proxy_module()
    observed = []
    async def app(scope, receive, send):
        observed.append(module._CONVERSATION_ACCESS_CONTEXT.get())
    middleware = module._ConversationRequestContext(app)
    asyncio.run(middleware({"type": "http", "client": ("192.0.2.1", 1)}, None, None))
    asyncio.run(middleware({"type": "http", "client": ("127.0.0.1", 1)}, None, None))
    assert observed == [False, True]


@pytest.mark.parametrize("operation", ["status", "result", "cancel"])
@pytest.mark.parametrize("client,managed,allowed", [
    ("192.0.2.1", False, False),
    ("127.0.0.1", False, True),
    ("192.0.2.1", True, True),
])
def test_core_routed_tasks_enforce_http_caller_access(monkeypatch, operation, client, managed, allowed):
    module = load_proxy_module()
    tools = _tools(module)
    calls = []
    class Runtime:
        def result(self, caller, task_id):
            calls.append(("result", caller, task_id))
            return {"task_id": task_id, "status": "completed", "reply": "owner reply"}
        def cancel(self, caller, task_id):
            calls.append(("cancel", caller, task_id))
            return {"task_id": task_id, "status": "canceled"}
        def preview(self, record):
            return record
    def runtime():
        assert allowed, "Denied callers must not access owner routing state"
        return Runtime()
    monkeypatch.setattr(module, "_conversation_runtime", runtime)
    observed = []
    async def app(scope, receive, send):
        observed.append(json.loads(tools["bridge_agent_delegate_" + operation]("codex-private-task")))
    middleware = module._BearerTokenMiddleware(
        app, ["synthetic-token"], "/mcp",
        peer_resolver=lambda token: "managed-peer" if managed else None,
    )
    asyncio.run(middleware({"type": "http", "path": "/mcp", "client": (client, 1),
                            "headers": [(b"authorization", b"Bearer synthetic-token")]}, None, None))
    if allowed:
        assert observed[0]["status"] == ("canceled" if operation == "cancel" else "completed")
        assert calls == [("cancel" if operation == "cancel" else "result",
                          "managed-peer" if managed else "local", "codex-private-task")]
    else:
        assert observed[0]["error"] == "managed_peer_required"
        assert calls == []


def test_legacy_deliveries_serialize_and_resume_latest_session(tmp_path, monkeypatch):
    module = load_proxy_module()
    monkeypatch.setattr(module, "BRIDGE_STATE_DIR", tmp_path)
    monkeypatch.setattr(module, "BRIDGE_STATE_FILE", tmp_path / "tasks.json")
    entered, release = threading.Event(), threading.Event()
    starts = []
    class Proc:
        pid = 1234
        returncode = 0
        def poll(self):
            return self.returncode
        def communicate(self, timeout):
            if len(starts) == 1:
                entered.set()
                assert release.wait(3)
            return "reply " + "x" * 16000, "session_id: persisted-session"
    def start(args, cwd):
        starts.append(args)
        return Proc()
    monkeypatch.setattr(module, "_start_hidden", start)
    prepared = {"prompt": "contribute", "args": [], "cwd": tmp_path, "max_turns": 5, "a0_thread_key": "same-topic"}
    first = module._start_delegate_task(prepared, 20)
    assert entered.wait(3)
    second = module._start_delegate_task(prepared, 20)
    time.sleep(0.1)
    assert len(starts) == 1
    release.set()
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and module._task_status(second["task_id"])["status"] != "completed":
        time.sleep(0.02)
    assert len(starts) == 2
    assert "persisted-session" in starts[1]
    assert len(module._complete_task_record(first["task_id"])["stdout"]) > 16000
