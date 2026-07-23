#!/usr/bin/env python3
"""Canonical Agent Bridge MCP entrypoint.

The legacy module remains the implementation entrypoint for v1.3.x import
compatibility. This wrapper keeps both launch names on the same runtime.
"""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("windows-hermes-proxy-mcp.py")),
        run_name="__main__",
    )
