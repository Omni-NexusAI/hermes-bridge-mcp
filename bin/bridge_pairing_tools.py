"""Bridge pairing management MCP tools.

Self-contained module that adds pairing management tools to the bridge
MCP server. Any MCP-compatible agent (Hermes, Agent Zero, Claude Code,
Cursor, etc.) can invoke these tools — no slash commands or skills
required.

Tools provided:
  - bridge_manual_pair: Pin a secure identity or configure an existing legacy key
  - bridge_pair_status: Comprehensive peer status with connectivity checks
  - bridge_repair_peer: Diagnose and fix broken peer connections
  - bridge_discovery_scan: List discovery candidates (v1.3.0+)
  - bridge_discovery_pair: Approve a discovered candidate (v1.3.0+)

Imported optionally by the main bridge server via add_pairing_tools(mcp).
Works with v1.2.7 (manual) and v1.3.0+ (discovery if module present).
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


def _hermes_home() -> Path:
    explicit = os.environ.get("HERMES_BRIDGE_HOME") or os.environ.get("HERMES_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
    return Path.home() / ".hermes"


def _bridge_state_dir() -> Path:
    return Path(os.environ.get("HERMES_BRIDGE_STATE_DIR", str(_hermes_home() / "bridge-state"))).expanduser()


def _peer_config_file() -> Path:
    return Path(os.environ.get("HERMES_BRIDGE_PEERS_CONFIG", str(_bridge_state_dir() / "peers.json"))).expanduser()


def _local_peer_id() -> str:
    return os.environ.get("HERMES_BRIDGE_PEER_ID") or f"{platform.node() or 'hermes'}-{platform.system().lower() or 'peer'}"


def _lan_ip() -> str:
    """Get this machine's primary LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def _read_peers(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        if isinstance(data, dict) and "peers" in data:
            return data
        if isinstance(data, list):
            return {"peers": data}
        return {"peers": []}
    except FileNotFoundError:
        return {"peers": []}
    except Exception:
        return {"peers": []}


def _write_peers(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _discovery_available() -> bool:
    """Check if the v1.3.0 network module is present."""
    network_path = _hermes_home() / "bin" / "hermes_bridge_network.py"
    return network_path.exists()


def add_pairing_tools(mcp, network_manager=None) -> None:
    """Register pairing management MCP tools on the server.

    Called by the main bridge server during startup. All tools are
    architecture-agnostic — any MCP client discovers and invokes them.
    """

    @mcp.tool()
    def bridge_manual_pair(
        peer_id: str,
        url: str,
        platform_name: Optional[str] = None,
        expected_fingerprint: Optional[str] = None,
    ) -> str:
        """Pair by pinned HTTPS identity or configure an existing legacy key.

        HTTPS inspects the remote identity and requires fingerprint confirmation.
        Legacy HTTP uses an already configured HERMES_BRIDGE_PAIR_KEY; it never
        invents a credential that the remote listener cannot accept.

        Args:
            peer_id: Unique name for the device (e.g. "laptop", "quest3")
            url: Peer bridge URL (e.g. "http://192.168.1.50:18084/mcp")
            platform_name: Device platform. Auto-detected if omitted.

        Any MCP client can call this — no slash commands or skills needed.
        """
        if not peer_id or not peer_id.strip():
            return _json({"error": "peer_id is required"})
        if not url or not url.strip():
            return _json({"error": "url is required"})

        peer_id = peer_id.strip()
        url = url.strip()

        if urlparse(url).scheme == "https":
            if network_manager is None:
                return _json({"error": "secure_pairing_unavailable"})
            try:
                identity = network_manager.inspect_identity(url)
                if identity.get("peer_id") != peer_id:
                    return _json({"error": "peer_id_mismatch", "advertised_peer_id": identity.get("peer_id")})
                candidate = {
                    "peer_id": peer_id,
                    "display_name": identity.get("display_name", peer_id),
                    "fingerprint": identity["fingerprint"],
                    "url": url,
                    "platform": identity.get("platform", platform_name or "unknown"),
                    "bridge_version": identity.get("bridge_version", "unknown"),
                    "protocol_version": identity.get("protocol_version", "1"),
                    "source": "manual",
                }
                network_manager.state.ingest_candidate(candidate)
                if not expected_fingerprint:
                    return _json({"status": "confirmation_required", **candidate, "next_step": "Verify the remote fingerprint, then repeat with expected_fingerprint."})
                return _json(network_manager.approve(peer_id, expected_fingerprint))
            except Exception as exc:
                return _json({"error": type(exc).__name__, "message": str(exc), "peer_id": peer_id, "url": url})

        if not platform_name:
            platform_name = platform.system().lower() or "unknown"

        token = os.environ.get("HERMES_BRIDGE_PAIR_KEY", "").strip()
        if not token:
            return _json({
                "error": "legacy_shared_key_required",
                "message": "Set the same HERMES_BRIDGE_PAIR_KEY on both devices, or use HTTPS managed pairing. A local-only generated token cannot complete pairing.",
            })
        config_path = _peer_config_file()
        data = _read_peers(config_path)
        peers = data.get("peers", [])

        # Replace existing entry with same peer_id
        peers = [p for p in peers if isinstance(p, dict) and p.get("peer_id") != peer_id]

        entry = {
            "peer_id": peer_id,
            "url": url,
            "platform": platform_name,
            "pair_key": token,
        }
        peers.append(entry)
        data["peers"] = peers
        _write_peers(config_path, data)

        # Build info for remote paste block
        this_peer_id = _local_peer_id()
        this_ip = _lan_ip()
        this_platform = platform.system().lower() or "unknown"

        # Try to match the port from the provided URL
        this_port = os.environ.get("HERMES_BRIDGE_PORT", "18084")

        paste_block = (
            f"━━━ PASTE THIS INTO {peer_id}'s AGENT SESSION ━━━\n\n"
            f"Add this peer to your peers.json:\n"
            f"{{\n"
            f'  "peer_id": "{this_peer_id}",\n'
            f'  "url": "http://{this_ip}:{this_port}/mcp",\n'
            f'  "platform": "{this_platform}",\n'
            f'  "pair_key": "{token}"\n'
            f"}}\n\n"
            f'After adding, verify: bridge_peer_status(peer_id="{this_peer_id}")\n'
            f"━━━ END PASTE BLOCK ━━━"
        )

        return _json({
            "status": "configured",
            "peer_id": peer_id,
            "url": url,
            "platform": platform_name,
            "token_generated": False,
            "config_file": str(config_path),
            "paste_block_for_remote": paste_block,
            "next_step": (
                f"Run the paste block on {peer_id} to complete bidirectional pairing, "
                f'then verify with bridge_peer_status(peer_id="{peer_id}").'
            ),
        })

    @mcp.tool()
    def bridge_pair_status() -> str:
        """Comprehensive status of all configured peers and discovery state.

        Reports peer_id, URL, platform, token status for each peer.
        Includes discovery availability (v1.3.0+) and local device info.

        Any MCP client can call this for a full network overview.
        """
        config_path = _peer_config_file()
        data = _read_peers(config_path)
        peers = data.get("peers", [])

        peer_reports = []
        for entry in peers:
            if not isinstance(entry, dict):
                continue
            peer_reports.append({
                "peer_id": entry.get("peer_id", "unknown"),
                "url": entry.get("url", ""),
                "platform": entry.get("platform", "unknown"),
                "token_configured": bool(entry.get("pair_key") or entry.get("token")),
            })

        result = {
            "local_peer_id": _local_peer_id(),
            "local_platform": platform.system().lower() or "unknown",
            "local_lan_ip": _lan_ip(),
            "config_file": str(config_path),
            "configured_peers": peer_reports,
            "total_peers": len(peer_reports),
            "discovery_available": _discovery_available(),
        }

        if network_manager is not None:
            result["managed_network"] = network_manager.network_status()

        if _discovery_available():
            result["discovery_env"] = {
                "AUTO_DISCOVERY": os.environ.get("HERMES_BRIDGE_AUTO_DISCOVERY", "0"),
                "SECURE_NETWORK": os.environ.get("HERMES_BRIDGE_SECURE_NETWORK", "0"),
                "BACKEND": os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns"),
            }

        return _json(result)

    @mcp.tool()
    def bridge_repair_peer(peer_id: str) -> str:
        """Diagnose and attempt to repair a broken peer connection.

        Checks connectivity, auth, and common failure modes. Reports
        the specific issue and recommended fix.

        Args:
            peer_id: The peer to diagnose.

        Any MCP client can call this to troubleshoot a broken connection.
        """
        if not peer_id or not peer_id.strip():
            return _json({"error": "peer_id is required"})

        peer_id = peer_id.strip()
        config_path = _peer_config_file()
        data = _read_peers(config_path)
        peers = data.get("peers", [])

        entry = None
        for p in peers:
            if isinstance(p, dict) and p.get("peer_id") == peer_id:
                entry = p
                break

        if not entry:
            return _json({
                "error": f"peer not found: {peer_id}",
                "available_peers": [p.get("peer_id") for p in peers if isinstance(p, dict)],
            })

        url = entry.get("url", "")
        token = entry.get("pair_key") or entry.get("token") or ""

        diagnosis = {
            "peer_id": peer_id,
            "url": url,
            "token_configured": bool(token),
        }

        try:
            import httpx
            parsed = urlparse(url)
            host = parsed.hostname or ""
            port = parsed.port or 18084
            base = f"{parsed.scheme}://{host}:{port}"

            # Check if host is reachable
            try:
                response = httpx.get(f"{base}/", timeout=5.0)
                diagnosis["host_reachable"] = True
                diagnosis["bridge_responding"] = response.status_code in (200, 404, 405)

                if diagnosis["bridge_responding"]:
                    # Check auth
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    auth_resp = httpx.get(f"{base}/mcp", headers=headers, timeout=5.0)
                    if auth_resp.status_code == 200:
                        diagnosis["auth_status"] = "working"
                        diagnosis["repair_needed"] = False
                    elif auth_resp.status_code in (401, 403):
                        diagnosis["auth_status"] = "token_mismatch"
                        diagnosis["repair_needed"] = True
                        diagnosis["repair_action"] = (
                            "Token rejected. Call bridge_manual_pair to generate a new "
                            "shared token and update both peers."
                        )
                    elif auth_resp.status_code == 406:
                        diagnosis["auth_status"] = "version_mismatch"
                        diagnosis["repair_needed"] = True
                        diagnosis["repair_action"] = (
                            "HTTP 406 — likely bridge version mismatch. "
                            "Update both bridges to the same version."
                        )
                    else:
                        diagnosis["auth_status"] = f"http_{auth_resp.status_code}"
                        diagnosis["repair_needed"] = True
                else:
                    diagnosis["repair_needed"] = True
                    diagnosis["repair_action"] = (
                        f"Bridge returned HTTP {response.status_code}. "
                        "Check if the bridge service is running correctly."
                    )
            except httpx.ConnectError:
                diagnosis["host_reachable"] = False
                diagnosis["repair_needed"] = True
                diagnosis["repair_action"] = (
                    "Host unreachable. The device may be offline, the IP may have "
                    "changed (DHCP), or a firewall is blocking the connection."
                )
            except httpx.TimeoutException:
                diagnosis["host_reachable"] = False
                diagnosis["repair_needed"] = True
                diagnosis["repair_action"] = "Connection timed out. Check network/firewall."
        except Exception as exc:
            diagnosis["connectivity_check_error"] = f"{type(exc).__name__}: {exc}"
            diagnosis["repair_needed"] = True
            diagnosis["repair_action"] = "Unable to perform connectivity check."

        return _json(diagnosis)

    @mcp.tool()
    def bridge_discovery_scan() -> str:
        """Scan for discoverable bridge peers (requires v1.3.0+).

        Returns discovered candidates from the active backend (mDNS or
        Tailscale). If discovery is not available or not enabled, returns
        guidance on how to enable it.

        On v1.3.0+, prefer bridge_network_status for full candidate details.
        """
        if not _discovery_available():
            return _json({
                "error": "discovery_not_available",
                "message": "Bridge v1.3.0+ with automatic peer discovery is required.",
                "upgrade_guidance": "Update to the development branch with discovery support.",
            })

        auto = os.environ.get("HERMES_BRIDGE_AUTO_DISCOVERY", "0") == "1"
        if not auto:
            return _json({
                "error": "discovery_not_enabled",
                "message": "Automatic discovery is not enabled.",
                "required_env_vars": {
                    "HERMES_BRIDGE_AUTO_DISCOVERY": "1",
                    "HERMES_BRIDGE_SECURE_NETWORK": "1",
                },
                "optional_backend": "HERMES_BRIDGE_DISCOVERY_BACKEND=mdns|tailscale",
            })

        return _json({
            "discovery_active": True,
            "backend": os.environ.get("HERMES_BRIDGE_DISCOVERY_BACKEND", "mdns"),
            "message": "Discovery is active. Use bridge_network_status for candidate list, "
                       "then bridge_discovery_pair to approve.",
        })

    @mcp.tool()
    def bridge_discovery_pair(
        peer_id: str,
        expected_fingerprint: Optional[str] = None,
    ) -> str:
        """Approve a discovered peer for TOFU pairing (requires v1.3.0+).

        On v1.3.0+, this wraps bridge_peer_pair(action='approve').
        Both sides must approve for bidirectional communication.

        Args:
            peer_id: The discovered candidate's peer_id.
            expected_fingerprint: Candidate fingerprint for verification.

        On v1.3.0+, prefer bridge_peer_pair for the native pairing flow.
        """
        if not _discovery_available():
            return _json({
                "error": "discovery_not_available",
                "message": "Bridge v1.3.0+ required for discovery pairing.",
            })

        auto = os.environ.get("HERMES_BRIDGE_AUTO_DISCOVERY", "0") == "1"
        if not auto:
            return _json({
                "error": "discovery_not_enabled",
                "message": "Enable HERMES_BRIDGE_AUTO_DISCOVERY=1 first.",
            })

        return _json({
            "status": "guidance",
            "peer_id": peer_id,
            "message": (
                "On v1.3.0+, use bridge_peer_pair(action=\"approve\", "
                f"peer_id=\"{peer_id}\""
                + (f", expected_fingerprint=\"{expected_fingerprint}\")" if expected_fingerprint else ")")
                + " for the native TOFU pairing flow."
            ),
        })
