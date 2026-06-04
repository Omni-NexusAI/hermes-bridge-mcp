import argparse
import asyncio
import json
import time

import httpx
from mcp import ClientSession

try:
    from mcp.client.streamable_http import streamable_http_client as http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as http_client


async def call_json(url: str, token: str, tool: str, args: dict) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=None) as client:
        async with http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                return json.loads(result.content[0].text)


async def wait_result(url: str, token: str, task_id: str, deadline_seconds: int) -> dict:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        status = await call_json(url, token, "bridge_agent_delegate_status", {"task_id": task_id})
        if status.get("status") in {"completed", "failed", "timed_out", "canceled"}:
            return await call_json(url, token, "bridge_agent_delegate_result", {"task_id": task_id})
        await asyncio.sleep(5)
    raise TimeoutError(task_id)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke test one Hermes peer bridge endpoint.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--prompt", default="Reply exactly HERMES_PEER_OK.")
    parser.add_argument("--conversation-key", default="peer-smoke")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    args = parser.parse_args()

    status = await call_json(args.url, args.token, "bridge_agent_status", {})
    print("STATUS=" + json.dumps(status, sort_keys=True)[:1200])
    start = await call_json(args.url, args.token, "bridge_agent_delegate_start", {
        "prompt": args.prompt,
        "timeout_seconds": args.timeout_seconds,
        "max_turns": 5,
        "a0_thread_key": f"peer-smoke:{args.conversation_key}",
        "caller": "smoke_peer_bridge",
    })
    print("START=" + json.dumps(start, sort_keys=True))
    result = await wait_result(args.url, args.token, start["task_id"], args.timeout_seconds)
    print("RESULT=" + json.dumps(result, sort_keys=True)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
