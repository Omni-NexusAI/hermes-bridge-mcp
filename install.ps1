param(
    [string]$A0SettingsPath
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$HermesBin = Join-Path $HermesHome "bin"
$StartupDir = [Environment]::GetFolderPath("Startup")
$HermesPython = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $HermesBin | Out-Null

if (Test-Path -LiteralPath $HermesPython) {
    Write-Host "Installing the versioned native bridge runtime and pinned dependencies..."
    & $HermesPython (Join-Path $RepoRoot "bootstrap.py") install --source $RepoRoot | Out-Host
} else {
    Write-Warning "Hermes Python was not found at $HermesPython; run bootstrap.py install to create the bridge runtime."
}

$binFiles = @(
    "hermes-bridge-mcp-serve.cmd",
    "start-hermes-bridge-peer.ps1",
    "hermes_bridge_network.py",
    "bridge_pairing_tools.py",
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

Write-Host "Starting native Hermes Bridge MCP and secure peer listener..."
powershell -NoProfile -ExecutionPolicy Bypass -File $bridge | Out-Host
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $HermesBin "start-windows-hermes-peer-bridge.ps1") | Out-Host

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
Write-Host "Docker Hermes config:"
Write-Host "  hermes-bridge -> http://host.docker.internal:18082/mcp"
Write-Host "  hermes-bridge-staging -> http://host.docker.internal:18083/mcp"
Write-Host "  hermes-bridge legacy peer HTTP -> http://<LAN-IP>:18084/mcp"
Write-Host "  hermes-bridge automatic peer HTTPS -> https://<LAN-IP>:18443/mcp"
Write-Host "  legacy names windows-hermes and windows-hermes-staging still point to the same bridge if already configured"
Write-Host "  expected bridge_version: v1.3.1"
Write-Host ""
Write-Host "Verify from Docker Hermes:"
Write-Host "  hermes mcp test hermes-bridge"
Write-Host ""
Write-Host "A0 helper:"
Write-Host "  python scripts/configure-a0-mcp.py --settings /a0/usr/settings.json --dry-run --check-health"
