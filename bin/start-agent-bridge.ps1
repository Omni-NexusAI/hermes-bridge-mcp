param([switch]$Restart)
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "start-windows-hermes-bridge.ps1") -Restart:$Restart
