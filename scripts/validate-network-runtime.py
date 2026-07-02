from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from hermes_bridge_network import runtime_plan  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Hermes Bridge automatic-pairing runtime paths and isolation settings without starting services.")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--legacy-port", type=int, default=18084)
    parser.add_argument("--secure-port", type=int, default=18443)
    parser.add_argument("--sandbox", action="store_true", help="Resolve a safe temporary test plan with dynamic non-production ports supplied by the caller.")
    args = parser.parse_args()

    if args.sandbox:
        root = (args.state_dir or Path(tempfile.gettempdir()) / f"hermes-bridge-sandbox-{uuid.uuid4().hex}").resolve()
        os.environ["HERMES_BRIDGE_TEST_SANDBOX"] = "1"
        os.environ["HERMES_BRIDGE_SANDBOX_ROOT"] = str(root)
        os.environ["HERMES_BRIDGE_DISCOVERY_BACKEND"] = "memory"
        os.environ["HERMES_BRIDGE_DISCOVERY_NAMESPACE"] = f"test-{uuid.uuid4().hex}"
        os.environ["HERMES_BRIDGE_STUB_DELEGATE"] = "1"
        state_dir = root / "state"
        if args.legacy_port in {18082, 18083, 18084, 18443} or args.secure_port in {18082, 18083, 18084, 18443}:
            raise SystemExit("--sandbox requires explicit non-production --legacy-port and --secure-port values")
    else:
        state_dir = args.state_dir or Path(os.environ.get("HERMES_BRIDGE_STATE_DIR", Path.home() / ".hermes" / "bridge-state"))

    print(json.dumps(runtime_plan(state_dir, args.host, args.legacy_port, args.secure_port), indent=2))


if __name__ == "__main__":
    main()
