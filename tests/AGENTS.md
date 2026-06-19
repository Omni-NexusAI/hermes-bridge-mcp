# Tests

## Purpose

Automated verification for the bridge's compatibility, security, discovery, pairing, and recovery behavior.

## Local Contracts

- Tests must fail closed if a real Hermes path, production port, production discovery namespace, or non-loopback network interface is selected.
- Never invoke a real Hermes binary or contact an existing bridge, agent, container, or device.
- Use temporary roots, generated credentials, stub delegate runners, and injectable discovery.
- Production mDNS broadcast is forbidden in automated tests.
- The integration harness may start only temporary loopback HTTPS bridge processes with dynamic ports and `HERMES_BRIDGE_STUB_DELEGATE=1`.

## Verification

- Assert the 11 stable core tool names and schemas separately from optional extension tools.
- Cover approval, rejection, replay, expiry, certificate mismatch, revocation, restart, endpoint change, and identity-preserving rekey.
- Exercise signed introductions and confirm introduced unknown identities remain candidates until approval.

## Child DOX Index

- No child DOX scopes.
