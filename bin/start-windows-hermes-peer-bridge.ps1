$ErrorActionPreference = "Stop"

$HermesHome = "$env:LOCALAPPDATA\hermes"
$PythonExe = "$HermesHome\hermes-agent\venv\Scripts\python.exe"
$BridgeScript = Join-Path $HermesHome "bin\windows-hermes-proxy-mcp.py"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "windows-peer-bridge.log"
$ErrPath = Join-Path $LogDir "windows-peer-bridge.err.log"
$PidPath = Join-Path $HermesHome "windows-peer-bridge.pid"
$Port = if ($env:HERMES_BRIDGE_PORT) { [int]$env:HERMES_BRIDGE_PORT } else { 18084 }
$HostAddress = if ($env:HERMES_BRIDGE_HOST) { $env:HERMES_BRIDGE_HOST } else { "0.0.0.0" }

if (-not $env:HERMES_BRIDGE_AUTH_TOKEN) {
    throw "HERMES_BRIDGE_AUTH_TOKEN is required for LAN-facing peer bridge"
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path $PidPath) {
    $oldPid = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Output "OK: Windows Hermes peer bridge already running as PID $oldPid"
            return
        }
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
}

if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
if (Test-Path $ErrPath) { Remove-Item $ErrPath -Force -ErrorAction SilentlyContinue }

$arguments = @(
    $BridgeScript,
    "--transport", "streamable-http",
    "--host", $HostAddress,
    "--port", "$Port"
)

$process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru
$process.Id | Set-Content -Path $PidPath -NoNewline
Start-Sleep -Seconds 3

if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
    Write-Output "OK: Windows Hermes peer bridge started as PID $($process.Id) on $HostAddress`:$Port"
} else {
    Write-Output "WARN: Windows Hermes peer bridge process exited after start"
    Write-Output "--- stderr (last 40 lines) ---"
    Get-Content $ErrPath -Tail 40 -ErrorAction SilentlyContinue
}
