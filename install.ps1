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
$BootstrapPython = $env:AGENT_BRIDGE_PYTHON
if (-not $BootstrapPython) { $BootstrapPython = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $BootstrapPython) { $BootstrapPython = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $BootstrapPython -and (Test-Path -LiteralPath $HermesPython)) { $BootstrapPython = $HermesPython }
if (-not $BootstrapPython) { throw "Install Python 3.10+ or set AGENT_BRIDGE_PYTHON; Hermes is optional." }
& $BootstrapPython -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Bootstrap requires working Python 3.10+. No bridge launchers were changed." }
$env:AGENT_BRIDGE_HOME = $BridgeHome
Write-Host "Installing the versioned native bridge runtime and pinned dependencies..."
& $BootstrapPython (Join-Path $RepoRoot "bootstrap.py") install --source $RepoRoot | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Runtime installation failed. Existing release and startup entries were retained." }
New-Item -ItemType Directory -Force -Path $BridgeBin | Out-Null
# Compatibility entrypoints and helpers must move together. The active runtime
# remains selected atomically by current.json, including its dependency Python.
Get-ChildItem -LiteralPath (Join-Path $RepoRoot "bin") -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $BridgeBin $_.Name) -Force
}
$ExistingStartup = @("Watch Agent Bridge MCP.vbs", "Watch Windows Hermes MCP Bridge.vbs") |
    Where-Object { Test-Path -LiteralPath (Join-Path $StartupDir $_) }
if (-not $ExistingStartup) {
    $watchdog = (Join-Path $BridgeBin "agent-bridge-background-watchdog.ps1").Replace('"', '""')
    $startupHome = $BridgeHome.Replace('"', '""')
    $launcher = @"
Set shell = CreateObject("WScript.Shell")
shell.Environment("Process")("AGENT_BRIDGE_HOME") = "$startupHome"
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$watchdog""", 0, False
"@
    Set-Content -LiteralPath (Join-Path $StartupDir "Watch Agent Bridge MCP.vbs") -Value $launcher -Encoding Unicode
} else {
    Write-Host "Preserved existing Startup entry: $($ExistingStartup -join ', '). Both compatibility watchdogs share one owner lock."
}

$bridge = Join-Path $BridgeBin "start-agent-bridge.ps1"

Write-Host "Installed Agent Bridge MCP scripts to: $BridgeBin"
Write-Host "Installed hidden Startup launchers to: $StartupDir"

Write-Host "Starting native Agent Bridge MCP and secure peer listener..."
powershell -NoProfile -ExecutionPolicy Bypass -File $bridge | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Local listener did not report readiness. Checkpoint active tasks before a deliberate -Restart." }
powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $BridgeBin "start-agent-bridge-peer.ps1") | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Peer listener did not report readiness; inspect local diagnostics." }

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
