$ErrorActionPreference = "Stop"

$HermesHome = "$env:LOCALAPPDATA\hermes"
$BridgeCmd = Join-Path $HermesHome "bin\windows-hermes-mcp-serve.cmd"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "windows-bridge-supergateway-staging.log"
$ErrPath = Join-Path $LogDir "windows-bridge-supergateway-staging.err.log"
$PidPath = Join-Path $HermesHome "windows-bridge-supergateway-staging.pid"
$Port = 18083

function Test-PortOpen {
    param([int]$PortToCheck)
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $task = $client.ConnectAsync('127.0.0.1', $PortToCheck)
        if (-not $task.Wait(2000)) { $client.Dispose(); return $false }
        $client.Dispose()
        return $true
    } catch {
        return $false
    }
}

function Get-ListenerPid {
    param([int]$PortToCheck)
    $lines = cmd /c "netstat -ano | findstr :$PortToCheck" 2>$null
    foreach ($line in $lines) {
        if ($line -match "LISTENING\s+(\d+)\s*$") { return [int]$Matches[1] }
    }
    return $null
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if ((-not (Test-Path $PidPath)) -and (Test-PortOpen -PortToCheck $Port)) {
    $existingPid = Get-ListenerPid -PortToCheck $Port
    if ($existingPid) { $existingPid | Set-Content -Path $PidPath -NoNewline }
    Write-Output "OK: existing staging bridge is already listening on port $Port"
    return
}

if (Test-Path $PidPath) {
    $oldPid = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
}

Start-Sleep -Seconds 1

$supergateway = (Get-Command supergateway.cmd -ErrorAction SilentlyContinue).Source
if (-not $supergateway) {
    $supergateway = (Get-Command supergateway -ErrorAction Stop).Source
}

if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
if (Test-Path $ErrPath) { Remove-Item $ErrPath -Force -ErrorAction SilentlyContinue }

$arguments = @(
    "--stdio", "`"$BridgeCmd`"",
    "--port", "$Port",
    "--baseUrl", "http://localhost:$Port",
    "--outputTransport", "streamableHttp",
    "--streamableHttpPath", "/mcp",
    "--stateful",
    "--sessionTimeout", "600000",
    "--healthEndpoint", "/healthz",
    "--logLevel", "info"
)

$process = Start-Process -FilePath $supergateway -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru
$process.Id | Set-Content -Path $PidPath -NoNewline

Start-Sleep -Seconds 3

if (Test-PortOpen -PortToCheck $Port) {
    Write-Output "OK: staging supergateway PID=$($process.Id) listening on port $Port"
} else {
    Write-Output "WARN: staging supergateway PID=$($process.Id) started but port $Port is not reachable"
    Write-Output "--- stderr (last 20 lines) ---"
    Get-Content $ErrPath -Tail 20 -ErrorAction SilentlyContinue
}
