$ErrorActionPreference = "Stop"

$HermesHome = "$env:LOCALAPPDATA\hermes"
$PythonExe = "$HermesHome\hermes-agent\venv\Scripts\python.exe"
$BridgeScript = Join-Path $HermesHome "bin\windows-hermes-proxy-mcp.py"
$LogDir = Join-Path $HermesHome "logs"
$HostAddress = if ($env:HERMES_BRIDGE_HOST) { $env:HERMES_BRIDGE_HOST } else { "0.0.0.0" }
$LegacyPort = if ($env:HERMES_BRIDGE_PORT) { [int]$env:HERMES_BRIDGE_PORT } else { 18084 }
$SecurePort = if ($env:HERMES_BRIDGE_SECURE_PORT) { [int]$env:HERMES_BRIDGE_SECURE_PORT } else { 18443 }
$DiscoveryEnabled = $env:HERMES_BRIDGE_AUTO_DISCOVERY -ne "0"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Start-BridgeProcess {
    param(
        [string]$Name,
        [int]$Port,
        [switch]$Secure
    )

    $PidPath = Join-Path $HermesHome "$Name.pid"
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

    $arguments = @($BridgeScript, "--transport", "streamable-http", "--host", $HostAddress, "--port", "$Port")
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

if ($env:HERMES_BRIDGE_PAIR_KEY -or $env:HERMES_BRIDGE_AUTH_TOKEN) {
    Start-BridgeProcess -Name "windows-peer-bridge" -Port $LegacyPort
} elseif (-not $DiscoveryEnabled) {
    throw "Set HERMES_BRIDGE_PAIR_KEY for legacy peers or HERMES_BRIDGE_AUTO_DISCOVERY=1 for automatic pairing."
} else {
    Write-Output "INFO: legacy HTTP peer listener skipped because no shared pair key is configured"
}

if ($DiscoveryEnabled) {
    Start-BridgeProcess -Name "windows-secure-peer-bridge" -Port $SecurePort -Secure
} else {
    Write-Output "INFO: secure discovery listener disabled by HERMES_BRIDGE_AUTO_DISCOVERY=0"
}
