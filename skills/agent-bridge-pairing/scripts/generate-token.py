#!/usr/bin/env python3
"""Generate a cryptographically secure token for bridge pairing.

Uses secrets.token_urlsafe(32) — produces a 43-character URL-safe string
suitable for use as a bridge pair_key bearer token.

Usage:
    python generate-token.py          # print token to stdout
    python generate-token.py --json   # print as {"token": "..."} JSON

The token is the BARE VALUE used in peers.json "pair_key" field.
The bridge internally adds the "Bearer " prefix when sending the
Authorization header. Do NOT include "Bearer " in the stored value.
"""

import argparse
import json
import secrets
import sys


def generate_token() -> str:
    """Generate a cryptographically secure URL-safe token."""
    return secrets.token_urlsafe(32)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a bridge pairing token."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON object: {\"token\": \"...\"}",
    )
    args = parser.parse_args()

    token = generate_token()

    if args.json:
        print(json.dumps({"token": token}))
    else:
        print(token)

    return 0


if __name__ == "__main__":
    sys.exit(main())
