from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

from hermes_bridge_network import (  # noqa: E402
    NetworkASGI,
    NetworkManager,
    TailscaleDiscovery,
    _is_tailscale_ip,
)


def _status(*peers, backend="Running", self_ips=None):
    return {
        "BackendState": backend,
        "TailscaleIPs": self_ips or ["100.64.0.1", "fd7a:115c:a1e0::1"],
        "Self": {"TailscaleIPs": self_ips or ["100.64.0.1"]},
        "Peer": {f"node-{index}": peer for index, peer in enumerate(peers)},
    }


def _peer(*ips, tags=None, online=True, expired=False):
    return {
        "Online": online,
        "Expired": expired,
        "Tags": tags if tags is not None else ["tag:hermes-bridge"],
        "TailscaleIPs": list(ips),
    }


def _runner(payload, returncode=0):
    def run(command, timeout):
        assert command[-2:] == ["status", "--json"]
        assert timeout == 10.0
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    return run


def _identity(peer_id="remote", fingerprint="a" * 64):
    return {
        "peer_id": peer_id,
        "display_name": "Remote Bridge",
        "fingerprint": fingerprint,
        "platform": "linux",
        "bridge_version": "v1.3.0",
        "protocol_version": "1",
    }


def _api_device(*ips, tags=None, online=True, expires=None, hostname="remote-bridge", os_name="linux"):
    return {
        "addresses": list(ips),
        "tags": tags if tags is not None else ["tag:hermes-bridge"],
        "online": online,
        "expires": expires,
        "hostname": hostname,
        "name": f"{hostname}.example.ts.net",
        "os": os_name,
    }


def _api_payload(*devices):
    return {"devices": list(devices)}


def _api_fetcher(payload, *, error=None, seen=None):
    def fetch(url, token, timeout):
        if seen is not None:
            seen.append((url, token, timeout))
        if error:
            raise error
        return payload

    return fetch


def test_tailscale_ranges_are_narrow():
    assert _is_tailscale_ip("100.64.0.1")
    assert _is_tailscale_ip("100.127.255.254")
    assert not _is_tailscale_ip("100.115.92.1")
    assert not _is_tailscale_ip("100.128.0.1")
    assert _is_tailscale_ip("fd7a:115c:a1e0::1234")
    assert not _is_tailscale_ip("fd7a:115c:a1e1::1")


def test_scan_filters_to_online_tagged_unexpired_peers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_DISCOVERY_BACKEND", "tailscale")
    manager = NetworkManager(tmp_path / "state")
    payload = _status(
        _peer("100.64.0.2", "fd7a:115c:a1e0::2"),
        _peer("100.64.0.3", tags=["tag:other"]),
        _peer("100.64.0.4", online=False),
        _peer("100.64.0.5", expired=True),
        _peer("192.0.2.5"),
        _peer("100.64.0.1"),
    )
    probed = []

    def fetch(url):
        probed.append(url)
        return _identity()

    discovery = TailscaleDiscovery(manager, 18443, _runner(payload), fetch)
    result = discovery.scan_once()

    assert result["status"] == "healthy"
    assert result["eligible_peer_count"] == 1
    assert result["candidate_count"] == 1
    assert probed == ["https://100.64.0.2:18443/mcp"]
    candidate = manager.network_status()["candidates"][0]
    assert candidate["source"] == "tailscale"
    assert candidate["peer_id"] == "remote"
    assert manager.state.peer("remote") is None
    assert manager._advertised_url() == "https://100.64.0.1:18443/mcp"


def test_api_provider_filters_and_records_untrusted_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_PROVIDER", "api")
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_API_TOKEN", "tskey-api-secret")
    monkeypatch.setenv("HERMES_BRIDGE_TAILNET", "example.com")
    manager = NetworkManager(tmp_path / "state")
    payload = _api_payload(
        _api_device("100.64.0.20", "fd7a:115c:a1e0::20"),
        _api_device("100.64.0.21", tags=["tag:other"]),
        _api_device("100.64.0.22", online=False),
        _api_device("100.64.0.23", expires="2000-01-01T00:00:00Z"),
        _api_device("192.0.2.24"),
    )
    seen = []
    probed = []

    def fetch_identity(url):
        probed.append(url)
        return _identity("api-remote")

    discovery = TailscaleDiscovery(
        manager,
        18443,
        command_runner=lambda _command, _timeout: pytest.fail("api provider must not invoke CLI"),
        identity_fetcher=fetch_identity,
        api_fetcher=_api_fetcher(payload, seen=seen),
    )

    result = discovery.scan_once()

    assert result["status"] == "healthy"
    assert result["provider"] == "api"
    assert result["eligible_peer_count"] == 1
    assert result["candidate_count"] == 1
    assert probed == ["https://100.64.0.20:18443/mcp"]
    assert seen[0][1] == "tskey-api-secret"
    assert "/tailnet/example.com/devices?" in seen[0][0]
    candidate = manager.network_status()["candidates"][0]
    assert candidate["source"] == "tailscale-api"
    assert candidate["peer_id"] == "api-remote"
    assert manager.state.peer("api-remote") is None


def test_api_provider_falls_back_to_ipv6_and_skips_self_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_PROVIDER", "api")
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_API_TOKEN", "tskey-api-secret")
    monkeypatch.setenv("HERMES_BRIDGE_TAILNET", "example.com")
    manager = NetworkManager(tmp_path / "state")
    payload = _api_payload(
        _api_device("100.64.0.30", "fd7a:115c:a1e0::30"),
        _api_device("100.64.0.31"),
    )
    probed = []

    def fetch_identity(url):
        probed.append(url)
        if "100.64.0.30" in url:
            raise TimeoutError("ipv4 unavailable")
        if "fd7a:115c:a1e0::30" in url:
            return _identity("api-v6")
        return _identity(manager.identity.peer_id, manager.identity.fingerprint)

    discovery = TailscaleDiscovery(
        manager,
        25002,
        command_runner=lambda _command, _timeout: pytest.fail("api provider must not invoke CLI"),
        identity_fetcher=fetch_identity,
        api_fetcher=_api_fetcher(payload),
    )

    result = discovery.scan_once()

    assert result["candidate_count"] == 1
    assert result["probe_failure_count"] == 0
    assert probed == [
        "https://100.64.0.30:25002/mcp",
        "https://[fd7a:115c:a1e0::30]:25002/mcp",
        "https://100.64.0.31:25002/mcp",
    ]
    assert manager.network_status()["candidates"][0]["url"].startswith("https://[fd7a:")


def test_api_provider_can_refresh_known_peer_candidate_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_PROVIDER", "api")
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_API_TOKEN", "tskey-api-secret")
    monkeypatch.setenv("HERMES_BRIDGE_TAILNET", "example.com")
    manager = NetworkManager(tmp_path / "state")
    remote = NetworkManager(tmp_path / "remote-state").identity
    manager.state.save_peer({
        "peer_id": "known-peer",
        "fingerprint": remote.fingerprint,
        "cert_pem": remote.cert_pem,
        "url": "https://100.64.0.40:18443/mcp",
        "inbound_token": "i" * 32,
        "outbound_token": "o" * 32,
        "receipt": {"test": True},
    })
    payload = _api_payload(_api_device("100.64.0.41"))
    discovery = TailscaleDiscovery(
        manager,
        18443,
        command_runner=lambda _command, _timeout: pytest.fail("api provider must not invoke CLI"),
        identity_fetcher=lambda _url: _identity("known-peer", remote.fingerprint),
        api_fetcher=_api_fetcher(payload),
    )

    # With the new 'pause when paired' feature, scan_once will early return if a peer is paired.
    # Therefore it will not scan and will not find candidates. We assert this new behavior.
    assert discovery.scan_once().get("status") == "paused"

    peer = manager.state.peer("known-peer")
    assert peer["url"] == "https://100.64.0.40:18443/mcp"
    # Because it is paused, it shouldn't update the candidate_url either.
    assert "candidate_url" not in peer


def test_auto_provider_uses_api_when_cli_is_unavailable_and_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_PROVIDER", "auto")
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_API_TOKEN", "tskey-api-secret")
    monkeypatch.setenv("HERMES_BRIDGE_TAILNET", "example.com")
    manager = NetworkManager(tmp_path / "state")
    discovery = TailscaleDiscovery(
        manager,
        18443,
        command_runner=lambda _command, _timeout: (_ for _ in ()).throw(FileNotFoundError("tailscale")),
        identity_fetcher=lambda _url: _identity("api-fallback"),
        api_fetcher=_api_fetcher(_api_payload(_api_device("100.64.0.50"))),
    )

    result = discovery.scan_once()

    assert result["status"] == "healthy"
    assert result["provider"] == "api"
    assert manager.network_status()["candidates"][0]["source"] == "tailscale-api"


@pytest.mark.parametrize(
    ("env", "payload", "error", "message"),
    [
        ({}, None, None, "tailscale_api_token_missing"),
        ({"HERMES_BRIDGE_TAILSCALE_API_TOKEN": "tskey-api-secret"}, None, None, "tailscale_tailnet_missing"),
        ({"HERMES_BRIDGE_TAILSCALE_API_TOKEN": "tskey-api-secret", "HERMES_BRIDGE_TAILNET": "example.com"}, [], None, "tailscale_api_invalid_document"),
        ({"HERMES_BRIDGE_TAILSCALE_API_TOKEN": "tskey-api-secret", "HERMES_BRIDGE_TAILNET": "example.com"}, {"devices": "bad"}, None, "tailscale_api_invalid_document"),
        ({"HERMES_BRIDGE_TAILSCALE_API_TOKEN": "tskey-api-secret", "HERMES_BRIDGE_TAILNET": "example.com"}, None, RuntimeError("tailscale_api_unauthorized"), "tailscale_api_unauthorized"),
        ({"HERMES_BRIDGE_TAILSCALE_API_TOKEN": "tskey-api-secret", "HERMES_BRIDGE_TAILNET": "example.com"}, None, httpx.ReadTimeout("timeout"), "tailscale_api_timeout"),
    ],
)
def test_api_provider_failures_are_sanitized_and_nonfatal(tmp_path, monkeypatch, env, payload, error, message):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_PROVIDER", "api")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    manager = NetworkManager(tmp_path / "state")
    discovery = TailscaleDiscovery(
        manager,
        18443,
        command_runner=lambda _command, _timeout: pytest.fail("api provider must not invoke CLI"),
        identity_fetcher=lambda _url: _identity(),
        api_fetcher=_api_fetcher(payload, error=error),
    )

    result = discovery.scan_once()

    assert result["status"] == "degraded"
    assert result["last_error"] == message
    assert result["candidate_count"] == 0
    rendered = json.dumps(result)
    assert "tskey" not in rendered
    assert "example.com" not in rendered


def test_scan_falls_back_to_ipv6_and_brackets_url(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    payload = _status(_peer("100.64.0.9", "fd7a:115c:a1e0::9"))
    probed = []

    def fetch(url):
        probed.append(url)
        if url.startswith("https://100."):
            raise TimeoutError("unreachable")
        return _identity()

    discovery = TailscaleDiscovery(manager, 25001, _runner(payload), fetch)
    result = discovery.scan_once()

    assert result["candidate_count"] == 1
    assert probed == [
        "https://100.64.0.9:25001/mcp",
        "https://[fd7a:115c:a1e0::9]:25001/mcp",
    ]
    assert manager.network_status()["candidates"][0]["url"].startswith("https://[fd7a:")


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (_runner("not-json"), "invalid JSON"),
        (_runner(_status(backend="NeedsLogin")), "not running"),
        (_runner({}, returncode=1), "status command failed"),
        (lambda _command, _timeout: (_ for _ in ()).throw(FileNotFoundError("C:/secret/tailscale.exe")), "tailscale_cli_unavailable"),
        (lambda _command, _timeout: (_ for _ in ()).throw(subprocess.TimeoutExpired("tailscale", 10)), "tailscale_cli_timeout"),
    ],
)
def test_scan_failures_are_sanitized_and_nonfatal(tmp_path, runner, message):
    manager = NetworkManager(tmp_path / "state")
    discovery = TailscaleDiscovery(manager, 18443, runner, lambda _url: _identity())

    result = discovery.scan_once()

    assert result["status"] == "degraded"
    assert message in result["last_error"]
    assert result["candidate_count"] == 0
    assert "100.64." not in json.dumps(result)


def test_peer_list_shape_and_custom_tag_are_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_TAG", "tag:custom-bridge")
    payload = _status()
    payload["Peer"] = [_peer("100.64.0.6", tags=["tag:custom-bridge"])]
    manager = NetworkManager(tmp_path / "state")
    discovery = TailscaleDiscovery(manager, 18443, _runner(payload), lambda _url: _identity("custom"))

    assert discovery.scan_once()["candidate_count"] == 1
    assert manager.network_status()["candidates"][0]["peer_id"] == "custom"


def test_unreachable_tagged_peer_degrades_without_exposing_address(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    payload = _status(_peer("100.64.0.77"))
    discovery = TailscaleDiscovery(
        manager,
        18443,
        _runner(payload),
        lambda _url: (_ for _ in ()).throw(TimeoutError("100.64.0.77")),
    )

    result = discovery.scan_once()

    assert result["status"] == "degraded"
    assert result["probe_failure_count"] == 1
    assert result["last_error"] == "bridge_probe_unreachable"
    assert "100.64.0.77" not in json.dumps(result)


def test_scan_interval_is_bounded_and_invalid_values_fall_back(tmp_path, monkeypatch):
    manager = NetworkManager(tmp_path / "state")
    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_SCAN_INTERVAL", "1")
    assert TailscaleDiscovery(manager, 18443, _runner(_status()), lambda _url: _identity()).interval_seconds == 10

    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_SCAN_INTERVAL", "9999")
    assert TailscaleDiscovery(manager, 18443, _runner(_status()), lambda _url: _identity()).interval_seconds == 300

    monkeypatch.setenv("HERMES_BRIDGE_TAILSCALE_SCAN_INTERVAL", "invalid")
    assert TailscaleDiscovery(manager, 18443, _runner(_status()), lambda _url: _identity()).interval_seconds == 30


def test_real_discovery_cannot_start_in_test_sandbox(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    discovery = TailscaleDiscovery(manager, 18443, _runner(_status()), lambda _url: _identity())

    with pytest.raises(RuntimeError, match="forbidden in the test sandbox"):
        discovery.start()


def test_pairing_source_accepts_tailscale_ranges_only_when_enabled(tmp_path, monkeypatch):
    manager = NetworkManager(tmp_path / "state")
    app = NetworkASGI(None, manager)

    monkeypatch.setenv("HERMES_BRIDGE_DISCOVERY_BACKEND", "tailscale")
    app._check_rate({"client": ("100.64.0.8", 12345)})
    app._check_rate({"client": ("fd7a:115c:a1e0::8", 12345)})
    with pytest.raises(ValueError, match="local-network"):
        app._check_rate({"client": ("100.115.92.8", 12345)})

    monkeypatch.setenv("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns")
    with pytest.raises(ValueError, match="local-network"):
        app._check_rate({"client": ("100.64.0.9", 12345)})
    with pytest.raises(ValueError, match="local-network"):
        app._check_rate({"client": ("fd7a:115c:a1e0::9", 12345)})


def test_public_health_contains_no_tailnet_inventory(tmp_path):
    manager = NetworkManager(tmp_path / "state")
    payload = _status(_peer("100.64.0.2"))
    discovery = TailscaleDiscovery(manager, 18443, _runner(payload), lambda _url: _identity("secret-hostname"))

    discovery.scan_once()
    health = manager.network_status()["discovery_health"]
    rendered = json.dumps(health)

    assert health["backend"] == "tailscale"
    assert health["eligible_peer_count"] == 1
    assert "100.64" not in rendered
    assert "secret-hostname" not in rendered
