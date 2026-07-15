from __future__ import annotations

import base64
import contextlib
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import ssl
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


NETWORK_PROTOCOL_VERSION = "1"
NETWORK_EXTENSION = "automatic_pairing_v1"
PRODUCTION_MDNS_TYPE = "_hermes-bridge._tcp.local."
PRODUCTION_PORTS = {18082, 18083, 18084, 18443}
DEFAULT_SECURE_PORT = 18443
DEFAULT_TAILSCALE_TAG = "tag:hermes-bridge"
DEFAULT_TAILSCALE_SCAN_INTERVAL = 30.0
DEFAULT_TAILSCALE_PROVIDER = "auto"
DEFAULT_TAILSCALE_API_BASE = "https://api.tailscale.com/api/v2"
MAX_TAILSCALE_DISCOVERY_PEERS = 64
TAILSCALE_IPV4_RANGE = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_CHROMEOS_RANGE = ipaddress.ip_network("100.115.92.0/23")
TAILSCALE_IPV6_RANGE = ipaddress.ip_network("fd7a:115c:a1e0::/48")
MAX_NETWORK_BODY = 64 * 1024
PAIR_REQUEST_TTL_SECONDS = 300
CANDIDATE_TTL_SECONDS = 180
PEER_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")


def _now() -> float:
    return time.time()


def _canonical(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _safe_peer_id(value: Any) -> str:
    peer_id = str(value or "").strip()
    if not PEER_ID_RE.fullmatch(peer_id):
        raise ValueError("peer_id must be 1-96 letters, numbers, dot, colon, underscore, or dash")
    return peer_id


def _validate_peer_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port is None or parsed.path != "/mcp":
        raise ValueError("managed peer URL must be https://host:port/mcp")
    return url


def _validate_remote_peer_url(value: Any) -> str:
    """Accept a peer callback endpoint only when it cannot target this host.

    Loopback is valid in the isolated test harness but never for a production
    pairing offer: persisting it creates a successful-looking one-way pair.
    """
    url = _validate_peer_url(value)
    host = urlparse(url).hostname or ""
    try:
        if ipaddress.ip_address(host).is_loopback and os.environ.get("HERMES_BRIDGE_TEST_SANDBOX") != "1":
            raise ValueError("remote pairing endpoint must not use a loopback address")
    except ValueError as exc:
        if "loopback" in str(exc):
            raise
    return url


def _validate_token(value: Any) -> str:
    token = str(value or "")
    if len(token) < 32 or len(token) > 256:
        raise ValueError("pairing credential has an invalid length")
    return token


def _is_tailscale_ip(value: Any) -> bool:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address in TAILSCALE_IPV4_RANGE and address not in TAILSCALE_CHROMEOS_RANGE
    return address in TAILSCALE_IPV6_RANGE


def _url_host(address: str) -> str:
    parsed = ipaddress.ip_address(str(address).split("%", 1)[0])
    return f"[{parsed}]" if isinstance(parsed, ipaddress.IPv6Address) else str(parsed)


def _fingerprint_cert(cert_pem: str | bytes) -> str:
    raw = cert_pem.encode("utf-8") if isinstance(cert_pem, str) else cert_pem
    cert = x509.load_pem_x509_certificate(raw)
    return cert.fingerprint(hashes.SHA256()).hex()


def _public_key_from_cert(cert_pem: str | bytes):
    raw = cert_pem.encode("utf-8") if isinstance(cert_pem, str) else cert_pem
    return x509.load_pem_x509_certificate(raw).public_key()


def _verify_signature(cert_pem: str, payload: dict[str, Any], signature: str) -> None:
    key = _public_key_from_cert(cert_pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("unsupported identity key")
    key.verify(_unb64(signature), _canonical(payload), ec.ECDSA(hashes.SHA256()))


def _restricted_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


class InterProcessFileLock:
    """Small standard-library lock used by both local and peer bridge processes."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.02)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.handle:
            return
        self.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class AtomicJsonStore:
    def __init__(self, path: Path, default_factory: Callable[[], dict[str, Any]]):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.default_factory = default_factory

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else self.default_factory()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return self.default_factory()

    def read(self) -> dict[str, Any]:
        with InterProcessFileLock(self.lock_path):
            return self._read_unlocked()

    def mutate(self, callback: Callable[[dict[str, Any]], Any]) -> Any:
        with InterProcessFileLock(self.lock_path):
            data = self._read_unlocked()
            result = callback(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _restricted_write(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            return result


@dataclass(frozen=True)
class DeviceIdentity:
    peer_id: str
    display_name: str
    fingerprint: str
    cert_pem: str
    cert_path: Path
    key_path: Path


class IdentityStore:
    def __init__(self, state_dir: Path):
        self.root = state_dir / "network"
        self.key_path = self.root / "identity-key.pem"
        self.cert_path = self.root / "identity-cert.pem"
        self.meta_path = self.root / "identity.json"
        self.lock_path = self.root / "identity.lock"

    def ensure(self) -> DeviceIdentity:
        with InterProcessFileLock(self.lock_path):
            if not (self.key_path.exists() and self.cert_path.exists() and self.meta_path.exists()):
                self._create()
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            cert_pem = self.cert_path.read_text(encoding="utf-8")
            fingerprint = _fingerprint_cert(cert_pem)
            if fingerprint != meta.get("fingerprint"):
                raise ValueError("identity certificate fingerprint does not match metadata")
            return DeviceIdentity(
                peer_id=_safe_peer_id(meta["peer_id"]),
                display_name=str(meta.get("display_name") or meta["peer_id"]),
                fingerprint=fingerprint,
                cert_pem=cert_pem,
                cert_path=self.cert_path,
                key_path=self.key_path,
            )

    def _create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        configured = os.environ.get("HERMES_BRIDGE_PEER_ID")
        peer_id = _safe_peer_id(configured) if configured else f"hermes-{uuid.uuid4().hex[:16]}"
        display_name = os.environ.get("HERMES_BRIDGE_DISPLAY_NAME") or platform.node() or peer_id
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, peer_id)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(peer_id), x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        _restricted_write(self.key_path, key_pem)
        _restricted_write(self.cert_path, cert_pem)
        _restricted_write(
            self.meta_path,
            json.dumps(
                {
                    "peer_id": peer_id,
                    "display_name": display_name,
                    "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
                    "created_at": _now(),
                },
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )

    def sign(self, payload: dict[str, Any]) -> str:
        self.ensure()
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError("unsupported identity private key")
        return _b64(key.sign(_canonical(payload), ec.ECDSA(hashes.SHA256())))


def _empty_network_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "peers": {},
        "candidates": {},
        "rejected": {},
        "revoked": {},
        "used_nonces": {},
        "approval_windows": {},
    }


class PairingState:
    def __init__(self, state_dir: Path):
        self.store = AtomicJsonStore(state_dir / "network" / "paired-peers.json", _empty_network_state)
        self.revocations = AtomicJsonStore(
            state_dir / "network" / "revocations.json",
            lambda: {"schema_version": 1, "revoked": {}},
        )

    def _revoked(self) -> dict[str, Any]:
        data = self.revocations.read()
        revoked = data.get("revoked", {})
        return revoked if isinstance(revoked, dict) else {}

    @staticmethod
    def _prune(data: dict[str, Any]) -> None:
        now = _now()
        data.setdefault("peers", {})
        data.setdefault("candidates", {})
        data.setdefault("rejected", {})
        data.setdefault("revoked", {})
        data.setdefault("used_nonces", {})
        data.setdefault("approval_windows", {})
        for key, value in list(data["candidates"].items()):
            if float(value.get("expires_at", 0)) < now:
                data["candidates"].pop(key, None)
        for key, expiry in list(data["used_nonces"].items()):
            if float(expiry) < now:
                data["used_nonces"].pop(key, None)
        for key, window in list(data["approval_windows"].items()):
            if float(window.get("expires_at", 0)) < now:
                data["approval_windows"].pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        data = self.store.read()
        self._prune(data)
        data["revoked"] = {**data.get("revoked", {}), **self._revoked()}
        return data

    def ingest_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        peer_id = _safe_peer_id(candidate.get("peer_id"))
        fingerprint = str(candidate.get("fingerprint") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("candidate fingerprint must be SHA-256 hex")
        url = _validate_peer_url(candidate.get("url"))
        public = {
            "peer_id": peer_id,
            "display_name": str(candidate.get("display_name") or peer_id)[:128],
            "fingerprint": fingerprint,
            "url": url,
            "platform": str(candidate.get("platform") or "unknown")[:32],
            "bridge_version": str(candidate.get("bridge_version") or "unknown")[:32],
            "protocol_version": str(candidate.get("protocol_version") or "")[:16],
            "source": str(candidate.get("source") or "discovery")[:32],
            "last_seen": _now(),
            "expires_at": _now() + CANDIDATE_TTL_SECONDS,
        }
        durable_revoked = self._revoked()

        def update(data: dict[str, Any]):
            self._prune(data)
            peer = data["peers"].get(peer_id)
            if peer:
                if peer.get("fingerprint") != fingerprint:
                    public["conflict"] = "peer_id is already pinned to another identity"
                    data["candidates"][f"{peer_id}:{fingerprint}"] = public
                else:
                    peer["candidate_url"] = url
                    peer["last_seen"] = public["last_seen"]
                return public
            if fingerprint in durable_revoked or fingerprint in data["revoked"]:
                public["revoked"] = True
            data["candidates"][f"{peer_id}:{fingerprint}"] = public
            return public

        return self.store.mutate(update)

    def consume_nonce(self, nonce: str, expires_at: float) -> None:
        if not isinstance(nonce, str) or len(nonce) < 24 or len(nonce) > 128:
            raise ValueError("invalid pairing nonce")
        now = _now()
        if expires_at < now or expires_at > now + PAIR_REQUEST_TTL_SECONDS + 30:
            raise ValueError("pairing request is expired or has an invalid lifetime")

        def update(data: dict[str, Any]):
            self._prune(data)
            if nonce in data["used_nonces"]:
                raise ValueError("pairing nonce has already been used")
            data["used_nonces"][nonce] = expires_at

        self.store.mutate(update)

    def candidate(self, peer_id: str, fingerprint: Optional[str] = None) -> Optional[dict[str, Any]]:
        peer_id = _safe_peer_id(peer_id)
        data = self.snapshot()
        candidates = [
            value for value in data["candidates"].values()
            if value.get("peer_id") == peer_id and (not fingerprint or value.get("fingerprint") == fingerprint.lower())
        ]
        candidates.sort(key=lambda value: float(value.get("last_seen", 0)), reverse=True)
        return candidates[0] if candidates else None

    def save_peer(self, peer: dict[str, Any]) -> dict[str, Any]:
        peer_id = _safe_peer_id(peer.get("peer_id"))
        fingerprint = str(peer.get("fingerprint") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("peer fingerprint must be SHA-256 hex")
        required = ("cert_pem", "url", "inbound_token", "outbound_token", "receipt")
        if any(not peer.get(field) for field in required):
            raise ValueError("managed peer record is incomplete")
        if _fingerprint_cert(str(peer["cert_pem"])) != fingerprint:
            raise ValueError("managed peer certificate does not match its pinned fingerprint")
        record = dict(peer)
        record.update({
            "peer_id": peer_id,
            "fingerprint": fingerprint,
            "url": _validate_peer_url(peer["url"]),
            "inbound_token": _validate_token(peer["inbound_token"]),
            "outbound_token": _validate_token(peer["outbound_token"]),
            "status": "paired",
            "updated_at": _now(),
        })
        if fingerprint in self._revoked():
            raise ValueError("identity is revoked")

        def update(data: dict[str, Any]):
            self._prune(data)
            if fingerprint in data["revoked"]:
                raise ValueError("identity is revoked")
            existing = data["peers"].get(peer_id)
            if existing and existing.get("fingerprint") != fingerprint:
                raise ValueError("peer_id is pinned to another identity")
            data["peers"][peer_id] = record
            for key, candidate in list(data["candidates"].items()):
                if candidate.get("peer_id") == peer_id:
                    data["candidates"].pop(key, None)
            return record

        return self.store.mutate(update)

    def reject(self, peer_id: str, fingerprint: Optional[str] = None) -> dict[str, Any]:
        peer_id = _safe_peer_id(peer_id)

        def update(data: dict[str, Any]):
            self._prune(data)
            removed = []
            for key, candidate in list(data["candidates"].items()):
                if candidate.get("peer_id") == peer_id and (not fingerprint or candidate.get("fingerprint") == fingerprint):
                    removed.append(candidate.get("fingerprint"))
                    data["candidates"].pop(key, None)
            data["rejected"][peer_id] = {"fingerprints": removed, "rejected_at": _now()}
            return {"status": "rejected", "peer_id": peer_id, "fingerprints": removed}

        return self.store.mutate(update)

    def revoke(self, peer_id: str) -> dict[str, Any]:
        peer_id = _safe_peer_id(peer_id)

        peer = self.peer(peer_id)
        if not peer:
            raise ValueError(f"paired peer not found: {peer_id}")
        revocation = {"peer_id": peer_id, "revoked_at": _now()}

        def persist_revocation(data: dict[str, Any]):
            data.setdefault("revoked", {})[peer["fingerprint"]] = revocation

        # Persist denial first so an interrupted revoke fails closed.
        self.revocations.mutate(persist_revocation)

        def update(data: dict[str, Any]):
            self._prune(data)
            data["peers"].pop(peer_id, None)
            data["revoked"][peer["fingerprint"]] = revocation
            return {"status": "revoked", "peer_id": peer_id, "fingerprint": peer["fingerprint"]}

        return self.store.mutate(update)

    def peer(self, peer_id: str) -> Optional[dict[str, Any]]:
        return self.snapshot()["peers"].get(_safe_peer_id(peer_id))

    def inbound_tokens(self) -> list[str]:
        return [str(peer["inbound_token"]) for peer in self.snapshot()["peers"].values() if peer.get("inbound_token")]

    def open_approval_window(self, peer_id: str, fingerprint: str, ttl_seconds: int = 300) -> dict[str, Any]:
        peer_id = _safe_peer_id(peer_id)
        fingerprint = str(fingerprint or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("expected_fingerprint must be SHA-256 hex")
        expires_at = _now() + max(30, min(int(ttl_seconds), PAIR_REQUEST_TTL_SECONDS))
        def update(data: dict[str, Any]):
            self._prune(data)
            data["approval_windows"][f"{peer_id}:{fingerprint}"] = {"expires_at": expires_at}
            return {"status": "open", "peer_id": peer_id, "fingerprint": fingerprint, "expires_at": expires_at}
        return self.store.mutate(update)

    def consume_approval_window(self, peer_id: str, fingerprint: str) -> None:
        key = f"{_safe_peer_id(peer_id)}:{str(fingerprint).lower()}"
        def update(data: dict[str, Any]):
            self._prune(data)
            if key not in data["approval_windows"]:
                raise ValueError("no active approval window matches this identity")
            data["approval_windows"].pop(key, None)
        self.store.mutate(update)

    def public_status(self) -> dict[str, Any]:
        data = self.snapshot()
        peers = []
        for peer in data["peers"].values():
            peers.append({
                "peer_id": peer.get("peer_id"),
                "display_name": peer.get("display_name"),
                "fingerprint": peer.get("fingerprint"),
                "url": peer.get("url"),
                "platform": peer.get("platform"),
                "status": peer.get("status", "paired"),
                "last_seen": peer.get("last_seen"),
                "updated_at": peer.get("updated_at"),
                "credential_configured": bool(peer.get("inbound_token") and peer.get("outbound_token")),
                "certificate_pinned": bool(peer.get("cert_pem")),
            })
        return {
            "paired_peers": sorted(peers, key=lambda item: item["peer_id"] or ""),
            "candidates": sorted(data["candidates"].values(), key=lambda item: item.get("peer_id", "")),
            "revoked_fingerprints": sorted(data["revoked"].keys()),
        }


def _pinned_ssl_context(cert_pem: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=cert_pem)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class NetworkManager:
    def __init__(self, state_dir: Path, secure_port: int = DEFAULT_SECURE_PORT):
        self.state_dir = Path(state_dir).expanduser()
        self.identities = IdentityStore(self.state_dir)
        self.state = PairingState(self.state_dir)
        self.secure_port = int(secure_port)
        self._runtime_advertise_address: Optional[str] = None
        self._discovery_status_provider: Optional[Callable[[], dict[str, Any]]] = None

    @property
    def identity(self) -> DeviceIdentity:
        return self.identities.ensure()

    def identity_document(self) -> dict[str, Any]:
        identity = self.identity
        doc = {
            "protocol_version": NETWORK_PROTOCOL_VERSION,
            "peer_id": identity.peer_id,
            "display_name": identity.display_name,
            "fingerprint": identity.fingerprint,
            "cert_pem": identity.cert_pem,
            "platform": platform.system().lower() or "unknown",
            "bridge_version": os.environ.get("HERMES_BRIDGE_VERSION", "v1.3.0"),
        }
        doc["signature"] = self.identities.sign(doc)
        return doc

    def network_status(self) -> dict[str, Any]:
        identity = self.identity
        public = self.state.public_status()
        public.update({
            "extension": NETWORK_EXTENSION,
            "protocol_version": NETWORK_PROTOCOL_VERSION,
            "local_peer_id": identity.peer_id,
            "display_name": identity.display_name,
            "identity_fingerprint": identity.fingerprint,
            "secure_port": self.secure_port,
            "discovery_enabled": os.environ.get("HERMES_BRIDGE_AUTO_DISCOVERY", "1") != "0",
            "discovery_backend": os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns"),
            "discovery_namespace": os.environ.get("HERMES_BRIDGE_DISCOVERY_NAMESPACE", "production"),
            "tailscale_advertise_address_configured": bool(
                os.environ.get("HERMES_BRIDGE_ADVERTISE_ADDRESS") or self._runtime_advertise_address
            ),
        })
        if self._discovery_status_provider:
            public["discovery_health"] = self._discovery_status_provider()
        return public

    def set_discovery_status_provider(self, provider: Callable[[], dict[str, Any]]) -> None:
        self._discovery_status_provider = provider

    def set_runtime_advertise_address(self, address: str) -> None:
        parsed = ipaddress.ip_address(address)
        if parsed.is_loopback or parsed.is_unspecified:
            raise ValueError("runtime advertise address must be routable")
        self._runtime_advertise_address = str(parsed)

    def managed_peer_config(self) -> dict[str, dict[str, Any]]:
        return {
            peer_id: {
                "peer_id": peer_id,
                "url": peer["url"],
                "platform": peer.get("platform", "unknown"),
                "token": peer["outbound_token"],
                "cert_pem": peer["cert_pem"],
                "fingerprint": peer["fingerprint"],
                "managed": True,
            }
            for peer_id, peer in self.state.snapshot()["peers"].items()
        }

    def inspect_identity(self, url: str, timeout: float = 10.0) -> dict[str, Any]:
        base = _validate_peer_url(url).rsplit("/mcp", 1)[0]
        with httpx.Client(verify=False, timeout=timeout, trust_env=False) as client:
            response = client.get(f"{base}/bridge/v1/identity")
            response.raise_for_status()
            document = response.json()
        signature = document.pop("signature", "")
        cert_pem = str(document.get("cert_pem") or "")
        fingerprint = _fingerprint_cert(cert_pem)
        if fingerprint != str(document.get("fingerprint") or ""):
            raise ValueError("identity certificate does not match its fingerprint")
        _verify_signature(cert_pem, document, signature)
        document["signature"] = signature
        document["url"] = url
        return document

    def _fetch_identity(self, candidate: dict[str, Any]) -> dict[str, Any]:
        document = self.inspect_identity(candidate["url"])
        if document["fingerprint"] != candidate["fingerprint"]:
            raise ValueError("discovered certificate does not match advertised fingerprint")
        if document.get("peer_id") != candidate["peer_id"]:
            raise ValueError("discovered peer_id does not match identity document")
        return document

    def approve(self, peer_id: str, expected_fingerprint: Optional[str] = None) -> dict[str, Any]:
        candidate = self.state.candidate(peer_id, expected_fingerprint)
        if not candidate:
            raise ValueError(f"discovery candidate not found: {peer_id}")
        if candidate.get("conflict") or candidate.get("revoked"):
            raise ValueError(candidate.get("conflict") or "candidate identity is revoked")
        remote = self._fetch_identity(candidate)
        identity = self.identity
        token_for_remote = secrets.token_urlsafe(32)
        expires_at = _now() + PAIR_REQUEST_TTL_SECONDS
        # Single shared token model (v1.2.7+): both directions use the same token.
        # The dual-directional token model (separate inbound/outbound) is deprecated
        # but the field names are retained for backward compatibility.
        receipt = {
            "version": 1,
            "left_peer_id": identity.peer_id,
            "left_fingerprint": identity.fingerprint,
            "right_peer_id": remote["peer_id"],
            "right_fingerprint": remote["fingerprint"],
            "created_at": _now(),
            "epoch": secrets.token_hex(8),
        }
        offer = {
            "protocol_version": NETWORK_PROTOCOL_VERSION,
            "approved": True,
            "nonce": secrets.token_urlsafe(24),
            "expires_at": expires_at,
            "peer_id": identity.peer_id,
            "display_name": identity.display_name,
            "fingerprint": identity.fingerprint,
            "cert_pem": identity.cert_pem,
            "platform": platform.system().lower() or "unknown",
            "url": self._advertised_url(),
            "token_for_remote": token_for_remote,
            "receipt": receipt,
            "receipt_signature": self.identities.sign(receipt),
        }
        offer["signature"] = self.identities.sign(offer)
        base = candidate["url"].rsplit("/mcp", 1)[0]
        context = _pinned_ssl_context(remote["cert_pem"])
        with httpx.Client(verify=context, timeout=15.0, trust_env=False) as client:
            response = client.post(f"{base}/bridge/v1/pair", json=offer)
            response.raise_for_status()
            accepted = response.json()
        accepted_signature = accepted.pop("signature", "")
        _verify_signature(remote["cert_pem"], accepted, accepted_signature)
        if accepted.get("nonce") != offer["nonce"] or accepted.get("status") != "paired":
            raise ValueError("remote pairing response did not match the approved request")
        responder_url = _validate_remote_peer_url(accepted.get("url"))
        responder_identity = self.inspect_identity(responder_url)
        if responder_identity.get("fingerprint") != remote["fingerprint"]:
            raise ValueError("remote pairing response advertised an endpoint for another identity")
        final_receipt = dict(receipt)
        final_receipt["left_signature"] = offer["receipt_signature"]
        final_receipt["right_signature"] = accepted["receipt_signature"]
        peer = self.state.save_peer({
            "peer_id": remote["peer_id"],
            "display_name": remote.get("display_name"),
            "fingerprint": remote["fingerprint"],
            "cert_pem": remote["cert_pem"],
            "url": responder_url,
            "platform": remote.get("platform", "unknown"),
            "inbound_token": token_for_remote,
            "outbound_token": token_for_remote,  # Single shared token (v1.2.7+)
            "receipt": final_receipt,
            "last_seen": _now(),
        })
        return self._public_pair_result(peer, "paired")

    def accept_pair_offer(self, offer: dict[str, Any]) -> dict[str, Any]:
        offer = dict(offer)
        signature = str(offer.pop("signature", ""))
        cert_pem = str(offer.get("cert_pem") or "")
        fingerprint = _fingerprint_cert(cert_pem)
        if fingerprint != str(offer.get("fingerprint") or ""):
            raise ValueError("pairing certificate fingerprint mismatch")
        if offer.get("approved") is not True:
            raise ValueError("pairing request lacks explicit approval")
        _verify_signature(cert_pem, offer, signature)
        self.state.consume_nonce(str(offer.get("nonce") or ""), float(offer.get("expires_at") or 0))
        peer_id = _safe_peer_id(offer.get("peer_id"))
        if peer_id == self.identity.peer_id:
            raise ValueError("cannot pair an identity with itself")
        receipt = dict(offer.get("receipt") or {})
        _verify_signature(cert_pem, receipt, str(offer.get("receipt_signature") or ""))
        if receipt.get("left_peer_id") != peer_id or receipt.get("left_fingerprint") != fingerprint:
            raise ValueError("pairing receipt does not match the initiating identity")
        if receipt.get("right_peer_id") != self.identity.peer_id or receipt.get("right_fingerprint") != self.identity.fingerprint:
            raise ValueError("pairing receipt is not addressed to this identity")
        # Single shared token model (v1.2.7+): use the token from the offer for both directions.
        shared_token = _validate_token(offer.get("token_for_remote"))
        right_signature = self.identities.sign(receipt)
        final_receipt = dict(receipt)
        final_receipt["left_signature"] = offer["receipt_signature"]
        final_receipt["right_signature"] = right_signature
        self.state.save_peer({
            "peer_id": peer_id,
            "display_name": offer.get("display_name"),
            "fingerprint": fingerprint,
            "cert_pem": cert_pem,
            "url": _validate_remote_peer_url(offer.get("url")),
            "platform": offer.get("platform", "unknown"),
            "inbound_token": shared_token,
            "outbound_token": shared_token,  # Single shared token (v1.2.7+)
            "receipt": final_receipt,
            "last_seen": _now(),
        })
        response = {
            "status": "paired",
            "nonce": offer["nonce"],
            "peer_id": self.identity.peer_id,
            "fingerprint": self.identity.fingerprint,
            "token_for_initiator": shared_token,  # Same token both directions
            "receipt_signature": right_signature,
            "url": self._advertised_url(),
        }
        response["signature"] = self.identities.sign(response)
        return response

    def _verify_receipt(self, receipt: dict[str, Any], remote_cert_pem: str) -> None:
        payload = {key: value for key, value in receipt.items() if key not in {"left_signature", "right_signature"}}
        local = self.identity
        if payload.get("left_fingerprint") == local.fingerprint:
            left_cert, right_cert = local.cert_pem, remote_cert_pem
        elif payload.get("right_fingerprint") == local.fingerprint:
            left_cert, right_cert = remote_cert_pem, local.cert_pem
        else:
            raise ValueError("recovery receipt does not include this identity")
        _verify_signature(left_cert, payload, str(receipt.get("left_signature") or ""))
        _verify_signature(right_cert, payload, str(receipt.get("right_signature") or ""))

    def accept_rekey(self, request: dict[str, Any]) -> dict[str, Any]:
        request = dict(request)
        signature = str(request.pop("signature", ""))
        cert_pem = str(request.get("cert_pem") or "")
        fingerprint = _fingerprint_cert(cert_pem)
        if fingerprint != request.get("fingerprint"):
            raise ValueError("rekey certificate fingerprint mismatch")
        _verify_signature(cert_pem, request, signature)
        self.state.consume_nonce(str(request.get("nonce") or ""), float(request.get("expires_at") or 0))
        peer_id = _safe_peer_id(request.get("peer_id"))
        receipt = dict(request.get("receipt") or {})
        self._verify_receipt(receipt, cert_pem)
        receipt_ids = {receipt.get("left_peer_id"), receipt.get("right_peer_id")}
        receipt_fingerprints = {receipt.get("left_fingerprint"), receipt.get("right_fingerprint")}
        if peer_id not in receipt_ids or fingerprint not in receipt_fingerprints:
            raise ValueError("rekey identity does not match the signed receipt")
        # Single shared token model (v1.2.7+): reuse the initiator's token
        # for both directions, exactly like approve()/accept_pair_offer().
        shared_token = _validate_token(request.get("token_for_remote"))
        self.state.save_peer({
            "peer_id": peer_id,
            "display_name": request.get("display_name"),
            "fingerprint": fingerprint,
            "cert_pem": cert_pem,
            "url": _validate_peer_url(request.get("url")),
            "platform": request.get("platform", "unknown"),
            "inbound_token": shared_token,
            "outbound_token": shared_token,  # Single shared token (v1.2.7+)
            "receipt": receipt,
            "last_seen": _now(),
        })
        response = {
            "status": "rekeyed",
            "nonce": request["nonce"],
            "peer_id": self.identity.peer_id,
            "fingerprint": self.identity.fingerprint,
            "token_for_requester": shared_token,
        }
        response["signature"] = self.identities.sign(response)
        return response

    def rekey_peer(self, peer: dict[str, Any], endpoint: Optional[str] = None) -> dict[str, Any]:
        endpoint = endpoint or peer["url"]
        token_for_remote = secrets.token_urlsafe(32)
        request = {
            "protocol_version": NETWORK_PROTOCOL_VERSION,
            "nonce": secrets.token_urlsafe(24),
            "expires_at": _now() + PAIR_REQUEST_TTL_SECONDS,
            "peer_id": self.identity.peer_id,
            "display_name": self.identity.display_name,
            "fingerprint": self.identity.fingerprint,
            "cert_pem": self.identity.cert_pem,
            "platform": platform.system().lower() or "unknown",
            "url": self._advertised_url(),
            "token_for_remote": token_for_remote,
            "receipt": peer["receipt"],
        }
        request["signature"] = self.identities.sign(request)
        context = _pinned_ssl_context(peer["cert_pem"])
        base = endpoint.rsplit("/mcp", 1)[0]
        with httpx.Client(verify=context, timeout=15.0, trust_env=False) as client:
            response = client.post(f"{base}/bridge/v1/rekey", json=request)
            response.raise_for_status()
            accepted = response.json()
        accepted_signature = accepted.pop("signature", "")
        _verify_signature(peer["cert_pem"], accepted, accepted_signature)
        if accepted.get("nonce") != request["nonce"] or accepted.get("status") != "rekeyed":
            raise ValueError("remote rekey response did not match the request")
        updated = dict(peer)
        updated.update({
            "url": endpoint,
            "inbound_token": token_for_remote,
            "outbound_token": token_for_remote,  # Single shared token (v1.2.7+)
            "last_seen": _now(),
        })
        return self.state.save_peer(updated)

    def pair_action(self, action: str, peer_id: str, expected_fingerprint: Optional[str] = None) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action == "approve":
            return self.approve(peer_id, expected_fingerprint)
        if action == "reject":
            return self.state.reject(peer_id, expected_fingerprint)
        if action == "revoke":
            return self.state.revoke(peer_id)
        if action == "reconnect":
            peer = self.state.peer(peer_id)
            if not peer:
                raise ValueError(f"paired peer not found: {peer_id}")
            candidate = self.state.candidate(peer_id, peer["fingerprint"])
            endpoint = candidate["url"] if candidate else (peer.get("candidate_url") or peer["url"])
            peer = self.rekey_peer(peer, endpoint)
            return self._public_pair_result(peer, "reconnect_scheduled")
        raise ValueError("action must be approve, reject, revoke, or reconnect")

    def pairing_window(self, action: str, peer_id: str, expected_fingerprint: str, ttl_seconds: int = 300) -> dict[str, Any]:
        action = str(action or "").lower()
        if action == "open":
            return self.state.open_approval_window(peer_id, expected_fingerprint, ttl_seconds)
        if action == "approve":
            self.state.consume_approval_window(peer_id, expected_fingerprint)
            return self.approve(peer_id, expected_fingerprint)
        raise ValueError("action must be open or approve")

    def _advertised_url(self) -> str:
        address = os.environ.get("HERMES_BRIDGE_ADVERTISE_ADDRESS") or self._runtime_advertise_address
        if not address:
            if os.environ.get("HERMES_BRIDGE_TEST_SANDBOX") == "1":
                address = "127.0.0.1"
            else:
                raise ValueError("no routable advertised address is available")
        try:
            address = _url_host(address)
        except ValueError:
            pass
        return f"https://{address}:{self.secure_port}/mcp"

    @staticmethod
    def _public_pair_result(peer: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "status": status,
            "peer_id": peer.get("peer_id"),
            "display_name": peer.get("display_name"),
            "fingerprint": peer.get("fingerprint"),
            "url": peer.get("url"),
            "certificate_pinned": bool(peer.get("cert_pem")),
            "credential_configured": bool(peer.get("inbound_token") and peer.get("outbound_token")),
        }

    def introductions(self) -> dict[str, Any]:
        records = []
        for peer in self.state.public_status()["paired_peers"]:
            records.append({key: peer.get(key) for key in ("peer_id", "display_name", "fingerprint", "url", "platform")})
        payload = {"source_peer_id": self.identity.peer_id, "generated_at": _now(), "peers": records}
        payload["signature"] = self.identities.sign(payload)
        return payload

    def refresh_trusted_introductions(self) -> dict[str, Any]:
        discovered = 0
        errors: dict[str, str] = {}
        for peer_id, peer in self.state.snapshot()["peers"].items():
            try:
                context = _pinned_ssl_context(peer["cert_pem"])
                base = peer["url"].rsplit("/mcp", 1)[0]
                with httpx.Client(
                    verify=context,
                    headers={"Authorization": f"Bearer {peer['outbound_token']}"},
                    timeout=10.0,
                    trust_env=False,
                ) as client:
                    response = client.get(f"{base}/bridge/v1/introductions")
                    response.raise_for_status()
                    payload = response.json()
                signature = payload.pop("signature", "")
                _verify_signature(peer["cert_pem"], payload, signature)
                if payload.get("source_peer_id") != peer_id:
                    raise ValueError("introduction source does not match pinned peer")
                for record in payload.get("peers", []):
                    if record.get("peer_id") == self.identity.peer_id:
                        continue
                    candidate = dict(record)
                    candidate.update({
                        "source": "trusted_introduction",
                        "protocol_version": NETWORK_PROTOCOL_VERSION,
                        "bridge_version": "unknown",
                    })
                    self.state.ingest_candidate(candidate)
                    discovered += 1
            except Exception as exc:
                errors[peer_id] = f"{type(exc).__name__}: {exc}"
        return {"introduced_candidates": discovered, "errors": errors}

    def recover_known_endpoints(self) -> dict[str, Any]:
        recovered: list[str] = []
        errors: dict[str, str] = {}
        for peer_id, peer in self.state.snapshot()["peers"].items():
            endpoint = peer.get("candidate_url")
            if not endpoint or endpoint == peer.get("url"):
                continue
            try:
                self.rekey_peer(peer, endpoint)
                recovered.append(peer_id)
            except Exception as exc:
                errors[peer_id] = f"{type(exc).__name__}: {exc}"
        return {"recovered_peers": recovered, "errors": errors}


class NetworkASGI:
    """Adds bounded pairing routes while delegating MCP and lifespan to FastMCP."""

    def __init__(self, app: Any, manager: NetworkManager):
        self.app = app
        self.manager = manager
        self._rate_lock = threading.Lock()
        self._rate_events: dict[str, list[float]] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/bridge/v1/identity" and scope.get("method") == "GET":
            await self._json(send, 200, self.manager.identity_document())
            return
        if path in {"/bridge/v1/pair", "/bridge/v1/rekey"} and scope.get("method") == "POST":
            try:
                self._check_rate(scope)
                body = await self._read_body(receive)
                payload = json.loads(body.decode("utf-8"))
                result = self.manager.accept_pair_offer(payload) if path.endswith("/pair") else self.manager.accept_rekey(payload)
                await self._json(send, 200, result)
            except Exception as exc:
                await self._json(send, 400, {"error": str(exc), "error_type": type(exc).__name__})
            return
        if path == "/bridge/v1/introductions" and scope.get("method") == "GET":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            token = headers.get("authorization", "").removeprefix("Bearer ")
            if not token or not secrets.compare_digest(token, next((item for item in self.manager.state.inbound_tokens() if secrets.compare_digest(item, token)), "")):
                await self._json(send, 401, {"error": "unauthorized"})
                return
            await self._json(send, 200, self.manager.introductions())
            return
        await self.app(scope, receive, send)

    def _check_rate(self, scope: dict) -> None:
        client = scope.get("client") or ("unknown", 0)
        address = str(client[0])
        try:
            source = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("pairing source address is invalid") from exc
        tailscale_source = _is_tailscale_ip(source)
        tailscale_enabled = os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns") == "tailscale"
        local_source = (source.is_private or source.is_link_local or source.is_loopback) and not tailscale_source
        if not (local_source or (tailscale_enabled and tailscale_source)):
            raise ValueError("pairing requests are accepted only from local-network addresses")
        now = _now()
        with self._rate_lock:
            events = [stamp for stamp in self._rate_events.get(address, []) if stamp >= now - 60]
            if len(events) >= 10:
                raise ValueError("pairing rate limit exceeded")
            events.append(now)
            self._rate_events[address] = events

    @staticmethod
    async def _read_body(receive: Any) -> bytes:
        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > MAX_NETWORK_BODY:
                raise ValueError("request body is too large")
            more = bool(message.get("more_body"))
        return bytes(body)

    @staticmethod
    async def _json(send: Any, status: int, data: dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})


class InMemoryDiscovery:
    def __init__(self, manager: NetworkManager):
        self.manager = manager

    def publish(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = dict(candidate)
        candidate["source"] = "memory"
        return self.manager.state.ingest_candidate(candidate)


class TailscaleDiscovery:
    """Discovers explicitly tagged bridge nodes from Tailscale CLI or HTTP API inventory."""

    def __init__(
        self,
        manager: NetworkManager,
        port: int,
        command_runner: Optional[Callable[[list[str], float], Any]] = None,
        identity_fetcher: Optional[Callable[[str], dict[str, Any]]] = None,
        api_fetcher: Optional[Callable[[str, str, float], dict[str, Any]]] = None,
    ):
        self.manager = manager
        self.port = int(port)
        self.cli = os.environ.get("HERMES_BRIDGE_TAILSCALE_CLI", "tailscale")
        self.provider = os.environ.get("HERMES_BRIDGE_TAILSCALE_PROVIDER", DEFAULT_TAILSCALE_PROVIDER).strip().lower()
        self.tag = os.environ.get("HERMES_BRIDGE_TAILSCALE_TAG", DEFAULT_TAILSCALE_TAG).strip()
        self.api_token = os.environ.get("HERMES_BRIDGE_TAILSCALE_API_TOKEN", "").strip()
        self.tailnet = os.environ.get("HERMES_BRIDGE_TAILNET", "").strip()
        self.api_base = os.environ.get("HERMES_BRIDGE_TAILSCALE_API_BASE", DEFAULT_TAILSCALE_API_BASE).strip().rstrip("/")
        try:
            configured_interval = float(
                os.environ.get("HERMES_BRIDGE_TAILSCALE_SCAN_INTERVAL", DEFAULT_TAILSCALE_SCAN_INTERVAL)
            )
        except ValueError:
            configured_interval = DEFAULT_TAILSCALE_SCAN_INTERVAL
        self.interval_seconds = min(max(configured_interval, 10.0), 300.0)
        self.command_runner = command_runner or self._run_status_command
        self.api_fetcher = api_fetcher or self._run_api_request
        self.identity_fetcher = identity_fetcher or (lambda url: manager.inspect_identity(url, timeout=3.0))
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._health_lock = threading.Lock()
        self._health: dict[str, Any] = {
            "backend": "tailscale",
            "provider": self.provider,
            "status": "stopped",
            "scan_interval_seconds": self.interval_seconds,
            "eligible_peer_count": 0,
            "candidate_count": 0,
            "probe_failure_count": 0,
            "last_scan_at": None,
            "last_success_at": None,
            "last_error": None,
        }
        self.manager.set_discovery_status_provider(self.public_status)

    @staticmethod
    def _peer_records(status: dict[str, Any]) -> list[dict[str, Any]]:
        peers = status.get("Peer") or {}
        if isinstance(peers, dict):
            return [item for item in peers.values() if isinstance(item, dict)]
        if isinstance(peers, list):
            return [item for item in peers if isinstance(item, dict)]
        return []

    def _run_status_command(self, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, shell=False)

    @staticmethod
    def _timestamp_expired(value: Any) -> bool:
        if not value:
            return False
        try:
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp() < _now()
        except Exception:
            return False

    @staticmethod
    def _device_values(device: dict[str, Any], *names: str) -> list[Any]:
        for name in names:
            value = device.get(name)
            if isinstance(value, list):
                return value
        return []

    @staticmethod
    def _device_name_tokens(device: dict[str, Any]) -> set[str]:
        values = {
            str(device.get("hostname") or ""),
            str(device.get("name") or ""),
            str(device.get("dnsName") or ""),
        }
        tokens: set[str] = set()
        for value in values:
            lowered = value.strip().strip(".").lower()
            if lowered:
                tokens.add(lowered)
                tokens.add(lowered.split(".", 1)[0])
        return tokens

    def _local_name_tokens(self) -> set[str]:
        values = {
            platform.node(),
            self.manager.identity.display_name,
            self.manager.identity.peer_id,
            os.environ.get("HERMES_BRIDGE_DISPLAY_NAME", ""),
            os.environ.get("HERMES_BRIDGE_PEER_ID", ""),
        }
        return {str(value).strip().lower() for value in values if str(value).strip()}

    def _api_configured(self) -> bool:
        return bool(self.api_token and self.tailnet)

    def _run_api_request(self, url: str, token: str, timeout: float) -> dict[str, Any]:
        with httpx.Client(auth=(token, ""), timeout=timeout, trust_env=False) as client:
            response = client.get(url, headers={"accept": "application/json"})
            if response.status_code in {401, 403}:
                raise RuntimeError("tailscale_api_unauthorized")
            if response.status_code == 404:
                raise RuntimeError("tailscale_tailnet_not_found")
            response.raise_for_status()
            return response.json()

    def _load_cli_status(self) -> dict[str, Any]:
        result = self.command_runner([self.cli, "status", "--json"], 10.0)
        if int(getattr(result, "returncode", 1)) != 0:
            raise RuntimeError("Tailscale status command failed")
        try:
            status = json.loads(str(getattr(result, "stdout", "")))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Tailscale status returned invalid JSON") from exc
        if not isinstance(status, dict):
            raise RuntimeError("Tailscale status returned an invalid document")
        if status.get("BackendState") != "Running":
            raise RuntimeError("Tailscale is not running")
        return status

    def _api_url(self) -> str:
        parsed = urlparse(self.api_base)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("tailscale_api_base_invalid")
        query = urlencode({"fields": "addresses,tags,hostname,name,dnsName,os,lastSeen,expires,online,authorized"})
        return f"{self.api_base}/tailnet/{quote(self.tailnet, safe='')}/devices?{query}"

    def _api_device_to_peer(self, device: dict[str, Any]) -> dict[str, Any]:
        addresses = self._device_values(device, "addresses", "tailnetIPs", "TailscaleIPs")
        tags = self._device_values(device, "tags", "Tags")
        online = device.get("online", device.get("Online", True))
        return {
            "Online": online is not False,
            "Expired": bool(device.get("expired") or device.get("Expired")) or self._timestamp_expired(device.get("expires")),
            "Tags": [str(tag) for tag in tags],
            "TailscaleIPs": [str(address) for address in addresses],
        }

    def _load_api_status(self) -> dict[str, Any]:
        if not self.api_token:
            raise RuntimeError("tailscale_api_token_missing")
        if not self.tailnet:
            raise RuntimeError("tailscale_tailnet_missing")
        try:
            payload = self.api_fetcher(self._api_url(), self.api_token, 10.0)
        except httpx.TimeoutException as exc:
            raise RuntimeError("tailscale_api_timeout") from exc
        except httpx.RequestError as exc:
            raise RuntimeError("tailscale_api_unavailable") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("tailscale_api_invalid_document")
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise RuntimeError("tailscale_api_invalid_document")
        local_names = self._local_name_tokens()
        self_ips: list[str] = []
        peers: list[dict[str, Any]] = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            peer = self._api_device_to_peer(device)
            if self._device_name_tokens(device) & local_names:
                self_ips.extend(str(value).split("%", 1)[0] for value in peer["TailscaleIPs"] if _is_tailscale_ip(value))
            peers.append(peer)
        return {
            "BackendState": "Running",
            "TailscaleIPs": sorted(set(self_ips), key=lambda value: ipaddress.ip_address(value).version),
            "Self": {"TailscaleIPs": sorted(set(self_ips), key=lambda value: ipaddress.ip_address(value).version)},
            "Peer": peers,
        }

    def _load_inventory(self) -> tuple[dict[str, Any], str]:
        if self.provider not in {"auto", "cli", "api"}:
            raise RuntimeError("tailscale_provider_invalid")
        if self.provider == "cli":
            return self._load_cli_status(), "cli"
        if self.provider == "api":
            return self._load_api_status(), "api"
        try:
            return self._load_cli_status(), "cli"
        except FileNotFoundError:
            if self._api_configured():
                return self._load_api_status(), "api"
            raise

    @staticmethod
    def _self_ips(status: dict[str, Any]) -> set[str]:
        values = list(status.get("TailscaleIPs") or [])
        self_record = status.get("Self") or {}
        if isinstance(self_record, dict):
            values.extend(self_record.get("TailscaleIPs") or [])
        return {str(value).split("%", 1)[0] for value in values if _is_tailscale_ip(value)}

    def _eligible_addresses(self, status: dict[str, Any]) -> list[list[str]]:
        self_ips = self._self_ips(status)
        eligible: list[list[str]] = []
        for peer in self._peer_records(status):
            tags = peer.get("Tags") or []
            if not isinstance(tags, list) or self.tag not in tags:
                continue
            if peer.get("Online") is not True or peer.get("Expired") is True:
                continue
            addresses = []
            for value in peer.get("TailscaleIPs") or []:
                address = str(value).split("%", 1)[0]
                if address not in self_ips and _is_tailscale_ip(address):
                    addresses.append(address)
            if addresses:
                addresses.sort(key=lambda value: ipaddress.ip_address(value).version)
                eligible.append(addresses)
        return eligible[:MAX_TAILSCALE_DISCOVERY_PEERS]

    def scan_once(self) -> dict[str, Any]:
        scan_at = _now()
        try:
            status, provider = self._load_inventory()
            self_ips = sorted(self._self_ips(status), key=lambda value: ipaddress.ip_address(value).version)
            if self_ips and not os.environ.get("HERMES_BRIDGE_ADVERTISE_ADDRESS"):
                self.manager.set_runtime_advertise_address(self_ips[0])
            eligible = self._eligible_addresses(status)
            candidates = 0
            probe_failures = 0
            for addresses in eligible:
                peer_reached = False
                for address in addresses:
                    url = f"https://{_url_host(address)}:{self.port}/mcp"
                    try:
                        identity = self.identity_fetcher(url)
                        if identity.get("peer_id") == self.manager.identity.peer_id:
                            peer_reached = True
                            break
                        candidate = {
                            "peer_id": identity.get("peer_id"),
                            "display_name": identity.get("display_name"),
                            "fingerprint": identity.get("fingerprint"),
                            "url": url,
                            "platform": identity.get("platform"),
                            "bridge_version": identity.get("bridge_version"),
                            "protocol_version": identity.get("protocol_version"),
                            "source": "tailscale-api" if provider == "api" else "tailscale",
                        }
                        self.manager.state.ingest_candidate(candidate)
                        candidates += 1
                        peer_reached = True
                        break
                    except Exception:
                        continue
                if not peer_reached:
                    probe_failures += 1
            with self._health_lock:
                self._health.update({
                    "provider": provider,
                    "status": "degraded" if probe_failures else "healthy",
                    "eligible_peer_count": len(eligible),
                    "candidate_count": candidates,
                    "probe_failure_count": probe_failures,
                    "last_scan_at": scan_at,
                    "last_success_at": _now(),
                    "last_error": "bridge_probe_unreachable" if probe_failures else None,
                })
            return self.public_status()
        except Exception as exc:
            if isinstance(exc, FileNotFoundError):
                public_error = "tailscale_cli_unavailable"
            elif isinstance(exc, subprocess.TimeoutExpired):
                public_error = "tailscale_cli_timeout"
            elif isinstance(exc, RuntimeError):
                public_error = str(exc)
            else:
                public_error = "tailscale_discovery_error"
            with self._health_lock:
                self._health.update({
                    "status": "degraded",
                    "eligible_peer_count": 0,
                    "candidate_count": 0,
                    "probe_failure_count": 0,
                    "last_scan_at": scan_at,
                    "last_error": public_error,
                })
            return self.public_status()

    def public_status(self) -> dict[str, Any]:
        with self._health_lock:
            return dict(self._health)

    def start(self) -> None:
        if os.environ.get("HERMES_BRIDGE_TEST_SANDBOX") == "1":
            raise RuntimeError("real Tailscale discovery is forbidden in the test sandbox")
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="hermes-bridge-tailscale-discovery", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)
        with self._health_lock:
            self._health["status"] = "stopped"

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self.scan_once()
            if self.stop_event.wait(self.interval_seconds):
                return


class RecoveryWorker:
    """Best-effort trusted-peer gossip and endpoint reconciliation with bounded backoff."""

    def __init__(self, manager: NetworkManager, interval_seconds: float = 30.0):
        self.manager = manager
        self.interval_seconds = max(5.0, float(interval_seconds))
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.last_result: dict[str, Any] = {}

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="hermes-bridge-recovery", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def _run(self) -> None:
        delay = self.interval_seconds
        while not self.stop_event.wait(delay):
            try:
                introductions = self.manager.refresh_trusted_introductions()
                recovery = self.manager.recover_known_endpoints()
                self.last_result = {"introductions": introductions, "recovery": recovery}
                delay = self.interval_seconds
            except Exception as exc:
                self.last_result = {"error": f"{type(exc).__name__}: {exc}"}
                delay = min(delay * 2, 300.0) + secrets.randbelow(1000) / 1000


class MdnsDiscovery:
    def __init__(self, manager: NetworkManager, host: str, port: int):
        self.manager = manager
        self.host = host
        self.port = port
        self.zeroconf = None
        self.browser = None
        self.info = None

    def start(self) -> None:
        if os.environ.get("HERMES_BRIDGE_TEST_SANDBOX") == "1":
            raise RuntimeError("production mDNS is forbidden in the test sandbox")
        try:
            from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf
        except ImportError as exc:
            raise RuntimeError("zeroconf is required when automatic discovery is enabled") from exc
        identity = self.manager.identity
        address = os.environ.get("HERMES_BRIDGE_ADVERTISE_ADDRESS") or self._select_address()
        self.manager.set_runtime_advertise_address(address)
        properties = {
            "protocol": NETWORK_PROTOCOL_VERSION,
            "peer_id": identity.peer_id,
            "display_name": identity.display_name,
            "fingerprint": identity.fingerprint,
            "platform": platform.system().lower() or "unknown",
            "bridge_version": os.environ.get("HERMES_BRIDGE_VERSION", "v1.3.0"),
            "path": "/mcp",
        }
        service_name = f"{identity.peer_id}.{PRODUCTION_MDNS_TYPE}"
        self.info = ServiceInfo(
            PRODUCTION_MDNS_TYPE,
            service_name,
            addresses=[socket.inet_aton(address)],
            port=self.port,
            properties=properties,
            server=f"{identity.peer_id}.local.",
        )
        self.zeroconf = Zeroconf()
        self.zeroconf.register_service(self.info)

        manager = self.manager

        class Listener:
            def add_service(self, zc, service_type, name):
                self.update_service(zc, service_type, name)

            def update_service(self, zc, service_type, name):
                info = zc.get_service_info(service_type, name, timeout=2000)
                if not info:
                    return
                props = {k.decode("utf-8"): v.decode("utf-8") for k, v in info.properties.items()}
                if props.get("peer_id") == manager.identity.peer_id or not info.parsed_addresses():
                    return
                try:
                    manager.state.ingest_candidate({
                        "peer_id": props.get("peer_id"),
                        "display_name": props.get("display_name"),
                        "fingerprint": props.get("fingerprint"),
                        "url": f"https://{info.parsed_addresses()[0]}:{info.port}{props.get('path', '/mcp')}",
                        "platform": props.get("platform"),
                        "bridge_version": props.get("bridge_version"),
                        "protocol_version": props.get("protocol"),
                        "source": "mdns",
                    })
                except Exception:
                    return

            def remove_service(self, zc, service_type, name):
                return

        self.browser = ServiceBrowser(self.zeroconf, PRODUCTION_MDNS_TYPE, Listener())

    def stop(self) -> None:
        if self.zeroconf:
            if self.info:
                with contextlib.suppress(Exception):
                    self.zeroconf.unregister_service(self.info)
            self.zeroconf.close()
            self.zeroconf = None

    @staticmethod
    def _select_address() -> str:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not ipaddress.ip_address(address).is_loopback:
                return address
        raise RuntimeError("no non-loopback IPv4 address found; set HERMES_BRIDGE_ADVERTISE_ADDRESS")


def validate_sandbox_config(state_dir: Path, host: str, ports: list[int]) -> dict[str, Any]:
    sandbox = os.environ.get("HERMES_BRIDGE_TEST_SANDBOX") == "1"
    if not sandbox:
        return {"sandbox": False}
    root_value = os.environ.get("HERMES_BRIDGE_SANDBOX_ROOT")
    if not root_value:
        raise RuntimeError("HERMES_BRIDGE_SANDBOX_ROOT is required in sandbox mode")
    root = Path(root_value).resolve()
    resolved_state = Path(state_dir).resolve()
    if resolved_state != root and root not in resolved_state.parents:
        raise RuntimeError("sandbox state directory escapes HERMES_BRIDGE_SANDBOX_ROOT")
    if host not in {"127.0.0.1", "localhost", "::1"} and not host.startswith("127."):
        raise RuntimeError("sandbox host must be loopback")
    if any(int(port) in PRODUCTION_PORTS for port in ports):
        raise RuntimeError("sandbox cannot use a production bridge port")
    if os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "memory") != "memory":
        raise RuntimeError("sandbox discovery backend must be memory")
    if os.environ.get("HERMES_BRIDGE_STUB_DELEGATE") != "1":
        raise RuntimeError("sandbox requires HERMES_BRIDGE_STUB_DELEGATE=1")
    namespace = os.environ.get("HERMES_BRIDGE_DISCOVERY_NAMESPACE", "")
    if not namespace.startswith("test-"):
        raise RuntimeError("sandbox discovery namespace must start with test-")
    return {"sandbox": True, "root": str(root), "state_dir": str(resolved_state), "host": host, "ports": ports, "namespace": namespace}


def runtime_plan(state_dir: Path, host: str, legacy_port: int, secure_port: int) -> dict[str, Any]:
    isolation = validate_sandbox_config(state_dir, host, [legacy_port, secure_port])
    return {
        "bridge_version": "v1.3.0",
        "state_dir": str(Path(state_dir).resolve()),
        "identity_dir": str((Path(state_dir) / "network").resolve()),
        "host": host,
        "legacy_http_port": int(legacy_port),
        "secure_https_port": int(secure_port),
        "discovery_enabled": os.environ.get("HERMES_BRIDGE_AUTO_DISCOVERY", "1") != "0",
        "discovery_backend": os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns"),
        "discovery_namespace": os.environ.get("HERMES_BRIDGE_DISCOVERY_NAMESPACE", "production"),
        "isolation": isolation,
    }
