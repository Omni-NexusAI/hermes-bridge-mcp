$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$HermesBin = Join-Path $HermesHome "bin"
$StartupDir = [Environment]::GetFolderPath("Startup")

New-Item -ItemType Directory -Force -Path $HermesBin | Out-Null

$binFiles = @(
    "windows-hermes-proxy-mcp.py",
    "windows-hermes-mcp-serve.cmd",
    "start-windows-hermes-bridge.ps1",
    "windows-hermes-bridge-background-watchdog.ps1",
    "start-windows-hermes-gateway.ps1",
    "windows-hermes-gateway-background-watchdog.ps1"
)

foreach ($file in $binFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "bin\$file") -Destination (Join-Path $HermesBin $file) -Force
}

$startupFiles = @(
    "Watch Windows Hermes MCP Bridge.vbs",
    "Watch Windows Hermes Gateway.vbs"
)

foreach ($file in $startupFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "startup\$file") -Destination (Join-Path $StartupDir $file) -Force
}

$bridge = Join-Path $HermesBin "start-windows-hermes-bridge.ps1"
$gateway = Join-Path $HermesBin "start-windows-hermes-gateway.ps1"

Write-Host "Installed Windows Hermes proxy MCP scripts to: $HermesBin"
Write-Host "Installed hidden Startup launchers to: $StartupDir"

if (Get-Command supergateway.cmd -ErrorAction SilentlyContinue) {
    Write-Host "Starting Windows Hermes gateway..."
    powershell -NoProfile -ExecutionPolicy Bypass -File $gateway | Out-Host

    Write-Host "Starting Windows Hermes MCP bridge..."
    powershell -NoProfile -ExecutionPolicy Bypass -File $bridge | Out-Host
} else {
    Write-Warning "supergateway.cmd was not found. Install it with: npm install -g supergateway"
}

Write-Host ""
Write-Host "Docker Hermes config:"
Write-Host "  windows-hermes -> http://host.docker.internal:18082/mcp"
Write-Host ""
Write-Host "Verify from Docker Hermes:"
Write-Host "  hermes mcp test windows-hermes"
