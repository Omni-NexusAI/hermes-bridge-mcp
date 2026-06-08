import asyncio
import json
import sys
import time

from mcp import ClientSession

try:
    from mcp.client.streamable_http import streamable_http_client as http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as http_client


async def call_json(session: ClientSession, name: str, args: dict) -> dict:
    result = await session.call_tool(name, args)
    return json.loads(result.content[0].text)


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://host.docker.internal:18083/mcp"
    async with http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            start = await call_json(session, "bridge_agent_delegate_start", {
                "prompt": "Reply exactly DIRECT_BRIDGE_ONLY_OK. Do not use tools.",
                "a0_thread_key": "agentspine-direct-bridge-smoke",
                "caller": "agentspine-standard-pre",
                "timeout_seconds": 180,
                "max_turns": 5,
            })
            print("START=" + json.dumps(start, sort_keys=True))
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                status = await call_json(session, "bridge_agent_delegate_status", {"task_id": start["task_id"]})
                print(f"STATUS={status.get('status')} elapsed_ms={status.get('elapsed_ms')}")
                if status.get("status") in {"completed", "failed", "timed_out", "canceled"}:
                    result = await call_json(session, "bridge_agent_delegate_result", {"task_id": start["task_id"]})
                    print("RESULT=" + json.dumps(result, sort_keys=True)[:2000])
                    return
                await asyncio.sleep(5)
            raise TimeoutError(start["task_id"])


if __name__ == "__main__":
    asyncio.run(main())
