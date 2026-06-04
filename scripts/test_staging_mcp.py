import asyncio
import sys

from mcp import ClientSession

try:
    from mcp.client.streamable_http import streamable_http_client as http_client
except ImportError:
    from mcp.client.streamable_http import streamablehttp_client as http_client


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://host.docker.internal:18083/mcp"
    async with http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            wanted = [
                name
                for name in names
                if name == "windows_agent_status"
                or name.startswith("windows_agent_delegate")
                or name == "bridge_agent_status"
                or name.startswith("bridge_agent_delegate")
            ]
            print("TOOLS=" + ",".join(sorted(wanted)))
            messaging = [name for name in names if name in {"messages_send", "conversations_list"}]
            print("MESSAGING_TOOLS=" + ",".join(sorted(messaging)))
            result = await session.call_tool("windows_agent_status", {})
            print(result.content[0].text[:1200])


if __name__ == "__main__":
    asyncio.run(main())
