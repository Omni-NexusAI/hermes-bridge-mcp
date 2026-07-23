# Automatic Pairing Validation on Unpaired Devices

Use this checklist only on devices that are not part of the existing paired
agent network. Agent Bridge MCP v1.3.5 is a release candidate; existing agents should
remain untouched until this validation is complete.

## Before Installation

1. Use a separate test network or devices that are not currently paired.
2. Confirm ports `18084` and `18443` are available on each test device.
3. Preview the resolved configuration without starting services:

   ```powershell
   python scripts/validate-network-runtime.py
   ```

4. Record the current bridge checkout and back up only the test device's bridge
   state if it already has one.

## Install and Enable

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
$env:HERMES_BRIDGE_AUTO_DISCOVERY = "1"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\hermes\bin\start-hermes-bridge-peer.ps1
```

Android/Termux:

```sh
python -m pip install -r requirements-android.txt
cp config/android-peer.env.example config/android-peer.env
# Set HERMES_BRIDGE_AUTO_DISCOVERY=1 in config/android-peer.env
sh bin/start-hermes-bridge-peer.sh
```

Do not copy an existing production `peers.json`, pair key, identity directory,
or `paired-peers.json` onto a test device.

## First Pairing

1. Call `bridge_network_status` on one device.
2. Confirm the candidate's display name, platform, and full SHA-256 fingerprint
   against `bridge_network_status` on the other device.
3. Approve exactly that fingerprint:

   ```text
   bridge_peer_pair(
     action="approve",
     peer_id="other-device-id",
     expected_fingerprint="full-sha256-fingerprint"
   )
   ```

4. Confirm both devices report `certificate_pinned: true` and
   `credential_configured: true` without exposing credentials.
5. Run `bridge_peer_status` in both directions, then start and retrieve a small
   delegated task in both directions.

## Recovery Scenarios

- Restart one peer service and confirm delegation resumes without approval.
- Change one test device's LAN address, wait for discovery, call
  `bridge_peer_pair(action="reconnect", peer_id="...")`, and confirm its pinned
  fingerprint remains unchanged.
- On a disposable test node, remove only `paired-peers.json` while preserving
  its identity key and certificate. Reconnect from the peer retaining the signed
  receipt and confirm credentials are restored automatically.
- Present a new identity under the same peer ID and confirm it is shown as a
  conflict requiring revocation and fresh approval.
- Confirm an identity introduced by a trusted peer remains an unpaired candidate
  until explicitly approved.

## Revocation

```text
bridge_peer_pair(action="revoke", peer_id="other-device-id")
```

Confirm the revoked fingerprint cannot reconnect using its old receipt or
credentials. Re-pairing requires an explicit new approval.

## Rollback

1. Stop the v1.3.5 peer launcher on the test devices.
2. Disable or remove `HERMES_BRIDGE_AUTO_DISCOVERY`.
3. Restore the prior bridge checkout or reinstall the prior version.
4. Remove the test device's `bridge-state/network` directory only if its new
   identity and pairings are intentionally being discarded.
5. Restore any test-only static `peers.json` and shared pair key if legacy peer
   mode is required.

Rollback does not require changing existing agents because this validation must
not involve them.
