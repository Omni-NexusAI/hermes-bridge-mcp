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
    text = result.content[0].text
    return json.loads(text)


async def wait_task(session: ClientSession, task_id: str, timeout_seconds: int = 240) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = await call_json(session, "bridge_agent_delegate_status", {"task_id": task_id})
        print(f"STATUS {task_id} {status.get('status')} elapsed_ms={status.get('elapsed_ms')}")
        if status.get("status") in {"completed", "failed", "timed_out", "canceled"}:
            return await call_json(session, "bridge_agent_delegate_result", {"task_id": task_id})
        await asyncio.sleep(5)
    raise TimeoutError(task_id)


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://host.docker.internal:18083/mcp"
    thread_key = "agentspine-staging-hermes-bridge-test"
    async with http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            first = await call_json(session, "bridge_agent_delegate_start", {
                "prompt": "Reply exactly STAGING_BRIDGE_OK. Do not use tools.",
                "a0_thread_key": thread_key,
                "caller": "agentspine-standard-pre",
                "timeout_seconds": 240,
                "max_turns": 5,
            })
            print("FIRST_START=" + json.dumps(first, sort_keys=True))
            first_result = await wait_task(session, first["task_id"])
            print("FIRST_RESULT=" + json.dumps(first_result, sort_keys=True)[:2000])

            second = await call_json(session, "bridge_agent_delegate_start", {
                "prompt": "Reply exactly STAGING_BRIDGE_RESUME_OK. Do not use tools.",
                "a0_thread_key": thread_key,
                "caller": "agentspine-standard-pre",
                "timeout_seconds": 240,
                "max_turns": 5,
            })
            print("SECOND_START=" + json.dumps(second, sort_keys=True))
            second_result = await wait_task(session, second["task_id"])
            print("SECOND_RESULT=" + json.dumps(second_result, sort_keys=True)[:2000])

            timeout_probe = await call_json(session, "bridge_agent_delegate", {
                "prompt": "Reply exactly TIMEOUT_PROBE_OK. Do not use tools.",
                "a0_thread_key": thread_key + "-timeout",
                "caller": "agentspine-standard-pre",
                "timeout_seconds": 1,
                "max_turns": 5,
            })
            print("TIMEOUT_PROBE=" + json.dumps(timeout_probe, sort_keys=True)[:2000])
            if timeout_probe.get("task_id") and timeout_probe.get("status") == "still_running":
                canceled = await call_json(session, "bridge_agent_delegate_cancel", {
                    "task_id": timeout_probe["task_id"],
                })
                print("TIMEOUT_CANCEL=" + json.dumps(canceled, sort_keys=True)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
