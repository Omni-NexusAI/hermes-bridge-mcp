from __future__ import annotations

import uuid

import pytest


@pytest.fixture(autouse=True)
def isolated_bridge_environment(monkeypatch, tmp_path):
    root = tmp_path / "sandbox"
    state = root / "state"
    home = root / "home"
    local_app = root / "localapp"
    namespace = f"test-{uuid.uuid4().hex}"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app))
    monkeypatch.setenv("HERMES_BRIDGE_HOME", str(root / "hermes"))
    monkeypatch.setenv("HERMES_BRIDGE_STATE_DIR", str(state))
    monkeypatch.setenv("HERMES_BRIDGE_STATE_FILE", str(state / "task-state.json"))
    monkeypatch.setenv("HERMES_BRIDGE_PEERS_CONFIG", str(state / "legacy-peers.json"))
    monkeypatch.setenv("HERMES_BRIDGE_TEST_SANDBOX", "1")
    monkeypatch.setenv("HERMES_BRIDGE_SANDBOX_ROOT", str(root))
    monkeypatch.setenv("HERMES_BRIDGE_DISCOVERY_BACKEND", "memory")
    monkeypatch.setenv("HERMES_BRIDGE_DISCOVERY_NAMESPACE", namespace)
    monkeypatch.setenv("HERMES_BRIDGE_STUB_DELEGATE", "1")
    monkeypatch.setenv("HERMES_BRIDGE_AUTO_DISCOVERY", "0")
    monkeypatch.delenv("HERMES_BRIDGE_PAIR_KEY", raising=False)
    monkeypatch.delenv("HERMES_BRIDGE_AUTH_TOKEN", raising=False)
    yield
