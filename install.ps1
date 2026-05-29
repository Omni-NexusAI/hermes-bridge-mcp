param(
    [switch]$InstallSupergateway,
    [string]$A0SettingsPath
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$HermesBin = Join-Path $HermesHome "bin"
$StartupDir = [Environment]::GetFolderPath("Startup")
$BridgeUrl = "http://host.docker.internal:18082/mcp"
$BridgeHealthUrl = "http://host.docker.internal:18082/healthz"

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

if (-not (Get-Command supergateway.cmd -ErrorAction SilentlyContinue)) {
    if ($InstallSupergateway) {
        Write-Host "supergateway.cmd was not found. Installing with npm..."
        & npm install -g supergateway
    } else {
        Write-Warning "supergateway.cmd was not found. Install it with: npm install -g supergateway"
    }
}

if (Get-Command supergateway.cmd -ErrorAction SilentlyContinue) {
    Write-Host "Starting Windows Hermes gateway..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $gateway

    Write-Host "Starting Windows Hermes MCP bridge..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File $bridge -Restart
}

if ($A0SettingsPath) {
    $helper = Join-Path $RepoRoot "scripts\configure-a0-mcp.py"
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $python) {
        Write-Warning "Python was not found. Cannot update A0 settings automatically."
    } elseif (-not (Test-Path -LiteralPath $A0SettingsPath)) {
        Write-Warning "A0 settings path was not found: $A0SettingsPath"
    } else {
        Write-Host "Adding hermes-bridge to A0 MCP settings..."
        & $python $helper --settings $A0SettingsPath
    }
}

Write-Host ""
Write-Host "MCP client config entry:"
Write-Host "  name: hermes-bridge"
Write-Host "  type: streamable-http"
Write-Host "  url:  $BridgeUrl"
Write-Host ""
Write-Host "A0 quick setup from the A0 container:"
Write-Host "  python scripts/configure-a0-mcp.py --settings /a0/usr/settings.json"
Write-Host "  Restart A0, then verify Settings > MCP/A2A > External MCP Servers > Open."
Write-Host ""
Write-Host "Verify from an MCP client/container:"
Write-Host "  $BridgeHealthUrl"
