$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$HermesBin = Join-Path $HermesHome "bin"
$StartupDir = [Environment]::GetFolderPath("Startup")

New-Item -ItemType Directory -Force -Path $HermesBin | Out-Null

$binFiles = @(
    "hermes-bridge-mcp-serve.cmd",
    "start-hermes-bridge-peer.ps1",
    "windows-hermes-proxy-mcp.py",
    "windows-hermes-mcp-serve.cmd",
    "start-windows-hermes-bridge.ps1",
    "start-windows-hermes-bridge-staging.ps1",
    "start-windows-hermes-peer-bridge.ps1",
    "windows-hermes-bridge-background-watchdog.ps1"
)

foreach ($file in $binFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "bin\$file") -Destination (Join-Path $HermesBin $file) -Force
}

$startupFiles = @(
    "Watch Windows Hermes MCP Bridge.vbs"
)

foreach ($file in $startupFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "startup\$file") -Destination (Join-Path $StartupDir $file) -Force
}

$bridge = Join-Path $HermesBin "start-windows-hermes-bridge.ps1"

Write-Host "Installed Hermes Bridge MCP scripts to: $HermesBin"
Write-Host "Installed hidden Startup launchers to: $StartupDir"

if (Get-Command supergateway.cmd -ErrorAction SilentlyContinue) {
    Write-Host "Starting Hermes Bridge MCP..."
    powershell -NoProfile -ExecutionPolicy Bypass -File $bridge | Out-Host
} else {
    Write-Warning "supergateway.cmd was not found. Install it with: npm install -g supergateway"
}

Write-Host ""
Write-Host "Docker Hermes config:"
Write-Host "  hermes-bridge -> http://host.docker.internal:18082/mcp"
Write-Host "  hermes-bridge-staging -> http://host.docker.internal:18083/mcp"
Write-Host "  hermes-bridge peer HTTP -> http://<LAN-IP>:18084/mcp"
Write-Host "  legacy names windows-hermes and windows-hermes-staging still point to the same bridge if already configured"
Write-Host ""
Write-Host "Verify from Docker Hermes:"
Write-Host "  hermes mcp test hermes-bridge"
