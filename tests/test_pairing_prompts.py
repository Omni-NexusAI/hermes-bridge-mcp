import asyncio
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP


BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))

import bridge_pairing_tools


def _server() -> FastMCP:
    server = FastMCP("pairing-prompt-test")
    bridge_pairing_tools.add_pairing_tools(server)
    return server


def _argument_names(prompt) -> list[str]:
    return [argument.name for argument in prompt.arguments]


def _render_text(prompt, arguments=None) -> str:
    messages = asyncio.run(prompt.render(arguments))
    assert len(messages) == 1
    return messages[0].content.text


def test_pairing_prompts_are_registered_with_stable_names_and_arguments():
    prompts = _server()._prompt_manager._prompts

    assert set(prompts) == {
        "manual_pair",
        "discovery_pair",
        "tailscale_pair",
        "pair_status",
        "repair",
    }
    assert _argument_names(prompts["manual_pair"]) == [
        "peer_id",
        "url",
        "expected_fingerprint",
    ]
    assert _argument_names(prompts["discovery_pair"]) == [
        "peer_id",
        "expected_fingerprint",
    ]
    assert _argument_names(prompts["tailscale_pair"]) == [
        "peer_id",
        "expected_fingerprint",
    ]
    assert _argument_names(prompts["pair_status"]) == []
    assert _argument_names(prompts["repair"]) == ["peer_id"]


def test_manual_pair_prompt_preserves_exact_identity_inputs():
    fingerprint = "a" * 64
    text = _render_text(
        _server()._prompt_manager._prompts["manual_pair"],
        {
            "peer_id": "test-laptop",
            "url": "https://192.0.2.10:18443/mcp",
            "expected_fingerprint": fingerprint,
        },
    )

    assert '"peer_id": "test-laptop"' in text
    assert '"url": "https://192.0.2.10:18443/mcp"' in text
    assert fingerprint in text
    assert "Never shorten or infer a fingerprint" in text


def test_discovery_prompt_never_auto_approves_an_unselected_candidate():
    text = _render_text(_server()._prompt_manager._prompts["discovery_pair"])

    assert "bridge_discovery_scan" in text
    assert "Do not approve a candidate automatically" in text


def test_repair_prompt_is_peer_scoped_and_preserves_pinned_identity():
    text = _render_text(
        _server()._prompt_manager._prompts["repair"],
        {"peer_id": "test-desktop"},
    )

    assert '"test-desktop"' in text
    assert "Do not replace a pinned fingerprint" in text
