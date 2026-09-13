"""Persistent device identity and signing primitives for Agent Bridge.

Network discovery consumes these primitives but cannot replace an existing
identity after a read failure or missing file.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import platform
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agent_bridge_storage import InterProcessFileLock

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
            present = [path.exists() for path in (self.key_path, self.cert_path, self.meta_path)]
            if any(present) and not all(present):
                raise ValueError("identity is incomplete; restore its missing files from backup before starting the bridge")
            if not any(present):
                self._create()
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
            cert_pem = self.cert_path.read_text(encoding="utf-8")
            fingerprint = _fingerprint_cert(cert_pem)
            if fingerprint != meta.get("fingerprint"):
                raise ValueError("identity certificate fingerprint does not match metadata")
            private_key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            certificate_key = _public_key_from_cert(cert_pem)
            if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(certificate_key, ec.EllipticCurvePublicKey):
                raise ValueError("unsupported identity private key or certificate")
            if private_key.public_key().public_numbers() != certificate_key.public_numbers():
                raise ValueError("identity private key does not match certificate")
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
        configured = os.environ.get("AGENT_BRIDGE_PEER_ID", os.environ.get("HERMES_BRIDGE_PEER_ID"))
        peer_id = _safe_peer_id(configured) if configured else f"hermes-{uuid.uuid4().hex[:16]}"
        display_name = os.environ.get("AGENT_BRIDGE_DISPLAY_NAME", os.environ.get("HERMES_BRIDGE_DISPLAY_NAME")) or platform.node() or peer_id
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
