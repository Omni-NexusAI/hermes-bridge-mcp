$ErrorActionPreference = "Stop"

$LegacyHome = Join-Path $env:LOCALAPPDATA "hermes"
$CanonicalHome = Join-Path $env:LOCALAPPDATA "agent-bridge"
$BridgeHome = if ($env:AGENT_BRIDGE_HOME) {
    $env:AGENT_BRIDGE_HOME
} elseif ($env:HERMES_BRIDGE_HOME) {
    $env:HERMES_BRIDGE_HOME
} elseif (Test-Path (Join-Path $LegacyHome "bridge-state")) {
    $LegacyHome
} else {
    $CanonicalHome
}
$RuntimeRoot = Join-Path $BridgeHome "bridge-runtime"
$Marker = Join-Path $RuntimeRoot "current.json"
$Release = if (Test-Path $Marker) { (Get-Content $Marker -Raw | ConvertFrom-Json).release } else { $BridgeHome }
$PythonExe = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) { $PythonExe = Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe" }
$BridgeScript = Join-Path $Release "bin\agent-bridge-mcp.py"
if (-not (Test-Path $BridgeScript)) { $BridgeScript = Join-Path $Release "bin\windows-hermes-proxy-mcp.py" }
$LogDir = Join-Path $BridgeHome "logs"
$HostAddress = if ($env:AGENT_BRIDGE_HOST) { $env:AGENT_BRIDGE_HOST } elseif ($env:HERMES_BRIDGE_HOST) { $env:HERMES_BRIDGE_HOST } else { "0.0.0.0" }
$LegacyPort = if ($env:AGENT_BRIDGE_PORT) { [int]$env:AGENT_BRIDGE_PORT } elseif ($env:HERMES_BRIDGE_PORT) { [int]$env:HERMES_BRIDGE_PORT } else { 18084 }
$SecurePort = if ($env:AGENT_BRIDGE_SECURE_PORT) { [int]$env:AGENT_BRIDGE_SECURE_PORT } elseif ($env:HERMES_BRIDGE_SECURE_PORT) { [int]$env:HERMES_BRIDGE_SECURE_PORT } else { 18443 }
$DiscoverySetting = if ($env:AGENT_BRIDGE_AUTO_DISCOVERY) { $env:AGENT_BRIDGE_AUTO_DISCOVERY } else { $env:HERMES_BRIDGE_AUTO_DISCOVERY }
$DiscoveryEnabled = $DiscoverySetting -ne "0"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Start-BridgeProcess {
    param(
        [string]$Name,
        [int]$Port,
        [switch]$Secure
    )

    $PidPath = Join-Path $BridgeHome "$Name.pid"
    $LogPath = Join-Path $LogDir "$Name.log"
    $ErrPath = Join-Path $LogDir "$Name.err.log"

    if (Test-Path $PidPath) {
        $oldPid = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
            Write-Output "OK: $Name already running as PID $oldPid"
            return
        }
        Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
    }

    $arguments = @($BridgeScript, "--transport", "streamable-http", "--host", $HostAddress, "--port", "$Port", "--stateless-http", "--json-response")
    if ($Secure) { $arguments += "--secure-network" }
    $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru
    $process.Id | Set-Content -Path $PidPath -NoNewline
    Start-Sleep -Seconds 2
    if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
        $transport = if ($Secure) { "HTTPS" } else { "legacy HTTP" }
        Write-Output "OK: $Name PID=$($process.Id) listening with $transport on $HostAddress`:$Port"
    } else {
        Write-Output "WARN: $Name exited after start"
        Get-Content $ErrPath -Tail 40 -ErrorAction SilentlyContinue
    }
}

$PairKeyConfigured = (
    $env:AGENT_BRIDGE_PAIR_KEY -or
    $env:AGENT_BRIDGE_AUTH_TOKEN -or
    $env:HERMES_BRIDGE_PAIR_KEY -or
    $env:HERMES_BRIDGE_AUTH_TOKEN
)
if ($PairKeyConfigured) {
    Start-BridgeProcess -Name "agent-peer-bridge" -Port $LegacyPort
} elseif (-not $DiscoveryEnabled) {
    throw "Set AGENT_BRIDGE_PAIR_KEY for legacy peers or AGENT_BRIDGE_AUTO_DISCOVERY=1 for automatic pairing."
} else {
    Write-Output "INFO: legacy HTTP peer listener skipped because no shared pair key is configured"
}

if ($DiscoveryEnabled) {
    Start-BridgeProcess -Name "agent-secure-peer-bridge" -Port $SecurePort -Secure
} else {
    Write-Output "INFO: secure discovery listener disabled by AGENT_BRIDGE_AUTO_DISCOVERY=0"
}
