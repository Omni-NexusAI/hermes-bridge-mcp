from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from hermes_bridge_network import (  # noqa: E402
    InMemoryDiscovery,
    NetworkManager,
    PairingState,
    _pinned_ssl_context,
    validate_sandbox_config,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_identity_is_persistent_and_private(tmp_path):
    manager = NetworkManager(tmp_path / "state", secure_port=free_port())
    first = manager.identity
    second = NetworkManager(tmp_path / "state", secure_port=free_port()).identity

    assert first.peer_id == second.peer_id
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert "PRIVATE KEY" in first.key_path.read_text(encoding="utf-8")
    assert first.fingerprint not in first.key_path.read_text(encoding="utf-8")


def test_candidates_are_untrusted_redacted_and_conflicts_are_retained(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    remote = NetworkManager(tmp_path / "remote-state").identity
    discovery = InMemoryDiscovery(manager)
    fingerprint_a = remote.fingerprint
    fingerprint_b = "b" * 64
    discovery.publish({"peer_id": "peer-a", "fingerprint": fingerprint_a, "url": "https://127.0.0.1:24001/mcp"})

    status = manager.network_status()
    assert status["paired_peers"] == []
    assert status["candidates"][0]["fingerprint"] == fingerprint_a
    assert "token" not in json.dumps(status).lower()

    manager.state.save_peer({
        "peer_id": "peer-a",
        "fingerprint": fingerprint_a,
        "cert_pem": remote.cert_pem,
        "url": "https://127.0.0.1:24001/mcp",
        "inbound_token": "i" * 32,
        "outbound_token": "o" * 32,
        "receipt": {"test": True},
    })
    conflict = discovery.publish({"peer_id": "peer-a", "fingerprint": fingerprint_b, "url": "https://127.0.0.1:24002/mcp"})
    assert "pinned to another identity" in conflict["conflict"]


def test_forced_local_forget_is_fingerprint_bound_and_retry_visible(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    remote = NetworkManager(tmp_path / "remote-state").identity
    manager.state.save_peer({
        "peer_id": "offline-peer",
        "fingerprint": remote.fingerprint,
        "cert_pem": remote.cert_pem,
        "url": "https://127.0.0.1:24001/mcp",
        "inbound_token": "i" * 32,
        "outbound_token": "o" * 32,
        "receipt": {"test": True},
    })
    with pytest.raises(ValueError, match="expected fingerprint"):
        manager.forget_local("offline-peer", "f" * 64)
    preview = manager.forget_local("offline-peer", remote.fingerprint, dry_run=True)
    assert preview["remote_cleanup_required"] is True
    assert manager.state.peer("offline-peer") is not None
    forgotten = manager.forget_local("offline-peer", remote.fingerprint)
    assert forgotten["status"] == "local_only"
    assert forgotten["scope"] == "local"
    assert forgotten["remote_cleanup_required"] is True
    assert manager.state.peer("offline-peer") is None


def test_replay_expiry_revocation_and_sandbox_guards(tmp_path, monkeypatch):
    state = PairingState(tmp_path / "state")
    nonce = "n" * 24
    state.consume_nonce(nonce, time.time() + 30)
    with pytest.raises(ValueError, match="already been used"):
        state.consume_nonce(nonce, time.time() + 30)
    with pytest.raises(ValueError, match="expired"):
        state.consume_nonce("x" * 24, time.time() - 1)

    root = tmp_path / "sandbox"
    monkeypatch.setenv("HERMES_BRIDGE_SANDBOX_ROOT", str(root))
    with pytest.raises(RuntimeError, match="production bridge port"):
        validate_sandbox_config(root / "state", "127.0.0.1", [18084])
    with pytest.raises(RuntimeError, match="loopback"):
        validate_sandbox_config(root / "state", "0.0.0.0", [24001])


def test_revoked_and_changed_identities_cannot_silently_reclaim_peer_id(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    remote_a = NetworkManager(tmp_path / "remote-a").identity
    remote_b = NetworkManager(tmp_path / "remote-b").identity
    manager.state.save_peer({
        "peer_id": "remote",
        "fingerprint": remote_a.fingerprint,
        "cert_pem": remote_a.cert_pem,
        "url": "https://127.0.0.1:24001/mcp",
        "inbound_token": "i" * 32,
        "outbound_token": "o" * 32,
        "receipt": {"test": True},
    })
    revoked = manager.state.revoke("remote")
    assert revoked["status"] == "revoked"
    candidate = manager.state.ingest_candidate({"peer_id": "remote", "fingerprint": remote_a.fingerprint, "url": "https://127.0.0.1:24002/mcp"})
    assert candidate["revoked"] is True
    with pytest.raises(ValueError, match="revoked"):
        manager.state.save_peer({
            "peer_id": "remote",
            "fingerprint": remote_a.fingerprint,
            "cert_pem": remote_a.cert_pem,
            "url": "https://127.0.0.1:24002/mcp",
            "inbound_token": "i" * 32,
            "outbound_token": "o" * 32,
            "receipt": {"test": True},
        })

    manager.state.ingest_candidate({"peer_id": "changed", "fingerprint": remote_b.fingerprint, "url": "https://127.0.0.1:24003/mcp"})
    manager.state.save_peer({
        "peer_id": "changed",
        "fingerprint": remote_b.fingerprint,
        "cert_pem": remote_b.cert_pem,
        "url": "https://127.0.0.1:24003/mcp",
        "inbound_token": "j" * 32,
        "outbound_token": "p" * 32,
        "receipt": {"test": True},
    })
    conflict = manager.state.ingest_candidate({"peer_id": "changed", "fingerprint": "c" * 64, "url": "https://127.0.0.1:24004/mcp"})
    assert conflict["conflict"]


def _server_env(root: Path, state: Path, port: int, peer_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
        "LOCALAPPDATA": str(root / "localapp"),
        "HERMES_BRIDGE_HOME": str(root / "hermes"),
        "HERMES_BRIDGE_STATE_DIR": str(state),
        "HERMES_BRIDGE_STATE_FILE": str(state / "tasks.json"),
        "HERMES_BRIDGE_PEERS_CONFIG": str(state / "legacy.json"),
        "HERMES_BRIDGE_TEST_SANDBOX": "1",
        "HERMES_BRIDGE_SANDBOX_ROOT": str(root),
        "HERMES_BRIDGE_DISCOVERY_BACKEND": "memory",
        "HERMES_BRIDGE_DISCOVERY_NAMESPACE": f"test-{peer_id}",
        "HERMES_BRIDGE_STUB_DELEGATE": "1",
        "HERMES_BRIDGE_AUTO_DISCOVERY": "0",
        "HERMES_BRIDGE_PEER_ID": peer_id,
        "HERMES_BRIDGE_DISPLAY_NAME": peer_id,
        "HERMES_BRIDGE_SECURE_PORT": str(port),
        "HERMES_BRIDGE_ADVERTISE_ADDRESS": "127.0.0.1",
        "PYTHONPATH": str(BIN),
    })
    env.pop("HERMES_BRIDGE_PAIR_KEY", None)
    env.pop("HERMES_BRIDGE_AUTH_TOKEN", None)
    return env


def _start_server(root: Path, state: Path, port: int, peer_id: str) -> subprocess.Popen:
    (root / "home").mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [sys.executable, str(BIN / "windows-hermes-proxy-mcp.py"), "--transport", "streamable-http", "--secure-network", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=_server_env(root, state, port, peer_id),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 20
    url = f"https://127.0.0.1:{port}/bridge/v1/identity"
    while time.time() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(f"sandbox server exited early\nstdout={stdout}\nstderr={stderr}")
        try:
            if httpx.get(url, verify=False, timeout=0.5, trust_env=False).status_code == 200:
                return process
        except Exception:
            time.sleep(0.1)
    process.terminate()
    raise AssertionError("sandbox server did not become ready")


def _stop_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


async def _remote_tool(peer: dict, tool: str, arguments: dict) -> dict:
    context = _pinned_ssl_context(peer["cert_pem"])
    headers = {"Authorization": f"Bearer {peer['outbound_token']}"}
    async with httpx.AsyncClient(verify=context, headers=headers, timeout=10, trust_env=False) as client:
        async with streamable_http_client(peer["url"], http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                for item in result.content:
                    text = getattr(item, "text", "")
                    if text:
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError as exc:
                            raise AssertionError(f"tool returned non-JSON text: {text!r}; result={result!r}") from exc
                structured = getattr(result, "structuredContent", None)
                if isinstance(structured, dict):
                    value = structured.get("result", structured)
                    return json.loads(value) if isinstance(value, str) else value
                raise AssertionError(f"tool returned no JSON content: {result!r}")


def test_two_sandboxed_https_peers_pair_delegate_recover_and_rekey(tmp_path):
    root = tmp_path / "integration"
    state_a = root / "a" / "state"
    state_b = root / "b" / "state"
    port_a, port_b = free_port(), free_port()
    proc_a = _start_server(root, state_a, port_a, "sandbox-a")
    proc_b = _start_server(root, state_b, port_b, "sandbox-b")
    try:
        manager_a = NetworkManager(state_a, secure_port=port_a)
        manager_b = NetworkManager(state_b, secure_port=port_b)
        identity_b = manager_b.identity
        with pytest.raises(Exception):
            asyncio.run(_remote_tool({
                "url": f"https://127.0.0.1:{port_b}/mcp",
                "cert_pem": manager_a.identity.cert_pem,
                "outbound_token": "not-a-real-token",
            }, "bridge_agent_status", {}))
        manager_a.state.ingest_candidate({
            "peer_id": identity_b.peer_id,
            "display_name": identity_b.display_name,
            "fingerprint": identity_b.fingerprint,
            "url": f"https://127.0.0.1:{port_b}/mcp",
            "platform": "sandbox",
            "bridge_version": "v1.3.0",
            "protocol_version": "1",
            "source": "memory",
        })

        paired = manager_a.approve("sandbox-b", identity_b.fingerprint)
        assert paired["status"] == "paired"
        assert manager_b.state.peer("sandbox-a") is not None

        identity_c = NetworkManager(root / "c" / "state").identity
        manager_b.state.save_peer({
            "peer_id": "sandbox-c",
            "display_name": "sandbox-c",
            "fingerprint": identity_c.fingerprint,
            "cert_pem": identity_c.cert_pem,
            "url": f"https://127.0.0.1:{free_port()}/mcp",
            "platform": "sandbox",
            "inbound_token": "c" * 32,
            "outbound_token": "d" * 32,
            "receipt": {"test": "introduction-only"},
        })
        introductions = manager_a.refresh_trusted_introductions()
        assert introductions["introduced_candidates"] == 1
        assert manager_a.state.candidate("sandbox-c", identity_c.fingerprint) is not None
        assert manager_a.state.peer("sandbox-c") is None

        result = asyncio.run(_remote_tool(manager_a.state.peer("sandbox-b"), "bridge_agent_delegate_start", {"prompt": "sandbox work", "timeout_seconds": 5, "max_turns": 2}))
        assert result["task_id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            final = asyncio.run(_remote_tool(manager_a.state.peer("sandbox-b"), "bridge_agent_delegate_result", {"task_id": result["task_id"]}))
            if final.get("status") in {"completed", "failed"}:
                break
            time.sleep(0.1)
        assert final["status"] == "completed"
        assert "SANDBOX_DELEGATE_OK" in final["stdout"]

        _stop_server(proc_b)
        # Simulate loss of B's peer records while preserving B's device identity.
        manager_b.state.store.mutate(lambda data: data.update({"peers": {}}))
        new_port_b = free_port()
        proc_b = _start_server(root, state_b, new_port_b, "sandbox-b")
        manager_b = NetworkManager(state_b, secure_port=new_port_b)
        manager_a.state.ingest_candidate({
            "peer_id": "sandbox-b",
            "fingerprint": identity_b.fingerprint,
            "url": f"https://127.0.0.1:{new_port_b}/mcp",
            "platform": "sandbox",
            "bridge_version": "v1.3.0",
            "protocol_version": "1",
            "source": "memory",
        })
        recovered = manager_a.recover_known_endpoints()
        assert recovered["recovered_peers"] == ["sandbox-b"]
        assert manager_a.state.peer("sandbox-b")["url"].endswith(f":{new_port_b}/mcp")
        assert manager_b.state.peer("sandbox-a") is not None

        reverse = asyncio.run(_remote_tool(manager_b.state.peer("sandbox-a"), "bridge_agent_status", {}))
        assert reverse["bridge_version"] == "v1.3.5"

        preview = manager_a.unpair("sandbox-b", identity_b.fingerprint, dry_run=True)
        assert preview["status"] == "preview"
        assert manager_a.state.peer("sandbox-b") is not None
        # Simulate a lost commit response: B committed, while A still records
        # the prepared transaction. Retrying must finish idempotently.
        transaction_id = "r" * 24
        peer_b = manager_a.state.peer("sandbox-b")
        manager_a.state.prepare_unpair("sandbox-b", identity_b.fingerprint, transaction_id, "sandbox-a")
        token = peer_b["outbound_token"]
        manager_b.accept_unpair_prepare(manager_a._unpair_request(peer_b, transaction_id, "prepare"), token)
        manager_b.accept_unpair_commit(manager_a._unpair_request(peer_b, transaction_id, "commit"), token)
        unpaired = manager_a.unpair("sandbox-b", identity_b.fingerprint)
        assert unpaired["status"] == "committed"
        assert unpaired["scope"] == "both"
        assert manager_a.state.peer("sandbox-b") is None
        manager_b = NetworkManager(state_b, secure_port=new_port_b)
        assert manager_b.state.peer("sandbox-a") is None
    finally:
        _stop_server(proc_a)
        if proc_b.poll() is None:
            _stop_server(proc_b)
