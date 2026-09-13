param([switch]$Restart)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "start-windows-hermes-peer-bridge.ps1") -Restart:$Restart
