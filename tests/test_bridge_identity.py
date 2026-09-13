"""Trust persistence regressions using generated, temporary identities only."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from hermes_bridge_network import AtomicJsonStore, IdentityStore, NetworkManager, PairingState, TailscaleDiscovery, _verify_signature


@pytest.mark.parametrize("missing", ["key_path", "cert_path", "meta_path"])
def test_partial_identity_fails_without_replacing_remaining_files(tmp_path, missing):
    store = IdentityStore(tmp_path / "state")
    store.ensure()
    paths = [store.key_path, store.cert_path, store.meta_path]
    getattr(store, missing).unlink()
    remaining = {path: path.read_bytes() for path in paths if path.exists()}
    with pytest.raises(ValueError, match="incomplete"):
        store.ensure()
    assert {path: path.read_bytes() for path in remaining} == remaining
    assert not getattr(store, missing).exists()


def test_identity_rejects_mismatched_private_key(tmp_path):
    first, second = IdentityStore(tmp_path / "first"), IdentityStore(tmp_path / "second")
    first.ensure()
    second.ensure()
    first.key_path.write_bytes(second.key_path.read_bytes())
    with pytest.raises(ValueError, match="private key.*certificate"):
        first.ensure()


@pytest.mark.parametrize("content", ["broken-json", "[]", "null"])
def test_corrupt_pairing_state_does_not_erase_revocations(tmp_path, content):
    path = tmp_path / "network.json"
    path.write_text(content, encoding="utf-8")
    store = AtomicJsonStore(path, lambda: {"revoked": {}})
    with pytest.raises((ValueError, RuntimeError)):
        store.mutate(lambda data: data.update(peers={"previously-revoked": {}}))
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("revoked", [[], None, "invalid"])
def test_malformed_revocations_fail_closed(tmp_path, revoked):
    state = PairingState(tmp_path / "state")
    state.revocations.path.parent.mkdir(parents=True)
    state.revocations.path.write_text(json.dumps({"schema_version": 1, "revoked": revoked}), encoding="utf-8")
    with pytest.raises(ValueError, match="revocations"):
        state.snapshot()


def test_discovery_outage_preserves_pairing_and_credentials(tmp_path):
    manager = NetworkManager(tmp_path / "local")
    local = manager.identity
    remote = IdentityStore(tmp_path / "remote").ensure()
    manager.state.save_peer({"peer_id": "remote", "fingerprint": remote.fingerprint,
                             "cert_pem": remote.cert_pem, "url": "https://127.0.0.1:24789/mcp",
                             "inbound_token": "i" * 32, "outbound_token": "o" * 32,
                             "receipt": {"test": True}})
    before = manager.state.snapshot()
    def unavailable(*args):
        raise FileNotFoundError("synthetic unavailable CLI")
    discovery = TailscaleDiscovery(manager, 24789, unavailable, unavailable)
    discovery.provider = "cli"
    assert discovery.scan_once()["status"] == "degraded"
    assert manager.state.snapshot() == before
    assert manager.identity.fingerprint == local.fingerprint


def test_extracted_identity_uses_canonical_name_and_signs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_PEER_ID", "canonical-peer")
    monkeypatch.setenv("HERMES_BRIDGE_PEER_ID", "legacy-peer")
    from agent_bridge_identity import IdentityStore as ExtractedIdentityStore
    assert ExtractedIdentityStore is IdentityStore
    store = ExtractedIdentityStore(tmp_path / "state")
    identity = store.ensure()
    assert identity.peer_id == "canonical-peer"
    payload = {"nonce": "test-only-nonce", "purpose": "compatibility"}
    _verify_signature(identity.cert_pem, payload, store.sign(payload))
