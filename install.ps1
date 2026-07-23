param(
    [string]$A0SettingsPath
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$CanonicalHome = Join-Path $env:LOCALAPPDATA "agent-bridge"
$BridgeHome = if ($env:AGENT_BRIDGE_HOME) {
    $env:AGENT_BRIDGE_HOME
} elseif ($env:HERMES_BRIDGE_HOME) {
    $env:HERMES_BRIDGE_HOME
} elseif (Test-Path (Join-Path $HermesHome "bridge-state")) {
    $HermesHome
} else {
    $CanonicalHome
}
$BridgeBin = Join-Path $BridgeHome "bin"
$StartupDir = [Environment]::GetFolderPath("Startup")
$HermesPython = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"

New-Item -ItemType Directory -Force -Path $BridgeBin | Out-Null

if (Test-Path -LiteralPath $HermesPython) {
    Write-Host "Installing the versioned native bridge runtime and pinned dependencies..."
    & $HermesPython (Join-Path $RepoRoot "bootstrap.py") install --source $RepoRoot | Out-Host
} else {
    Write-Warning "Hermes Python was not found at $HermesPython; run bootstrap.py install to create the bridge runtime."
}

$binFiles = @(
    "agent-bridge-mcp.py",
    "agent_bridge_universal.py",
    "agent-bridge-mcp-serve.cmd",
    "start-agent-bridge.ps1",
    "start-agent-bridge-peer.ps1",
    "start-agent-bridge-peer.sh",
    "agent-bridge-background-watchdog.ps1",
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
    Copy-Item -LiteralPath (Join-Path $RepoRoot "bin\$file") -Destination (Join-Path $BridgeBin $file) -Force
}

$startupFiles = @(
    "Watch Agent Bridge MCP.vbs",
    "Watch Windows Hermes MCP Bridge.vbs"
)

foreach ($file in $startupFiles) {
    Copy-Item -LiteralPath (Join-Path $RepoRoot "startup\$file") -Destination (Join-Path $StartupDir $file) -Force
}

$bridge = Join-Path $BridgeBin "start-agent-bridge.ps1"

Write-Host "Installed Agent Bridge MCP scripts to: $BridgeBin"
Write-Host "Installed hidden Startup launchers to: $StartupDir"

Write-Host "Starting native Agent Bridge MCP and secure peer listener..."
powershell -NoProfile -ExecutionPolicy Bypass -File $bridge | Out-Host
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $BridgeBin "start-agent-bridge-peer.ps1") | Out-Host

if ($A0SettingsPath) {
    $helper = Join-Path $RepoRoot "scripts\configure-a0-mcp.py"
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { $python = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $python) {
        Write-Warning "Python was not found. Cannot update A0 settings automatically."
    } elseif (-not (Test-Path -LiteralPath $A0SettingsPath)) {
        Write-Warning "A0 settings path was not found: $A0SettingsPath"
    } else {
        Write-Host "Adding agent-bridge to A0 MCP settings..."
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
Write-Host "  agent-bridge -> http://host.docker.internal:18082/mcp"
Write-Host "  hermes-bridge and windows-hermes remain compatibility aliases"
Write-Host "  expected bridge_version: v1.3.5"
Write-Host ""
Write-Host "Verify from Docker Hermes:"
Write-Host "  hermes mcp test hermes-bridge"
Write-Host ""
Write-Host "A0 helper:"
Write-Host "  python scripts/configure-a0-mcp.py --settings /a0/usr/settings.json --dry-run --check-health"
