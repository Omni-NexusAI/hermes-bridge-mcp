import importlib.util
import sys
import types
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_proxy():
    path = ROOT / "bin" / "windows-hermes-proxy-mcp.py"
    name = f"agent_bridge_rebrand_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_canonical_and_compatibility_launchers_are_shipped():
    canonical = (
        "bin/agent-bridge-mcp.py",
        "bin/agent-bridge-mcp-serve.cmd",
        "bin/start-agent-bridge.ps1",
        "bin/start-agent-bridge-peer.ps1",
        "bin/start-agent-bridge-peer.sh",
        "bin/agent-bridge-background-watchdog.ps1",
        "startup/Watch Agent Bridge MCP.vbs",
    )
    compatibility = (
        "bin/windows-hermes-proxy-mcp.py",
        "bin/hermes-bridge-mcp-serve.cmd",
        "bin/start-hermes-bridge-peer.ps1",
        "bin/start-hermes-bridge-peer.sh",
        "startup/Watch Windows Hermes MCP Bridge.vbs",
    )

    assert all((ROOT / name).is_file() for name in canonical)
    assert all((ROOT / name).is_file() for name in compatibility)


def test_canonical_environment_variables_override_legacy_aliases(
    tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical-state"
    legacy = tmp_path / "legacy-state"
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(canonical))
    monkeypatch.setenv("HERMES_BRIDGE_STATE_DIR", str(legacy))

    module = _load_proxy()

    assert module.BRIDGE_STATE_DIR == canonical
    assert module.os.environ["HERMES_BRIDGE_STATE_DIR"] == str(canonical)
    assert module.BRIDGE_VERSION == "v1.3.5"


def test_new_install_uses_agent_bridge_path_but_upgrade_keeps_legacy_state(
    tmp_path, monkeypatch
):
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    for name in (
        "AGENT_BRIDGE_HOME",
        "HERMES_BRIDGE_HOME",
        "HERMES_HOME",
        "AGENT_BRIDGE_STATE_DIR",
        "HERMES_BRIDGE_STATE_DIR",
        "AGENT_BRIDGE_STATE_FILE",
        "HERMES_BRIDGE_STATE_FILE",
    ):
        monkeypatch.delenv(name, raising=False)

    fresh = _load_proxy()
    assert fresh.AGENT_BRIDGE_HOME == local / "agent-bridge"
    assert fresh.BRIDGE_STATE_DIR == local / "agent-bridge" / "bridge-state"

    legacy_state = local / "hermes" / "bridge-state"
    legacy_state.mkdir(parents=True)
    legacy_file = legacy_state / "windows-hermes-proxy-state.json"
    legacy_file.write_text('{"sessions": {}, "tasks": {}}', encoding="utf-8")

    upgraded = _load_proxy()
    assert upgraded.AGENT_BRIDGE_HOME == local / "hermes"
    assert upgraded.BRIDGE_STATE_DIR == legacy_state
    assert upgraded.BRIDGE_STATE_FILE == legacy_file


def test_mdns_advertises_and_browses_canonical_and_legacy_services(
    monkeypatch, tmp_path
):
    from hermes_bridge_network import (
        CANONICAL_MDNS_TYPE,
        PRODUCTION_MDNS_TYPE,
        MdnsDiscovery,
    )

    registered = []
    browsed = []

    class FakeInfo:
        def __init__(self, service_type, name, **kwargs):
            self.type = service_type
            self.name = name

    class FakeZeroconf:
        def register_service(self, info, allow_name_change=False):
            registered.append(info.type)

        def unregister_service(self, info):
            return None

        def close(self):
            return None

    class FakeBrowser:
        def __init__(self, zeroconf, service_type, listener):
            browsed.append(service_type)

    fake_module = types.ModuleType("zeroconf")
    fake_module.ServiceInfo = FakeInfo
    fake_module.Zeroconf = FakeZeroconf
    fake_module.ServiceBrowser = FakeBrowser
    monkeypatch.setitem(sys.modules, "zeroconf", fake_module)
    monkeypatch.setenv("HERMES_BRIDGE_TEST_SANDBOX", "0")
    monkeypatch.setenv("AGENT_BRIDGE_ADVERTISE_ADDRESS", "192.0.2.10")

    class Identity:
        peer_id = "test-peer"
        display_name = "Test Peer"
        fingerprint = "a" * 64

    class Manager:
        identity = Identity()

        def set_runtime_advertise_address(self, address):
            self.address = address

    discovery = MdnsDiscovery(Manager(), "0.0.0.0", 18443)
    discovery.start()
    discovery.stop()

    assert set(registered) == {CANONICAL_MDNS_TYPE, PRODUCTION_MDNS_TYPE}
    assert set(browsed) == {CANONICAL_MDNS_TYPE, PRODUCTION_MDNS_TYPE}
