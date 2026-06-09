# Hermes Bridge MCP

Current bridge version: `v1.2.7`.

Bridge one Hermes agent to another Hermes agent through MCP.

Hermes Bridge MCP exposes direct delegation tools so a Docker-hosted agent such
as A0/Agentspine can delegate work to a local Hermes Bridge agent. Delegated work runs
through Hermes itself, so Hermes can use its normal tools, session persistence,
memory, and approval policy.

It can also run as a native Streamable HTTP peer bridge for Hermes-to-Hermes
agent communication across a trusted network. The first supported peer targets
are Windows and Android/Termux, with Quest 3 as the first Android validation
device.

This bridge is not a Hermes messaging gateway proxy. It does not expose Telegram,
Discord, Slack, WhatsApp, or `messages_send`/`conversations_list` tools.
Messaging gateway launchers and watchdogs are intentionally not shipped by this
bridge anymore; install Hermes messaging integrations separately if needed.

## What It Provides

- `bridge_agent_status`
- `bridge_agent_delegate`
- `bridge_agent_delegate_start`
- `bridge_agent_delegate_status`
- `bridge_agent_delegate_result`
- `bridge_agent_delegate_cancel`
- `bridge_peer_*` tools for authenticated Hermes-to-Hermes delegation

Legacy `windows_agent_*` aliases are hidden by default. Set
`HERMES_BRIDGE_ENABLE_LEGACY_WINDOWS_TOOLS=1` only for older bridge clients that
still call those names.

`bridge_agent_delegate` runs the task through the local Hermes Bridge agent:

```text
hermes chat --query ... --quiet --source mcp-hermes-bridge --accept-hooks
```

The delegate tool now resumes a persistent Hermes session per A0 thread key
when `a0_thread_key` is provided. Long-running work should use
`bridge_agent_delegate_start`, then poll with `bridge_agent_delegate_status`
or `bridge_agent_delegate_result`.

Adaptive timeout behavior keeps long delegated tasks alive after the initiating
MCP call returns. For `bridge_agent_delegate`,
`timeout_seconds` is the inline wait before a pollable `task_id` is returned;
the background task has a separate longer `hard_timeout_seconds`. Set
`kill_on_timeout=true` only when the caller explicitly wants the old destructive
timeout behavior.

It does not expose a raw PowerShell or CMD proxy.

## Hermes-to-Hermes Peer Bridge

Peer mode runs the same MCP server directly over Streamable HTTP and does not
require `supergateway`:

```powershell
$env:HERMES_BRIDGE_PAIR_KEY = "same-secret-on-each-paired-agent"
powershell -ExecutionPolicy Bypass -File $env:LOCALAPPDATA\hermes\bin\start-hermes-bridge-peer.ps1
```

Android/Termux:

```sh
python -m pip install -r requirements-android.txt
cp config/android-peer.env.example config/android-peer.env
sh bin/start-hermes-bridge-peer.sh
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
- `bridge_peer_delegate_start(peer_id, prompt, cwd?, timeout_seconds?, max_turns?, conversation_key?, hard_timeout_seconds?)`
- `bridge_peer_delegate_status(peer_id, task_id)`
- `bridge_peer_delegate_result(peer_id, task_id)`
- `bridge_peer_delegate_cancel(peer_id, task_id)`

LAN-facing peer bridge startup requires `HERMES_BRIDGE_PAIR_KEY` unless
explicitly run with the unsafe development override. Both paired agents should
use the same pair key for the simplest setup. For multi-peer setups, each peer
entry can use a distinct `pair_key_env`. Legacy `HERMES_BRIDGE_AUTH_TOKEN`,
`token`, and `token_env` values are still accepted for existing installs.

Peer mode uses shared bearer-token authentication only in `v1.2.7`. OAuth-based
peer discovery is deferred until manual pairing is stable.

## Requirements

- A local Hermes install available to the bridge runtime
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

If the A0 `settings.json` file is available from Windows, the installer can add
or update the `hermes-bridge` MCP entry:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -A0SettingsPath C:\path\to\settings.json
```

## Docker Hermes Config

In Docker Hermes `config.yaml`:

```yaml
mcp_servers:
  hermes-bridge:
    url: http://host.docker.internal:18082/mcp
    timeout: 120
    connect_timeout: 30
```

Do not add `transport: sse` for this bridge. It uses supergateway
Streamable HTTP at `/mcp`.

## Verify

From the Docker Hermes container:

```bash
hermes mcp test hermes-bridge
```

Expected result includes direct delegation tools such as:

```text
bridge_agent_status
bridge_agent_delegate
bridge_agent_delegate_start
bridge_peer_status
```

By default, the bridge exposes exactly 11 tools:

```text
bridge_agent_status
bridge_agent_delegate
bridge_agent_delegate_start
bridge_agent_delegate_status
bridge_agent_delegate_result
bridge_agent_delegate_cancel
bridge_peer_status
bridge_peer_delegate_start
bridge_peer_delegate_status
bridge_peer_delegate_result
bridge_peer_delegate_cancel
```

`bridge_agent_status` should report `bridge_version` as `v1.2.7` on every
platform. It reports the local Hermes runtime separately as `hermes_version`.

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
                "prompt": "Reply exactly HERMES_BRIDGE_OK and do not use tools.",
                "timeout_seconds": 180,
                "max_turns": 10,
            })
            print(result.content[0].text)

asyncio.run(main())
```

## Files

- `bin/windows-hermes-proxy-mcp.py` - universal companion MCP server; filename retained for compatibility
- `bin/hermes-bridge-mcp-serve.cmd` - universal stdio entrypoint wrapper for supergateway on Windows
- `bin/start-hermes-bridge-peer.ps1` - universal Windows peer launcher wrapper
- `bin/start-hermes-bridge-peer.sh` - universal POSIX/Android peer launcher wrapper
- `bin/start-windows-hermes-bridge*.ps1` - Windows-specific A0 bridge launchers
- `bin/start-android-hermes-peer-bridge.sh` - Android/Termux-specific peer launcher implementation
- `bin/windows-hermes-bridge-background-watchdog.ps1` - Windows-specific bridge watchdog
- `scripts/configure-a0-mcp.py` - backup-first A0 MCP settings helper
- `config/*.example.*` - peer config and env templates
- `docs/quest-android-setup.md` - Android/Quest setup checklist
- `startup/*.vbs` - hidden Startup-folder launchers

## Notes

- Existing Docker Hermes sessions may cache MCP tools. Restart Docker Hermes or
  start a new session after installing.
- New installs should use the MCP server name `hermes-bridge`. Existing
  `windows-hermes` endpoint names can remain as legacy configuration aliases.
- Delegated tasks use the local Hermes agent's normal approval policy.
