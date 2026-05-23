# Hermes Bridge MCP

Bridge container-hosted agents to native Windows Hermes through MCP.

Hermes Bridge MCP keeps the normal Windows Hermes conversation MCP tools and adds a
high-level proxy tool so any MCP client can delegate work to the native
Windows Hermes agent when it needs Windows-local filesystem, process, desktop,
credential, or host integration access.

## What It Provides

- `bridge_agent_status`
- `bridge_agent_delegate`
- Compatibility aliases: `windows_agent_status`, `windows_agent_delegate`
- Existing Hermes messaging tools such as `conversations_list`,
  `messages_read`, and `messages_send`

`bridge_agent_delegate` runs the task through native Windows Hermes:

```text
hermes chat --query ... --quiet --source mcp-windows-proxy --accept-hooks
```

It does not expose a raw PowerShell or CMD proxy.

## Requirements

- Native Windows Hermes installed at `%LOCALAPPDATA%\hermes`
- An MCP client that can reach `host.docker.internal:18082`
- Node.js with `supergateway` installed globally:

```powershell
npm install -g supergateway
```

## Install

From this repository on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The installer copies scripts into `%LOCALAPPDATA%\hermes\bin`, creates hidden
Startup launchers, and can restart the bridge.

## MCP Client Config

For MCP clients that accept URL servers, add:

```yaml
mcp_servers:
  hermes-bridge:
    url: http://host.docker.internal:18082/mcp
    timeout: 120
    connect_timeout: 30
```

Do not add `transport: sse` for this bridge. It uses supergateway
Streamable HTTP at `/mcp`.

## A0 Integration

Agent Zero / A0 stores MCP servers in `/a0/usr/settings.json` as a JSON string
under `mcp_servers`. Use the helper to add the bridge without replacing other
MCP servers:

```bash
python scripts/configure-a0-mcp.py --settings /a0/usr/settings.json
```

The helper writes a timestamped backup next to `settings.json` before changing
the file. The added server is named `hermes-bridge` and points at
`http://host.docker.internal:18082/mcp`.

To preview the change:

```bash
python scripts/configure-a0-mcp.py --settings /a0/usr/settings.json --dry-run
```

## Verify

From a Hermes container:

```bash
hermes mcp test windows-hermes
```

Expected result includes the bridge tools:

```text
bridge_agent_status
bridge_agent_delegate
```

You can also test the proxy directly:

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://host.docker.internal:18082/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("bridge_agent_delegate", {
                "prompt": "Reply exactly WINDOWS_PROXY_OK and do not use tools.",
                "timeout_seconds": 180,
                "max_turns": 10,
            })
            print(result.content[0].text)

asyncio.run(main())
```

## Files

- `bin/windows-hermes-proxy-mcp.py` - companion MCP server
- `bin/windows-hermes-mcp-serve.cmd` - stdio entrypoint for supergateway
- `bin/start-windows-hermes-bridge.ps1` - starts supergateway on port 18082
- `bin/windows-hermes-bridge-background-watchdog.ps1` - hidden bridge watchdog
- `bin/start-windows-hermes-gateway.ps1` - starts native Windows Hermes gateway
- `bin/windows-hermes-gateway-background-watchdog.ps1` - hidden gateway watchdog
- `scripts/configure-a0-mcp.py` - backup-first A0 MCP settings helper
- `startup/*.vbs` - hidden Startup-folder launchers

## Notes

- Existing agent sessions may cache MCP tools. Restart the client agent or
  start a new session after installing.
- The bridge endpoint remains
  `host.docker.internal:18082/mcp`.
- Delegated tasks use Windows Hermes normal approval policy.
