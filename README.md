# Hermes Bridge MCP

Bridge Docker-hosted Hermes to native Windows Hermes through MCP.

Hermes Bridge MCP exposes direct delegation tools so a Docker-hosted agent such
as A0/Agentspine can delegate work to native Windows Hermes. Delegated work runs
through Hermes itself, so Hermes can use its normal tools, session persistence,
memory, and approval policy.

It can also run as a native Streamable HTTP peer bridge for Hermes-to-Hermes
agent communication across a trusted network. The first supported peer targets
are Windows and Android/Termux, with Quest 3 as the first Android validation
device.

This bridge is not a Hermes messaging gateway proxy. It does not expose Telegram,
Discord, Slack, WhatsApp, or `messages_send`/`conversations_list` tools.

## What It Provides

- `bridge_agent_status`
- `bridge_agent_delegate`
- `bridge_agent_delegate_start`
- `bridge_agent_delegate_status`
- `bridge_agent_delegate_result`
- `bridge_agent_delegate_cancel`
- `windows_agent_*` compatibility aliases for older bridge clients
- `bridge_peer_*` tools for authenticated Hermes-to-Hermes delegation

`windows_agent_delegate` runs the task through native Windows Hermes:

```text
hermes chat --query ... --quiet --source mcp-windows-proxy --accept-hooks
```

The delegate tool now resumes a persistent Hermes session per A0 thread key
when `a0_thread_key` is provided. Long-running work should use
`windows_agent_delegate_start`, then poll with `windows_agent_delegate_status`
or `windows_agent_delegate_result`.

It does not expose a raw PowerShell or CMD proxy.

## Hermes-to-Hermes Peer Bridge

Peer mode runs the same MCP server directly over Streamable HTTP and does not
require `supergateway`:

```powershell
$env:HERMES_BRIDGE_PAIR_KEY = "same-secret-on-each-paired-agent"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\hermes\bin\start-windows-hermes-peer-bridge.ps1
```

Android/Termux:

```sh
python -m pip install -r requirements-android.txt
cp config/android-peer.env.example config/android-peer.env
sh bin/start-android-hermes-peer-bridge.sh
```

Peer endpoints default to `http://<LAN-IP>:18084/mcp`. Keep `18082` and `18083`
for the existing A0 live/staging bridge.

Static peer config lives at
`$HERMES_BRIDGE_PEERS_CONFIG` or `~/.hermes/bridge-state/peers.json`:

```json
{
  "peers": [
    {
      "peer_id": "quest3",
      "url": "http://QUEST_LAN_IP:18084/mcp",
      "platform": "android",
      "pair_key_env": "HERMES_BRIDGE_PAIR_KEY"
    }
  ]
}
```

Peer tools:

- `bridge_peer_status(peer_id)`
- `bridge_peer_delegate_start(peer_id, prompt, cwd?, timeout_seconds?, max_turns?, conversation_key?)`
- `bridge_peer_delegate_status(peer_id, task_id)`
- `bridge_peer_delegate_result(peer_id, task_id)`
- `bridge_peer_delegate_cancel(peer_id, task_id)`

LAN-facing peer bridge startup requires `HERMES_BRIDGE_PAIR_KEY` unless
explicitly run with the unsafe development override. Both paired agents should
use the same pair key for the simplest setup. For multi-peer setups, each peer
entry can use a distinct `pair_key_env`. Legacy `HERMES_BRIDGE_AUTH_TOKEN`,
`token`, and `token_env` values are still accepted for existing installs.

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

Expected result includes direct delegation tools such as:

```text
bridge_agent_status
bridge_agent_delegate
bridge_agent_delegate_start
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
- `bin/start-windows-hermes-bridge-staging.ps1` - starts a staging bridge on port 18083
- `bin/start-windows-hermes-peer-bridge.ps1` - starts native HTTP peer bridge on port 18084
- `bin/start-android-hermes-peer-bridge.sh` - starts native HTTP peer bridge on Android/Termux
- `bin/windows-hermes-bridge-background-watchdog.ps1` - hidden bridge watchdog
- `bin/start-windows-hermes-gateway.ps1` - optional native Windows Hermes messaging gateway helper
- `bin/windows-hermes-gateway-background-watchdog.ps1` - optional messaging gateway watchdog
- `config/*.example.*` - peer config and env templates
- `docs/quest-android-setup.md` - Android/Quest setup checklist
- `startup/*.vbs` - hidden Startup-folder launchers

## Notes

- Existing Docker Hermes sessions may cache MCP tools. Restart Docker Hermes or
  start a new session after installing.
- The bridge endpoint remains `windows-hermes` at
  `host.docker.internal:18082/mcp`.
- Delegated tasks use Windows Hermes normal approval policy.
