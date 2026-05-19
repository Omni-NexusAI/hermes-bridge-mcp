# Hermes Bridge MCP

Bridge Docker-hosted Hermes to native Windows Hermes through MCP.

Hermes Bridge MCP keeps the normal Windows Hermes conversation MCP tools and adds a
high-level proxy tool so a Docker Hermes agent can delegate work to the native
Windows Hermes agent when it needs Windows-local filesystem, process, desktop,
credential, or host integration access.

## What It Provides

- `windows_agent_status`
- `windows_agent_delegate`
- Existing Hermes messaging tools such as `conversations_list`,
  `messages_read`, and `messages_send`

`windows_agent_delegate` runs the task through native Windows Hermes:

```text
hermes chat --query ... --quiet --source mcp-windows-proxy --accept-hooks
```

It does not expose a raw PowerShell or CMD proxy.

## Requirements

- Native Windows Hermes installed at `%LOCALAPPDATA%\hermes`
- Docker Hermes configured with access to `host.docker.internal`
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

## Docker Hermes Config

In Docker Hermes `config.yaml`:

```yaml
mcp_servers:
  windows-hermes:
    url: http://host.docker.internal:18082/mcp
    timeout: 120
    connect_timeout: 30
```

Do not add `transport: sse` for this bridge. It uses supergateway
Streamable HTTP at `/mcp`.

## Verify

From the Docker Hermes container:

```bash
hermes mcp test windows-hermes
```

Expected result includes 12 tools:

```text
windows_agent_status
windows_agent_delegate
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
            result = await session.call_tool("windows_agent_delegate", {
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
- `startup/*.vbs` - hidden Startup-folder launchers

## Notes

- Existing Docker Hermes sessions may cache MCP tools. Restart Docker Hermes or
  start a new session after installing.
- The bridge endpoint remains `windows-hermes` at
  `host.docker.internal:18082/mcp`.
- Delegated tasks use Windows Hermes normal approval policy.
