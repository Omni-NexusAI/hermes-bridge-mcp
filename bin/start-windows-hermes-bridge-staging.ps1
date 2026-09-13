param([switch]$Restart)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "agent-bridge-launcher-common.ps1")
Initialize-BridgeRuntime
$env:AGENT_BRIDGE_AUTH_TOKEN = Get-BridgeSetting "STAGING_TOKEN" ""
if (-not $env:AGENT_BRIDGE_AUTH_TOKEN) { throw "Set AGENT_BRIDGE_STAGING_TOKEN before starting the staging listener." }
$env:HERMES_BRIDGE_AUTH_TOKEN = $env:AGENT_BRIDGE_AUTH_TOKEN
Start-ManagedBridge -Name "windows-bridge-staging" -Port ([int](Get-BridgeSetting "STAGING_PORT" 18083)) -BindAddress "127.0.0.1" -Restart:$Restart
